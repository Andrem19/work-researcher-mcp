# UK Job Boards — Coverage Map

Support tiers used by Work Researcher MCP:

- **built-in** — searched by fast HTTP/API automatically
- **api-key** — built-in once the free key is added (SETUP.md)
- **browser** — blocked for plain HTTP: the harness agent searches them in a
  browser and feeds findings back via `submit_job_observations`
- **future** — not wired yet, realistic provider candidates

| Board | Tier | Notes |
|---|---|---|
| [Totaljobs](https://www.totaljobs.com) (+ Jobsite) | built-in | StepStone group; HTML search parses cleanly |
| [Reed](https://www.reed.co.uk) | built-in (+api-key) | HTML fallback works without a key; API is cleaner |
| [Earthworks-jobs](https://www.earthworks-jobs.com) | built-in | geoscience/environment niche — Field Geologist sweet spot; JSON-LD listings |
| [Adzuna](https://www.adzuna.co.uk) | api-key | aggregator; adds many boards at once |
| [Jooble](https://www.jooble.org) | api-key | aggregator |
| [Indeed UK](https://uk.indeed.com) | browser | biggest volume; Cloudflare blocks non-browsers; Easy Apply flow in the playbook |
| [CV-Library](https://www.cv-library.co.uk) | browser | Akamai blocks plain HTTP |
| [LinkedIn Jobs](https://www.linkedin.com/jobs) | browser | ToS restrict automation — surface to user, slow pace |
| [Glassdoor](https://www.glassdoor.co.uk) | browser | same as CV-Library |
| [Jobserve](https://www.jobserve.com) | browser | results page is a JS shell over a session search |
| [FindAJob / GOV.UK Work Hub](https://www.jobs.service.gov.uk/jobs) | browser | GOV.UK's job search (replaces the old findajob.dwp.gov.uk). JS SPA — 618+ results for "data analyst". Filters: REMOTE/ONSITE/HYBRID/FIELD_BASED (maps to our work_mode). Sign in via gov.uk account (email/password or GOV.UK One Login, NOT Google SSO). |
| [Monster UK](https://www.monster.co.uk) | — | UK operations closed (2024) |
| [Otta / Welcome to the Jungle](https://uk.welcometothejungle.com) | future | tech/graduate roles |
| [CWJobs / TechnoJobs](https://www.cwjobs.co.uk) | future | IT boards, HTML like Totaljobs |
| [jobs.ac.uk](https://www.jobs.ac.uk) | future | academia — research geology roles; plain HTML |
| [Rigzone](https://www.rigzone.com) / [Oilandgasjobsearch](https://www.oilandgasjobsearch.com) | future | energy/geoscience niches |
| [Civil Service Jobs](https://www.jobsearch.civilservicejobs.service.gov.uk) | browser | own account (sign in only through the vacancy's "Apply now" — one-shot link). Applications are graded on WRITTEN STATEMENTS band by band, not on a CV upload; DWP/Government Recruitment Service ads reached via Work Hub redirect here. The apply plan carries `assessment_protocol` + `statement_plan`, and every text must pass `check_statement` before pasting. |
| [Escape the City](https://escapethecity.org) | future | career-change niche |
| [eFinancialCareers](https://www.efinancialcareers.co.uk) | future | finance analytics roles |

Adding a board: create `src/work_researcher/providers/<name>.py` with an async
`fetch(query, cfg) -> list[JobCard]`, register it in `providers/__init__.py`
`REGISTRY`, add a `[providers.<name>]` config block. Everything else (dedup,
ranking, location intelligence, blocklist, apply planning) applies automatically.
