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
- **Local CV management** — every candidate has a clearly reported local CV
  folder. Copy files there manually; `sync_cvs` parses, tags and indexes them.
- **Agent-optimized browser** — persistent login profile (real Edge by
  default), Google SSO walkthrough, every action returns a fresh snapshot,
  application forms with human-readable field labels, apply-wizard isolation
  (`browser_snapshot(modal_only=true)`), direct file setting on hidden
  inputs, cover-letter generation above board file-size minimums.
- **Adaptive response sizing** — tools accept the calling model's
  `context_window` (or an explicit `response_profile`), so a small local
  model gets compact resumable pages while a large model can take wider
  pages. Full result sets always live in SQLite.
- **Isolated candidate profiles** — switch between people without mixing CVs,
  application history or persistent browser logins. The
  original single-account folders remain the default and require no migration.
- **Statement-assessed applications** — Civil Service Jobs / DWP-style
  vacancies are detected automatically; the apply plan then carries the
  mandatory assessment protocol (read the advert's "Selection process
  details" first, copy the essential criteria verbatim, fill the word budget,
  quantify the outcomes) and `check_statement` gates every text before it is
  pasted into the form. See below.

## Tool surface (grouped, 31 tools)

| Group | Tools |
|---|---|
| profile | `manage_profiles` (list/switch candidate; live and persistent) |
| search | `get_status`, `search_jobs`, `get_job`, `list_stored_jobs`, `fetch_job_description`, `submit_job_observations` |
| cv | `list_cvs`, `sync_cvs` (local files only) |
| apply | `start_application`, `record_application`, `list_applications`, `check_applied`, `manage_blocklist`, `make_cover_letter` |
| statements | `check_statement` (quality gate), `record_sift_feedback` (panel scores → fixes) |
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
3. Copy each candidate's CV files into the folder shown by
   `work-researcher profiles`, then run `work-researcher index-cvs`.
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
6. Complete the form: `browser_form` / `browser_snapshot(modal_only=true)` +
   `browser_set` + `browser_upload` (cover letters via `make_cover_letter`).
7. `browser_screenshot` the confirmation →
   `record_application(status="submitted", evidence={…})`.

## Statement-assessed applications (Civil Service / DWP)

Some employers do not read a CV file at all — they score written statements
band by band (Not demonstrated → Outstanding) against the advert's essential
criteria. Civil Service Jobs vacancies are detected automatically (CSJ hosts,
e-foms, DWP/Government Recruitment Service employers, or an advert that
mentions a personal/technical/supporting statement), and the apply plan then
carries the mandatory protocol:

1. **Read the advert first.** Open it and read "Selection process details" in
   full — it names which statement the initial sift uses (DWP sifts the
   technical statement first) and the pass mark. Never write from a search
   snippet.
2. **Copy the essential criteria verbatim** from the advert into your notes.
3. **Fill the budget:** ≥90% of each limit, one block per criterion, each
   with what you did, how, and the outcome in numbers.
4. **Technical statement = quantified STARs** covering design / build / test /
   document / integrate / operate, with a *different* example from the
   personal statement.
5. **Humanise:** run `mcp__sapling__aidetect` on every text we send —
   personal statement, technical statement, employment history, cover letter,
   free-text answers — and rewrite until it reports **0% AI** (Sapling needs
   500+ characters to be reliable).
6. **Gate every text** with `check_statement(text, kind, word_limit,
   criteria, other_text=…, ai_score=0, ai_score_required=True)` and fix every
   block and warning before pasting it into the form — a non-zero Sapling
   score blocks, and a statement-assessed text cannot reach `pass` until the
   detector has been run.
7. **Close the loop:** when the panel's scores arrive, `record_sift_feedback`
   stores them and returns the fixes for the next application.

`start_application` returns `assessment_protocol` + `statement_plan` for these
vacancies (with criteria extracted from the stored description when it is
complete); ordinary board applications are untouched — no protocol, no extra
steps.

The 2026-08-30 DWP "Data Engineer Level I" rejection is the regression this
guards: technical 4/7 (exactly the bar), personal statement 3/7 (below it),
employment history not assessed — 441 of 750 words used and no quantified
outcomes. The same texts now come back from `check_statement` as `revise`
(under budget, no quantification, reused example, filler phrasing), and Sapling
scored them 66.8% (personal statement) / 60.9% (technical statement) AI — the
detector step exists because those readings should never have gone out.

## Configuration

All settings live in `config.toml` (annotated template in
`config.example.toml`). `[general].active_profile` selects the startup
candidate. `manage_profiles(action="list")` shows available profiles and
`manage_profiles(action="switch", profile="partner")` changes the candidate
immediately and persists the choice. The equivalent CLI commands are
`work-researcher profiles` and `work-researcher use-profile partner`.
Both the MCP tool and CLI accept either a canonical id such as
`irena_lobodzinska` or its full display name, for example `"Irena Lobodzinska"`.

The existing account uses `inherit_legacy=true`, so its traditional top-level
applicant/auth settings and its `CV_collection/` + `data/` folders remain
untouched. New profiles omit `inherit_legacy`; by default they use
`profiles/<id>/CV_collection`, `profiles/<id>/data`, a separate SQLite history,
a separate CV index, and a separate persistent browser login directory.
Shared search/provider/browser defaults remain top-level. Secrets can also come
from environment variables.

Each profile may define nested `[profiles.<id>.applicant]`, `.auth`,
`.search`, `.browser`, `.providers`, and `.blocklist` tables plus a free-form
`instructions` string. Profile IDs are limited to letters, numbers, `_` and
`-`; use `display_name` for a human-readable name.

For simultaneous independent tasks, pin each MCP process explicitly with
`work-researcher serve --profile <id>` (or `WORK_RESEARCHER_PROFILE=<id>`).
This avoids dependence on the globally persisted default while still using the
same isolated per-profile folders.

## Architecture

```
src/work_researcher/
  server.py        MCP wiring + tools            browser.py    Playwright session (persistent profile)
  providers/       totaljobs reed adzuna jooble  tracker.py    apply plans + per-site playbooks
                   earthworks govuk_workhub       dedup.py      cross-board duplicate merge
  persistence.py   SQLite (jobs/searches/apps/    geo.py        geocoding + work-mode commute policy
                   cvs/blocklist/locations)       cvmanager.py  CV parse + domain tagging + matching
  requirements.py  hard-requirements matching    ranking.py     relevance scoring
  training.py      paid-course-ad detection
  seller.py        agency vs employer             config.py     config.toml + env
```

## License

MIT
