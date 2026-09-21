# Setup Guide

Search works out of the box (Totaljobs, Reed HTML, Earthworks — no keys
needed). The steps below unlock the rest.

## 1. Applicant profile (2 minutes)

Edit `config.toml` → `[applicant]`: `full_name`, `email`, `phone`, and the
wizard answers boards ask during applications (`date_of_birth`,
`nationality`, `right_to_work`, `age_group`, `gender`, `earliest_start_date`
…). These are injected into every apply plan so screening questions answer
automatically. Location intelligence (`home_location`, `daily_commute_miles`,
`occasional_commute_miles`, `willing_to_relocate`, `relocate_areas`) also
lives here.

### Multiple candidates

The supplied configuration keeps the first candidate in the original layout:

```toml
[general]
active_profile = "primary"

[profiles.primary]
display_name = "Primary candidate"
inherit_legacy = true
data_dir = "data"
cv_dir = "CV_collection"
```

This is deliberately non-migrating: the current CV index, application history
and browser logins continue to work as before. Add another person
with isolated nested settings:

```toml
[profiles.partner]
display_name = "Partner"
instructions = "Search goals and candidate-specific application guidance."

[profiles.partner.applicant]
full_name = "..."
email = "..."
phone = "..."
home_location = "..."

[profiles.partner.auth]
google_account = "..."
auto_google_signin = true
```

Unless overridden, this creates `profiles/partner/CV_collection/`,
`profiles/partner/data/work_researcher.db`, and browser logins under
`profiles/partner/data/browser_profile/`. List or switch candidates with:

```text
work-researcher profiles
work-researcher use-profile partner
```

The selector accepts either the profile id or the full `display_name`, without
case sensitivity (for example `work-researcher use-profile "Irena Lobodzinska"`).

An MCP agent uses `manage_profiles(action="list")` and
`manage_profiles(action="switch", profile="partner")`; the latter closes the
old browser context before changing identity. `WORK_RESEARCHER_PROFILE` is an
optional per-process override and takes precedence at startup.

For concurrent tasks, start each MCP process with
`work-researcher serve --profile primary` / `--profile partner` (or set the
environment override per process). Each process then stays pinned to its own
candidate even if another task changes the default profile in `config.toml`.

## 2. Free API keys (5 minutes, optional but recommended)

| Provider | Where to get | Where to put |
|---|---|---|
| Adzuna | https://developer.adzuna.com (sign up → Application) | `[providers.adzuna]` `app_id`/`app_key` or env `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` |
| Reed | https://www.reed.co.uk/developers (free partner key) | `[providers.reed]` `api_key` or env `REED_API_KEY` |
| Jooble | https://jooble.org/api/about (request a key) | `[providers.jooble]` `api_key` or env `JOOBLE_API_KEY` |

Without keys the server still searches Totaljobs + Reed(HTML) + Earthworks
and tells you which providers are missing credentials in `get_status`.

## 3. Local CV folders

CV storage is deliberately manual. Run `work-researcher profiles` or call
`manage_profiles(action="list")` to see the exact `cv_dir` for every candidate.
Copy `.docx`, `.pdf`, or `.doc` files into that directory, then run:

```text
work-researcher index-cvs
```

An MCP agent uses `sync_cvs()` after you add, replace or remove files. There is
no Google Drive integration and no CV OAuth token.

Default locations:

- Existing `primary`/`andre` profile: `CV_collection/`
- New `partner` profile: `profiles/partner/CV_collection/`
- Any other profile: `profiles/<profile-id>/CV_collection/`

## 4. Board logins for applications

The embedded browser keeps a persistent login profile (`data/browser_profile`,
real Edge by default). `browser_login(url)` walks "Sign in with Google" and
picks the pre-approved account from `[auth].google_account` WITHOUT asking;
on 2FA/captcha it stops and asks you to finish in the visible window. Boards
without Google SSO (e.g. CV-Library, GOV.UK One Login) need a one-time
manual sign-in in that window — the profile persists afterwards.

## 5. Verify

```
uv run work-researcher doctor     # config / DB / providers / local CV folder
uv run work-researcher selftest   # in-process smoke test
```

## Where things live

- `config.toml` — all settings (see the annotated `config.example.toml`)
- `data/work_researcher.db` — jobs, searches, applications, CV index, blocklist
- `CV_collection/` — manually managed CV files for the existing default profile
- `profiles/<id>/CV_collection/` — manually managed CV files for new profiles
- `data/browser_profile/` — persistent browser logins
- `data/screenshots/` — application evidence
