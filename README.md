# Work Researcher MCP

A local [Model Context Protocol](https://modelcontextprotocol.io/) server for
**UK job search, CV management and end-to-end job applications**. Built for
agent workflows: ask "find junior Data Analyst vacancies near me", the agent
searches multiple job boards in parallel, deduplicates and ranks the results,
checks them against your CVs — then submits real applications through an
embedded browser with persistent logins.

## What it does

```
you: "find trainee/junior jobs within 25 miles, or remote"
      │
      ▼
search_jobs ──► Totaljobs, Reed, Adzuna, Earthworks (HTTP/API)
      │         + Indeed/CV-Library/GOV.UK via the agent's browser
      ▼
dedup + rank + filter:
  ✂ cross-board duplicates merged
  ✂ paid training/course ads excluded
  ✂ out-of-commute on-site jobs dropped (work-mode aware)
  ✂ hard-requirement gaps flagged against your CVs
  ✂ blocked agencies/agencies hidden
      │
      ▼
you pick ──► start_application ──► browser fills the real form
             (CV upload, cover letter, screening questions, wizards)
      │
      ▼
record_application ──► SQLite memory: never apply to the same job twice
```

## Features

- **Multi-board search** — Totaljobs, Reed, Earthworks over plain HTTP;
  Adzuna and Jooble via free API keys; browser-only boards (Indeed,
  CV-Library, LinkedIn, GOV.UK Work Hub) feed into the same store through
  `submit_job_observations`.
- **Cross-board deduplication** — the same vacancy posted on several boards
  merges into one canonical job (exact hash + fuzzy title/company matching).
- **Paid-training-ad guard** — course marketing dressed up as "trainee"
  jobs (fee language, known course-mill providers, bait salary ranges) is
  excluded automatically; real paid apprenticeships stay.
- **Location intelligence** — home base + work-mode-aware commute limits
  (daily office ≤ configurable miles; hybrid/field ≤ a larger radius;
  remote = UK-wide). Distances via postcodes.io with caching.
- **Requirements matching** — hard requirements (qualifications, licences,
  experience years) parsed from descriptions and checked against your CV
  text; jobs with unmet hard requirements are flagged or dropped.
- **Agency vs employer** — every listing is tagged `posted_by: agency |
  employer` so you always see who is hiring.
- **Application memory** — SQLite-backed history; searches mark
  `already_applied`; `start_application` refuses duplicates; `check_applied`
  matches by URL or fuzzy title+company across boards.
- **CV management** — local CV folder + two-way Google Drive sync. CVs are
  parsed, domain-tagged and matched to vacancies; edits flow back to Drive.
- **Agent-optimized browser** — persistent login profile (real Edge by
  default), Google SSO walkthrough, every action returns a fresh snapshot,
  application forms with human-readable field labels, apply-wizard isolation
  (`browser_snapshot(modal_only=true)`), direct file setting on hidden
  inputs, cover-letter generation above board file-size minimums.
- **Adaptive response sizing** — tools accept the calling model's
  `context_window` (or an explicit `response_profile`), so a small local
  model gets compact resumable pages while a large model can take wider
  pages. Full result sets always live in SQLite.

## Tool surface (grouped, ~30 tools)

| Group | Tools |
|---|---|
| search | `get_status`, `search_jobs`, `get_job`, `list_stored_jobs`, `fetch_job_description`, `submit_job_observations` |
| cv | `list_cvs`, `sync_cvs`, `push_cv_to_drive` |
| apply | `start_application`, `record_application`, `list_applications`, `check_applied`, `manage_blocklist`, `make_cover_letter` |
| browser | `browser_login`, `browser_open`, `browser_snapshot`, `browser_form`, `browser_click`, `browser_set`, `browser_type`, `browser_upload`, `browser_press`, `browser_wait`, `browser_screenshot`, `browser_eval`, `browser_tabs`, `browser_close` |

## Quick start

```bash
uv sync
uv run playwright install chromium      # bundled fallback (Edge used if present)
uv run work-researcher doctor           # config / DB / provider report
uv run work-researcher selftest         # in-process smoke test
uv run work-researcher serve --transport stdio
```

1. Copy `config.example.toml` → `config.toml` and fill in your profile:
   name, location, commute preferences, optional wizard answers
   (right-to-work, date of birth, etc. — boards ask these on apply).
2. Free API keys (optional but recommended): Adzuna, Reed, Jooble —
   see `SETUP.md`.
3. Google Drive CV sync (optional): one-time OAuth setup in `SETUP.md`.
4. Connect to your MCP host with the stdio command:

```
uv run --directory /path/to/work-researcher-mcp work-researcher serve --transport stdio
```

Board coverage and tiers: `JOB_SITES.md`. Full setup guide: `SETUP.md`.

## Intended agent workflow

1. `search_jobs(query="…", context_window=<your model's context>)` — or a
   saved `profile` from config.
2. Present the ranked list to the user (dedup merged, `posted_by`,
   `location_status`, `requirements_status`, short descriptions).
3. User picks vacancies.
4. `start_application(job_id)` per pick → apply plan: URL, method, site
   playbook, best-matching CV, applicant profile, cautions.
5. `browser_login(url)` if the board needs auth (one-time; the profile
   persists).
6. Drive the form: `browser_form` / `browser_snapshot(modal_only=true)` +
   `browser_set` + `browser_upload` (cover letters via `make_cover_letter`).
7. `browser_screenshot` the confirmation →
   `record_application(status="submitted", evidence={…})`.

## Configuration

All settings live in `config.toml` (annotated template in
`config.example.toml`): applicant profile, home location + commute limits,
search defaults and saved search profiles, board API keys, Google Drive CV
sync, browser preferences, pre-approved sign-in account, blocklist seeds.
Secrets can also come from environment variables.

## Architecture

```
src/work_researcher/
  server.py        MCP wiring + tools            browser.py    Playwright session (persistent profile)
  providers/       totaljobs reed adzuna jooble  tracker.py    apply plans + per-site playbooks
                   earthworks govuk_workhub       dedup.py      cross-board duplicate merge
  persistence.py   SQLite (jobs/searches/apps/    geo.py        geocoding + work-mode commute policy
                   cvs/blocklist/locations)       cvmanager.py  CV parse + domain tagging + matching
  drive.py         Google Drive read/write        requirements.py  hard-requirements extraction/matching
  ranking.py       relevance scoring              training.py   paid-course-ad detection
  seller.py        agency vs employer             config.py     config.toml + env
```

## License

MIT
