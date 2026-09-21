"""Command-line interface.

Commands: serve, doctor, search, index-cvs, profiles, use-profile, selftest.
Logs never go to stdout in serve mode (stdout is the MCP protocol stream).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


async def cmd_serve(args) -> int:
    from .config import load_settings
    from .server import run_stdio

    if args.transport != "stdio":
        print(f"Unsupported transport: {args.transport}", file=sys.stderr)
        return 2
    await run_stdio(load_settings(profile=args.profile))
    return 0


async def cmd_doctor(args) -> int:
    from . import persistence as db
    from .config import ensure_dirs, load_settings

    settings = load_settings()
    ensure_dirs(settings)
    await db.init_db(settings.db_path)
    report: dict = {
        "active_profile": settings.profile_id,
        "profile_name": settings.profile_name,
        "database": str(settings.db_path),
        "cv_dir": str(settings.cv_dir),
    }
    async with db.connect(settings.db_path) as conn:
        report["jobs"] = await db.count_rows(conn, "jobs")
        report["applications"] = await db.count_rows(conn, "applications")
        report["cvs"] = await db.count_rows(conn, "cvs")
    report["cv_storage"] = {
        "mode": "local_manual",
        "directory": str(settings.cv_dir),
        "instruction": "Copy CV files here and run work-researcher index-cvs",
    }
    for name in ("totaljobs", "reed", "adzuna", "jooble", "earthworks", "findajob"):
        report[f"provider.{name}"] = {
            "enabled": settings.provider_enabled(name),
            **({"key": bool(settings.secret(name, "api_key"))}
               if name in ("reed", "jooble") else {}),
            **({"app_id": bool(settings.secret(name, "app_id")),
                "app_key": bool(settings.secret(name, "app_key"))}
               if name == "adzuna" else {}),
        }
    try:
        import playwright  # noqa: F401

        report["playwright"] = "installed"
    except ImportError:
        report["playwright"] = "MISSING"
    _print(report)
    return 0


async def cmd_search(args) -> int:
    from .config import ensure_dirs, load_settings
    from .server import create_server

    settings = load_settings()
    ensure_dirs(settings)
    _, _ = create_server(settings)
    from .domain import SearchParams
    from .providers import run_search
    from . import dedup as dedup_mod
    from . import persistence as db
    from .ranking import score_job

    params = SearchParams(
        query=args.query, location=args.location or settings.default_location,
        max_days_old=args.max_days_old or settings.default_max_days_old,
        limit_per_source=args.limit or settings.default_limit_per_source,
    )
    cards_by_provider, reports = await run_search(settings, params.model_dump())
    all_cards = [c for cs in cards_by_provider.values() for c in cs]
    async with db.connect(settings.db_path) as conn:
        pool = await dedup_mod.load_pool(conn)
        resolution, merged = dedup_mod.resolution_map(all_cards, pool)
        await db.upsert_jobs(conn, all_cards, resolution)
        await conn.commit()
    scored = []
    for c in all_cards:
        s, _ = score_job(c, params.query)
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    out = {
        "reports": [r.model_dump() for r in reports],
        "cards": len(all_cards),
        "duplicates_merged": merged,
        "top": [
            {"score": s, "title": c.title, "company": c.company,
             "location": c.location_text, "salary": c.salary_raw,
             "posted": c.posted_at.isoformat() if c.posted_at else None,
             "url": c.url}
            for s, c in scored[: args.top]
        ],
    }
    _print(out)
    return 0


async def cmd_index_cvs(args) -> int:
    from .config import ensure_dirs, load_settings
    from .cvmanager import index_cvs

    settings = load_settings()
    ensure_dirs(settings)
    _print(await index_cvs(settings, force=args.force))
    return 0


async def cmd_selftest(args) -> int:
    """In-process smoke test of every tool layer (no MCP client needed)."""
    from .config import ensure_dirs, load_settings

    settings = load_settings()
    ensure_dirs(settings)
    results: dict[str, str] = {}

    from . import persistence as db

    await db.init_db(settings.db_path)
    results["db_init"] = "ok"

    # tool registration surface
    from .server import create_server

    mcp, _ = create_server(settings)
    names = [t.name for t in getattr(mcp, "list_tools_sync", lambda: [])()]
    if not names:
        try:
            tools = await mcp.list_tools()
            names = [t.name for t in tools]
        except Exception:
            names = []
    results["tools_registered"] = f"{len(names)}" if names else "UNKNOWN(list api)"
    results["tool_names_sample"] = ",".join(sorted(names)[:10])

    # dedup sanity
    from .dedup import resolution_map
    from .domain import JobCard

    cards = [
        JobCard(source="reed", title="Data Analyst", company="Acme Ltd",
                location_text="London", salary_min=40000),
        JobCard(source="totaljobs", title="Data Analyst (SQL, Power BI)",
                company="Acme Limited", location_text="London, Greater London",
                salary_min=42000),
        JobCard(source="adzuna", title="Field Geologist", company="GeoCo",
                location_text="Aberdeen", salary_min=35000),
    ]
    res, merged = resolution_map(cards, [])
    results["dedup_merged_expected_1"] = str(merged)
    results["dedup_ok"] = "ok" if merged == 1 else "FAIL"

    async with db.connect(settings.db_path) as conn:
        hashmap = await db.upsert_jobs(conn, cards, res)
        await conn.commit()
        distinct = {v[0] for v in hashmap.values()}
        results["dedup_rows_expected_2"] = str(len(distinct))
        results["dedup_rows_ok"] = "ok" if len(distinct) == 2 else "FAIL"
        # application guard
        job_id = sorted(distinct)[0]
        from . import tracker as tr

        plan1 = await tr.start_application(conn, settings, job_id)
        plan2 = await tr.start_application(conn, settings, job_id)
        results["apply_guard"] = "ok" if (plan1.get("ok") and plan2.get("already_exists")) else "FAIL"
        await conn.commit()

    # provider imports
    from .providers import provider_modules

    results["providers_importable"] = ",".join(sorted(provider_modules(settings))) or "none"

    # cv manager (no files → zero is fine)
    from .cvmanager import index_cvs

    idx = await index_cvs(settings, prune_missing=False)
    results["cv_index"] = f"found={idx['files_found']} indexed={idx['indexed']}"

    # browser import only (no window in selftest)
    try:
        from .browser import BrowserSession  # noqa: F401

        results["browser_import"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["browser_import"] = f"FAIL {exc}"

    _print(results)
    failures = [k for k, v in results.items() if isinstance(v, str) and v.startswith("FAIL")]
    if failures:
        print(f"SELFTEST FAILURES: {failures}", file=sys.stderr)
        return 1
    print("SELFTEST OK")
    return 0


async def cmd_profiles(args) -> int:
    from .config import load_settings

    settings = load_settings()
    _print({
        "active_profile": settings.profile_id,
        "active_display_name": settings.profile_name,
        "active_instructions": settings.profile_instructions or None,
        "profiles": settings.available_profiles,
    })
    return 0


async def cmd_use_profile(args) -> int:
    from .config import CONFIG_PATH, set_active_profile

    try:
        settings = set_active_profile(
            Path(args.config) if args.config else CONFIG_PATH,
            args.profile,
        )
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print({
        "ok": True,
        "active_profile": settings.profile_id,
        "display_name": settings.profile_name,
        "note": "restart any already-running MCP process; manage_profiles switches it live",
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="work-researcher",
                                description="UK job search & application MCP")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="Run the MCP server")
    sp.add_argument("--transport", default="stdio", choices=["stdio"])
    sp.add_argument("--profile", default=None, help="Candidate profile for this process")
    sp.set_defaults(func=cmd_serve)

    sub.add_parser("doctor", help="Config/DB/provider report").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("search", help="One-off CLI search (testing)")
    sp.add_argument("query")
    sp.add_argument("--location", default=None)
    sp.add_argument("--max-days-old", type=int, default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--top", type=int, default=15)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("index-cvs", help="Scan the active profile's CV directory")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_index_cvs)

    sub.add_parser("selftest", help="In-process smoke test").set_defaults(func=cmd_selftest)
    sub.add_parser("profiles", help="List candidate profiles and the active one") \
        .set_defaults(func=cmd_profiles)

    sp = sub.add_parser("use-profile", help="Persist the default candidate profile")
    sp.add_argument("profile")
    sp.add_argument("--config", default=None, help="Alternative config.toml path")
    sp.set_defaults(func=cmd_use_profile)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = asyncio.run(args.func(args))
    except KeyboardInterrupt:
        code = 1
    raise SystemExit(code)
