"""Settings: config.toml at the project root, environment variable overrides.

The project root is the parent of src/ (editable install keeps this stable),
so the server works regardless of the harness's working directory. Secrets
live in config.toml (gitignored) or in environment variables; env always wins.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("WORK_RESEARCHER_CONFIG", PROJECT_ROOT / "config.toml"))

DEFAULTS: dict = {
    "general": {
        "cv_dir": "CV_collection",
        "data_dir": "data",
        "log_level": "INFO",
        "active_profile": "",
    },
    "search": {
        "default_location": "UK",
        "default_radius_miles": 40,
        "default_max_days_old": 14,
        "default_work_from_home": False,
        "default_limit_per_source": 25,
        "provider_timeout_s": 30,
        "exclude_training_offers": True,
    },
    "applicant": {
        "full_name": "",
        "email": "",
        "phone": "",
        "date_of_birth": "",  # "YYYY-MM-DD" — Reed/Totaljobs wizards ask
        "nationality": "",
        "right_to_work": "",
        "right_to_work_docs": "",  # e.g. "UK passport", "settled status"
        "uk_residence_years": "",  # "lived in UK/EU/EEA 3+ years" wizard question
        "age_group": "",  # e.g. "35-39" (wizard dropdowns)
        "gender": "",  # Reed wizard Q5
        "ethnicity": "",  # Reed wizard Q6 (UK census groups)
        "still_in_education": False,  # Reed wizard Q7
        "earliest_start_date": "",  # e.g. "2026-09-07"; blank = ~2 Mondays ahead
        "highest_qualification": "",
        "past_apprenticeship": False,
        "owns_car": False,
        "location": "",
        "home_location": "",
        "home_postcode": "",
        "max_commute_miles": 40,
        "daily_commute_miles": 25,
        "occasional_commute_miles": 50,
        "willing_to_relocate": False,
        "relocate_areas": [],
        "notice_period": "",
        "salary_expectation": "",
        "linkedin": "",
    },
    "browser": {
        "headless": False,
        "channel": "msedge",  # real Edge passes WDAC + board anti-bot; falls
        # back to bundled Chromium when Edge is absent
        "default_timeout_ms": 15000,
    },
    "auth": {
        # Pre-approved by the user: the agent may click "Sign in with Google"
        # and pick this account on job boards WITHOUT asking permission.
        # 2FA/captcha still stops and asks the user.
        "google_account": "",
        "auto_google_signin": True,
    },
    "blocklist": {
        # Seeded into the DB blocklist on first use; runtime additions go
        # through the manage_blocklist MCP tool and persist in the DB.
        "companies": [],
    },
    "providers": {
        "totaljobs": {"enabled": True},
        "reed": {"enabled": True, "api_key": ""},
        "adzuna": {"enabled": True, "app_id": "", "app_key": ""},
        "jooble": {"enabled": True, "api_key": ""},
        "earthworks": {"enabled": True, "uk_only": True},
        "findajob": {"enabled": True, "browser_only": True},
    },
}


@dataclass
class Settings:
    project_root: Path = PROJECT_ROOT
    config_path: Path = CONFIG_PATH
    data_dir: Path = PROJECT_ROOT / "data"
    cv_dir: Path = PROJECT_ROOT / "CV_collection"
    db_path: Path = PROJECT_ROOT / "data" / "work_researcher.db"
    log_level: str = "INFO"

    # Named candidate profile.  ``None`` is the fully backwards-compatible
    # legacy mode used by configurations that do not define [profiles.*].
    profile_id: str | None = None
    profile_name: str = "Legacy/default candidate"
    profile_instructions: str = ""
    available_profiles: dict = field(default_factory=dict)

    default_location: str = "UK"
    default_radius_miles: int = 40
    default_max_days_old: int = 14
    default_work_from_home: bool = False
    default_limit_per_source: int = 25
    provider_timeout_s: int = 30
    exclude_training: bool = True
    daily_commute_miles: int = 25
    occasional_commute_miles: int = 50

    applicant: dict = field(default_factory=dict)
    browser: dict = field(default_factory=dict)
    auth: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    search_profiles: dict = field(default_factory=dict)
    blocklist_companies: list = field(default_factory=list)

    # -- derived helpers -------------------------------------------------
    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser_profile"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def provider_enabled(self, name: str) -> bool:
        return bool(self.providers.get(name, {}).get("enabled", False))

    def provider_cfg(self, name: str) -> dict:
        return self.providers.get(name, {})

    def secret(self, provider: str, key: str) -> str:
        """Secret resolution: env <PROVIDER>_<KEY> wins over config.toml."""
        env_name = f"{provider}_{key}".upper()
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
        return str(self.providers.get(provider, {}).get(key, "") or "").strip()

    def activate_from(self, other: Settings) -> None:
        """Replace this instance in-place so MCP tool closures stay valid."""
        for item in fields(self):
            setattr(self, item.name, getattr(other, item.name))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CANDIDATE_SECTIONS = ("applicant", "auth", "blocklist")


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Invalid TOML in {path}: {exc}") from exc


def _profile_instructions(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _profile_match_key(value: str) -> str:
    """Normalize an id/display name for natural-language profile selection."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _resolve_profile_id(profiles: dict, requested: str) -> str:
    """Accept a canonical id or a unique human-readable display name."""
    requested = requested.strip()
    if requested in profiles:
        return requested
    key = _profile_match_key(requested)
    matches = []
    for profile_id, profile in profiles.items():
        display_name = str(profile.get("display_name") or profile_id)
        if key in {_profile_match_key(profile_id), _profile_match_key(display_name)}:
            matches.append(profile_id)
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(
        f"{profile_id} ({profile.get('display_name') or profile_id})"
        for profile_id, profile in profiles.items()
    )
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous candidate profile {requested!r}; use an exact id. Available: {available}"
        )
    raise RuntimeError(f"Unknown candidate profile {requested!r}; available: {available}")


def _select_profile(raw: dict, explicit: str | None) -> tuple[str | None, dict]:
    profiles = raw.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise RuntimeError("[profiles] must be a TOML table")
    if not profiles:
        if explicit or os.environ.get("WORK_RESEARCHER_PROFILE", "").strip():
            raise RuntimeError("No named profiles are configured in config.toml")
        return None, profiles

    requested = (
        explicit
        or os.environ.get("WORK_RESEARCHER_PROFILE", "").strip()
        or str(raw.get("general", {}).get("active_profile", "")).strip()
    )
    if not requested:
        if len(profiles) == 1:
            requested = next(iter(profiles))
        else:
            raise RuntimeError(
                "Multiple candidate profiles are configured but [general].active_profile is empty"
            )
    selected = _resolve_profile_id(profiles, requested)
    if not _PROFILE_ID_RE.fullmatch(selected):
        raise RuntimeError(f"Invalid profile id {selected!r}; use letters, numbers, '_' or '-'")
    if not isinstance(profiles[selected], dict):
        raise RuntimeError(f"[profiles.{selected}] must be a TOML table")
    return selected, profiles


def _profile_merged_config(raw: dict, profile_id: str | None, profiles: dict) -> dict:
    """Build one effective config while keeping legacy candidate data isolated.

    Top-level applicant/auth/blocklist settings are used unchanged in
    legacy mode.  A named profile only receives them when it explicitly says
    ``inherit_legacy = true``; this is how the existing account stays exactly
    where it is while every new account starts isolated.
    """
    shared_raw = {k: v for k, v in raw.items() if k != "profiles"}
    merged = _merge(DEFAULTS, shared_raw)
    if profile_id is None:
        return merged

    profile = profiles[profile_id]
    inherit_legacy = bool(profile.get("inherit_legacy", False))

    # Personal sections must never silently leak from the existing account
    # into a newly-created candidate profile.
    for section in _CANDIDATE_SECTIONS:
        base = DEFAULTS.get(section, {})
        if inherit_legacy:
            base = _merge(base, raw.get(section, {}))
        merged[section] = _merge(base, profile.get(section, {}))

    # Search/browser/provider overrides are useful per candidate, while their
    # top-level values remain shared defaults.
    for section in ("search", "browser", "providers"):
        merged[section] = _merge(merged.get(section, {}), profile.get(section, {}))
    if not inherit_legacy:
        # Saved job searches are candidate-specific. A new candidate must not
        # inherit the legacy candidate's role/domain shortcuts.
        merged["search"]["profiles"] = dict(
            profile.get("search", {}).get("profiles", {})
        )

    profile_general = dict(profile.get("general", {}))
    profile_general.update({key: profile[key] for key in ("data_dir", "cv_dir") if key in profile})
    if not inherit_legacy:
        profile_general.setdefault("data_dir", f"profiles/{profile_id}/data")
        profile_general.setdefault("cv_dir", f"profiles/{profile_id}/CV_collection")
    merged["general"] = _merge(merged["general"], profile_general)

    return merged


def _profile_summaries(raw: dict, profiles: dict, project_root: Path) -> dict:
    summaries = {}
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        inherit = bool(profile.get("inherit_legacy", False))
        data_dir = profile.get(
            "data_dir",
            raw.get("general", {}).get("data_dir", "data")
            if inherit
            else f"profiles/{profile_id}/data",
        )
        cv_dir = profile.get(
            "cv_dir",
            raw.get("general", {}).get("cv_dir", "CV_collection")
            if inherit
            else f"profiles/{profile_id}/CV_collection",
        )
        applicant = (raw.get("applicant", {}) if inherit else {}) | profile.get("applicant", {})
        summaries[profile_id] = {
            "display_name": str(profile.get("display_name") or profile_id),
            "configured": bool(applicant.get("full_name")),
            "inherits_existing_account": inherit,
            "cv_dir": str(project_root / str(cv_dir)),
            "data_dir": str(project_root / str(data_dir)),
            "has_instructions": bool(_profile_instructions(profile.get("instructions"))),
        }
    return summaries


def load_settings(config_path: Path | None = None, profile: str | None = None) -> Settings:
    path = config_path or CONFIG_PATH
    raw = _read_config(path)
    profile_id, profiles = _select_profile(raw, profile)
    merged = _profile_merged_config(raw, profile_id, profiles)

    general = merged["general"]
    search = merged["search"]
    s = Settings(
        project_root=PROJECT_ROOT,
        config_path=path,
        data_dir=PROJECT_ROOT / general["data_dir"],
        cv_dir=PROJECT_ROOT / general["cv_dir"],
        log_level=general["log_level"],
        profile_id=profile_id,
        profile_name=(
            str(profiles[profile_id].get("display_name") or profile_id)
            if profile_id
            else "Legacy/default candidate"
        ),
        profile_instructions=(
            _profile_instructions(profiles[profile_id].get("instructions")) if profile_id else ""
        ),
        available_profiles=_profile_summaries(raw, profiles, PROJECT_ROOT),
        default_location=search["default_location"],
        default_radius_miles=search["default_radius_miles"],
        default_max_days_old=search["default_max_days_old"],
        default_work_from_home=search["default_work_from_home"],
        default_limit_per_source=search["default_limit_per_source"],
        provider_timeout_s=search["provider_timeout_s"],
        exclude_training=search.get("exclude_training_offers", True),
        daily_commute_miles=int(
            search.get("daily_commute_miles", merged["applicant"].get("daily_commute_miles", 25))
        ),
        occasional_commute_miles=int(
            search.get(
                "occasional_commute_miles", merged["applicant"].get("occasional_commute_miles", 50)
            )
        ),
        applicant=merged["applicant"],
        browser=merged["browser"],
        auth=merged["auth"],
        providers=merged["providers"],
        search_profiles=merged.get("search", {}).get("profiles", {}),
        blocklist_companies=list(merged.get("blocklist", {}).get("companies", [])),
    )
    s.db_path = s.data_dir / "work_researcher.db"
    return s


def set_active_profile(config_path: Path, profile: str) -> Settings:
    """Persist [general].active_profile without rewriting personal config.

    The replacement is intentionally surgical: comments, ordering and secrets
    remain byte-for-byte unchanged outside the one selector line.
    """
    selected = load_settings(config_path, profile=profile)  # validate first
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    general_start = next(
        (i for i, line in enumerate(lines) if line.strip().lower() == "[general]"), None
    )
    newline = "\r\n" if "\r\n" in text else "\n"
    assert selected.profile_id is not None
    selector = f'active_profile = "{selected.profile_id}"{newline}'
    if general_start is None:
        prefix = "" if not text or text.endswith(("\n", "\r")) else newline
        lines.extend([prefix, f"[general]{newline}", selector])
    else:
        section_end = next(
            (i for i in range(general_start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        active_line = next(
            (
                i
                for i in range(general_start + 1, section_end)
                if re.match(r"\s*active_profile\s*=", lines[i])
            ),
            None,
        )
        if active_line is None:
            lines.insert(general_start + 1, selector)
        else:
            lines[active_line] = selector

    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text("".join(lines), encoding="utf-8", newline="")
    # Validate the generated TOML before replacing the live configuration.
    _read_config(temporary)
    temporary.replace(config_path)
    return selected


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cv_dir.mkdir(parents=True, exist_ok=True)
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
