import json
import os
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from work_researcher.config import Settings, ensure_dirs, load_settings, set_active_profile
from work_researcher.cvmanager import index_cvs
from work_researcher.persistence import connect, init_db, upsert_cv
from work_researcher.server import create_server

LEGACY_CANDIDATE = """
[general]
cv_dir = "CV_collection"
data_dir = "data"

[applicant]
full_name = "Existing Candidate"
email = "existing@example.test"
home_location = "Existing Town"

[auth]
google_account = "existing-login@example.test"

[blocklist]
companies = ["Existing Blocked Company"]
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_legacy_config_keeps_exact_existing_paths_and_identity() -> None:
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORK_RESEARCHER_PROFILE": ""}):
        path = Path(directory) / "config.toml"
        _write(path, LEGACY_CANDIDATE)

        settings = load_settings(path)

        assert settings.profile_id is None
        assert settings.data_dir.name == "data"
        assert settings.cv_dir.name == "CV_collection"
        assert settings.applicant["full_name"] == "Existing Candidate"
        assert settings.auth["google_account"] == "existing-login@example.test"


def test_default_named_profile_can_inherit_existing_account_without_moving_it() -> None:
    config = (
        LEGACY_CANDIDATE.replace("[general]", '[general]\nactive_profile = "primary"')
        + """

[profiles.primary]
display_name = "Existing default"
inherit_legacy = true
data_dir = "data"
cv_dir = "CV_collection"
instructions = "Use the existing candidate only."
"""
    )
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORK_RESEARCHER_PROFILE": ""}):
        path = Path(directory) / "config.toml"
        _write(path, config)

        settings = load_settings(path)

        assert settings.profile_id == "primary"
        assert settings.data_dir.name == "data"
        assert settings.cv_dir.name == "CV_collection"
        assert settings.applicant["full_name"] == "Existing Candidate"
        assert settings.auth["google_account"] == "existing-login@example.test"
        assert settings.profile_instructions == "Use the existing candidate only."


def test_new_profile_isolates_all_candidate_state_and_does_not_inherit_identity() -> None:
    config = (
        LEGACY_CANDIDATE
        + """

[profiles.primary]
inherit_legacy = true
data_dir = "data"
cv_dir = "CV_collection"

[profiles.partner]
display_name = "Partner"

[profiles.partner.applicant]
full_name = "Other Candidate"
home_location = "Other Town"

[profiles.partner.auth]
google_account = "other-login@example.test"
"""
    )
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORK_RESEARCHER_PROFILE": ""}):
        path = Path(directory) / "config.toml"
        _write(path, config)

        settings = load_settings(path, profile="partner")

        assert settings.profile_id == "partner"
        assert settings.applicant["full_name"] == "Other Candidate"
        assert settings.applicant["email"] == ""
        assert settings.auth["google_account"] == "other-login@example.test"
        assert str(settings.data_dir).endswith("profiles\\partner\\data")
        assert str(settings.cv_dir).endswith("profiles\\partner\\CV_collection")
        assert settings.blocklist_companies == []


def test_switch_persists_only_selector_and_preserves_rest_of_toml() -> None:
    config = """# keep this comment
[general]
active_profile = "primary"
log_level = "INFO"

[providers.adzuna]
app_key = "do-not-rewrite-this-value"

[profiles.primary]
inherit_legacy = true

[profiles.partner]
display_name = "Partner"
"""
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORK_RESEARCHER_PROFILE": ""}):
        path = Path(directory) / "config.toml"
        _write(path, config)

        selected = set_active_profile(path, "partner")
        updated = path.read_text(encoding="utf-8")

        assert selected.profile_id == "partner"
        assert "# keep this comment" in updated
        assert 'app_key = "do-not-rewrite-this-value"' in updated
        assert updated.count('active_profile = "partner"') == 1
        assert tomllib.loads(updated)["general"]["active_profile"] == "partner"


def test_unknown_profile_fails_without_touching_config() -> None:
    config = """[general]
active_profile = "primary"

[profiles.primary]
inherit_legacy = true
"""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        _write(path, config)

        with pytest.raises(RuntimeError, match="Unknown candidate profile"):
            set_active_profile(path, "missing")

        assert path.read_text(encoding="utf-8") == config


def test_profile_can_be_selected_by_display_name_and_persists_canonical_id() -> None:
    config = """[general]
active_profile = "andrew_remniow"

[profiles.andrew_remniow]
display_name = "Andrew Remniow"

[profiles.irena_lobodzinska]
display_name = "Irena Lobodzinska"
"""
    with TemporaryDirectory() as directory, patch.dict(
        os.environ, {"WORK_RESEARCHER_PROFILE": ""}
    ):
        path = Path(directory) / "config.toml"
        _write(path, config)

        selected = set_active_profile(path, "irena lobodzinska")

        assert selected.profile_id == "irena_lobodzinska"
        assert selected.profile_name == "Irena Lobodzinska"
        assert tomllib.loads(path.read_text(encoding="utf-8"))["general"][
            "active_profile"
        ] == "irena_lobodzinska"


@pytest.mark.asyncio
async def test_mcp_switch_is_live_persistent_and_uses_a_different_database() -> None:
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"WORK_RESEARCHER_PROFILE": ""}):
        root = Path(directory)
        primary_data = (root / "primary-data").as_posix()
        primary_cvs = (root / "primary-cvs").as_posix()
        partner_data = (root / "partner-data").as_posix()
        partner_cvs = (root / "partner-cvs").as_posix()
        path = root / "config.toml"
        _write(
            path,
            f'''[general]
active_profile = "primary"

[profiles.primary]
display_name = "Primary"
data_dir = "{primary_data}"
cv_dir = "{primary_cvs}"

[profiles.primary.applicant]
full_name = "Primary Candidate"

[profiles.partner]
display_name = "Partner"
instructions = "Partner-specific instructions"
data_dir = "{partner_data}"
cv_dir = "{partner_cvs}"

[profiles.partner.applicant]
full_name = "Partner Candidate"
''',
        )
        settings = load_settings(path)
        old_db = settings.db_path
        mcp, _ = create_server(settings)

        result = await mcp.call_tool("manage_profiles", {"action": "switch", "profile": "partner"})
        payload = json.loads(result.content[0].text)

        assert payload["ok"] is True
        assert settings.profile_id == "partner"
        assert settings.profile_instructions == "Partner-specific instructions"
        assert settings.db_path != old_db
        assert settings.db_path.exists()
        assert (
            tomllib.loads(path.read_text(encoding="utf-8"))["general"]["active_profile"]
            == "partner"
        )

        tools = {tool.name for tool in await mcp.list_tools()}
        assert "sync_cvs" in tools
        assert "push_cv_to_drive" not in tools

        sync_result = await mcp.call_tool("sync_cvs", {})
        sync_payload = json.loads(sync_result.content[0].text)
        assert sync_payload["storage"] == "local_manual"
        assert sync_payload["cv_dir"] == str(Path(partner_cvs))


@pytest.mark.asyncio
async def test_new_cv_database_has_no_drive_metadata_columns() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "local-cv.db"
        await init_db(db_path)
        async with connect(db_path) as conn:
            cursor = await conn.execute("PRAGMA table_info(cvs)")
            columns = {row["name"] for row in await cursor.fetchall()}

        assert "drive_file_id" not in columns
        assert "drive_modified" not in columns


@pytest.mark.asyncio
async def test_local_cv_folder_is_source_of_truth_and_prunes_missing_files() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        settings = Settings(
            project_root=root,
            data_dir=root / "data",
            cv_dir=root / "CV_collection",
            db_path=root / "data" / "work_researcher.db",
        )
        ensure_dirs(settings)
        (settings.cv_dir / "current.doc").write_bytes(b"legacy-doc-placeholder")
        await index_cvs(settings)
        async with connect(settings.db_path) as conn:
            await upsert_cv(
                conn,
                {
                    "filename": "missing.txt",
                    "path": str(settings.cv_dir / "missing.txt"),
                    "full_text": "stale CV",
                },
            )

        result = await index_cvs(settings)
        async with connect(settings.db_path) as conn:
            cursor = await conn.execute("SELECT filename FROM cvs ORDER BY filename")
            filenames = [row["filename"] for row in await cursor.fetchall()]

        assert result["removed_missing"] == 1
        assert filenames == ["current.doc"]
