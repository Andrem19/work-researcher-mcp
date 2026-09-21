"""Application tracker: plans, guards against double applications, history."""

from __future__ import annotations

import aiosqlite

from . import persistence as db
from . import statement as statement_mod
from .config import Settings
from .domain import ApplyPlan

PLAYBOOKS: dict[str, str] = {
    "indeed": (
        "Indeed UK: log in once via browser_open('https://uk.indeed.com') + manual "
        "sign-in (profile persists). 'Easy Apply' = in-page modal (CV upload once, "
        "screening questions); other jobs redirect to the employer site — follow "
        "the redirect. Cap ~10 applications/day; pause and ask the user on "
        "captchas/verification."
    ),
    "reed": (
        "Reed: job page → 'Apply now' → an 'Application questions' WIZARD "
        "(modal, up to 19 questions for apprenticeships). USE "
        "browser_snapshot(modal_only=true) — it shows ONLY the active "
        "wizard's question + controls (hidden 'Session expired' templates "
        "excluded) — then browser_set/browser_click by number. NEVER close "
        "the wizard (its X button loses ALL progress; state is client-side "
        "and restarts from Q1) and NEVER click unnamed buttons. Known "
        "answers from the applicant config: right to work=Yes (UK birth "
        "certificate), UK/EU 3+ years=Yes, age group (35-39), DOB "
        "(date_of_birth), gender, ethnicity, still in education=No, start "
        "date≈2 Mondays ahead, location=England, top-3 programmes=IT + Data "
        "Analytics + Software Development, highest qualification=Level 6 "
        "(BSc/NARIC), past apprenticeship=No, driving licence=Yes, own "
        "car=Yes, reasonable adjustments=No. Q17 'Check options that apply' "
        "(required multi-select, accommodation types with NO none-option): "
        "SCROLL the option list (the 'none of these / prefer not to say' "
        "entry is usually below the fold); if truly absent select nothing "
        "is rejected — pick 'Prefer not to say' if present, otherwise "
        "report to the user. After the wizard: CV upload + short form → "
        "'Submit application'. 'Easy Apply' badge jobs skip the wizard."
    ),
    "totaljobs": (
        "Totaljobs (and Jobsite): job page → 'Apply now' → CV upload + cover "
        "letter field + screening questions → 'Submit application'. "
        "SUPPORTING FILES must be LARGER than 8KB — generate cover letters "
        "with make_cover_letter (DOCX, auto-padded). Uploads go through "
        "browser_upload directly on the hidden input[type=file] element "
        "(no native chooser needed). CAUTION: the browser profile may keep "
        "STALE TABS from previous sessions (old confirmation pages) — always "
        "verify you are on the CURRENT job page (check the URL) before "
        "concluding anything. Board apply redirects may lead to gov.uk "
        "'Find an apprenticeship' — if it says 'no longer accepting', the "
        "vacancy is CLOSED: mark the application failed and tell the user. "
        "My applications: https://www.totaljobs.com/profile (not /applications)."
    ),
    "cv-library": (
        "CV-Library: job page → 'Apply for job' → account CV or upload → quick "
        "questions → 'Submit application'."
    ),
    "linkedin": (
        "LinkedIn: ToS restrict automated applying. Use the logged-in session, "
        "slow pace, 'Easy Apply' modal (CV + questions). Prefer surfacing jobs "
        "to the user over bulk applying."
    ),
    "earthworks": (
        "Earthworks jobpost pages link to the employer/agency instructions — "
        "usually an email address or external portal; follow the page text."
    ),
    "findajob": (
        "GOV.UK Work Hub (jobs.service.gov.uk): the 'Apply for this job' "
        "button opens a 'Before you apply' page → click 'Continue to the "
        "employer's website' — Work Hub does NOT host its own application "
        "form; it redirects to the employer's external site (NHS Jobs, "
        "council portals, etc.). Sign in via GOV.UK One Login (email + "
        "confirmation code, NOT Google SSO) — needed to reach the apply "
        "button. On the employer site: upload CV, fill form, submit."
    ),
    "website_form": (
        "Employer career sites (Workday/SmartRecruiters/Greenhouse/iCIMS): "
        "browser_open(url) → browser_form() → browser_set per field → upload CV → "
        "next/submit → confirm → browser_screenshot. Workday flows are multi-page: "
        "expect 2-4 form pages."
    ),
    # Civil Service / DWP: the application is graded on written statements, so
    # the mechanics below are only half the job — the assessment protocol from
    # statement.py rides along in the plan as `assessment_protocol`.
    "civil_service_form": statement_mod.CSJ_PLAYBOOK,
}


def _apply_method(job: dict) -> tuple[str, str | None, list[str]]:
    """Guess how to apply from the job's source/URL."""
    url = (job.get("apply_url") or job.get("url") or "").lower()
    source = (job.get("source") or "").lower()
    if statement_mod.is_civil_service(job):
        # Statement-graded application: mechanics in the playbook, the
        # mandatory assessment protocol in `assessment_protocol`.
        return ("civil_service_form", job.get("apply_url") or job.get("url"),
                ["STATEMENT-ASSESSED: read 'Selection process details' in the "
                 "advert BEFORE writing — the initial sift usually runs on the "
                 "technical statement alone (DWP 2026-08-30: 4/7 there, "
                 "personal statement 3/7 = rejection)",
                 "Name-blind form, no CV upload: employment history, personal "
                 "statement and technical statement are TYPED; run "
                 "check_statement on each text before submitting"])
    if "indeed." in url or source == "indeed":
        return ("indeed_easy_apply", job.get("apply_url") or job.get("url"),
                ["Indeed blocks bots aggressively; prefer a logged-in browser profile",
                 "Easy Apply jobs complete in-page; others redirect to the employer site"])
    if "linkedin." in url or source == "linkedin":
        return ("linkedin_easy_apply", job.get("apply_url") or job.get("url"),
                ["LinkedIn ToS restrict automation — keep applications slow and manual-ish"])
    if source in ("reed", "totaljobs", "cv-library", "cvlibrary"):
        return ("board_account_apply", job.get("apply_url") or job.get("url"),
                ["Log into the board account in the browser profile first",
                 "Upload the chosen CV file, answer screening questions"])
    if source == "earthworks":
        return ("employer_site_or_email", job.get("apply_url") or job.get("url"),
                ["Earthworks posts link to the employer/agency instructions — read the page"])
    if source == "findajob":
        return ("employer_site_or_email", job.get("apply_url") or job.get("url"),
                ["GOV.UK Work Hub redirects to the employer's website — "
                 "click 'Continue to the employer's website' on the 'Before "
                 "you apply' page",
                 "GOV.UK One Login (email + code, NOT Google SSO) required "
                 "to reach the Apply button; the profile now has a session",
                 "On the employer site: upload CV, fill form, submit"])
    return ("website_form", job.get("apply_url") or job.get("url"), [])


def _protocol_steps(job: dict, apply_url: str | None,
                    criteria: list[str]) -> list[str]:
    """Mandatory pre-writing steps for statement-assessed (CSJ/DWP) vacancies."""
    where = apply_url or job.get("url") or "the vacancy page"
    steps = [
        "STATEMENT-ASSESSED VACANCY — BEFORE any writing: open "
        f"{where} and read 'Selection process details' IN FULL. Record: which "
        "statement the initial sift uses, the pass mark, what the full sift "
        "scores, and the interview stages. Writing from a search snippet is "
        "the 2026-08-30 failure that scored personal statement 3/7.",
        "Copy the advert's essential criteria VERBATIM into your notes (the "
        "form grades against those exact lines) and read the AI guidance "
        "linked from the advert.",
        "Collect the form's word limits (the page shows a live counter) and "
        "draft: personal statement filled to >=90% of the limit, one block "
        "per criterion with quantified outcomes; technical statement as "
        "quantified STARs covering design/build/test/document/integrate/"
        "operate — a DIFFERENT example from the personal statement.",
        "HUMANISE EVERY TEXT: run mcp__sapling__aidetect on the personal "
        "statement, technical statement, employment history and any cover "
        "letter / free-text answer, rewrite the flagged sentences in the "
        "candidate's own voice until the document reports 0% AI (Sapling "
        "needs 500+ characters to be reliable).",
        "Gate every text with check_statement(...) (pass kind, word_limit, "
        "criteria, the sibling text, ai_score=<Sapling %>, "
        "ai_score_required=True) and fix all blocks and warnings — do not "
        "submit while the verdict is not 'pass'. Record the final word counts "
        "and Sapling readings in record_application evidence.",
    ]
    if criteria:
        steps.insert(1, "Criteria extracted from the stored description "
                        f"({len(criteria)}): " + " | ".join(criteria[:6]))
    return steps


async def start_application(conn: aiosqlite.Connection, settings: Settings,
                            job_id: str, cv_id: str | None = None,
                            notes: str | None = None) -> dict:
    """Create (or return the existing) application with a full apply plan.

    Never creates a second application for a job that already has one in a
    non-withdrawn state — this is the memory that prevents re-applying to a
    vacancy submitted days or weeks ago, even when it is re-found on another
    board (dedup keeps one canonical job row).
    """
    job = await db.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": f"unknown job_id {job_id}"}
    existing = await db.application_for_job(conn, job_id)
    cautions: list[str] = []
    if existing and existing.get("status") not in ("withdrawn", "failed"):
        job_brief = {k: job.get(k) for k in
                     ("id", "title", "company", "location_text", "url", "apply_url",
                      "salary_raw", "posted_at", "source")}
        return {
            "ok": True, "already_exists": True, "application_id": existing["id"],
            "existing_status": existing["status"],
            "cautions": [
                f"An application for this job already exists "
                f"(status={existing['status']}, updated {existing['updated_at']}). "
                "Do NOT submit again; ask the user if they want to withdraw/re-apply."
            ],
            "application": existing,
        }
    from .cvmanager import recommend_cv

    import json as _json

    extra = {}
    try:
        extra = _json.loads(job.get("extra") or "{}")
    except (TypeError, ValueError):
        pass
    await db.ensure_seed_blocklist(conn, settings)
    companies, keywords = await db.load_blocked_norms(conn)
    hit = db.is_blocked(job.get("company"),
                        f"{job.get('title')} {job.get('description') or ''}",
                        companies, keywords)
    if hit:
        return {"ok": False, "blocked": True,
                "error": f"{job.get('company')} is on the blocklist ({hit}) — "
                         "remove it via manage_blocklist if the user changed "
                         "their mind"}
    if extra.get("training_offer"):
        return {"ok": False, "training_offer": True,
                "error": "this listing is a paid training/course ad "
                         f"({extra.get('training_reason')}) — not a real job; "
                         "do not apply"}
    loc_status = extra.get("location_status")
    if loc_status == "mismatch":
        cautions.append(
            f"LOCATION MISMATCH: {extra.get('location_reason')} — only proceed if "
            "the user explicitly confirmed they want to apply to this far-away "
            "non-remote job"
        )
    elif loc_status == "caution":
        cautions.append(f"Location caution: {extra.get('location_reason')}")
    recs = await recommend_cv(conn, job, limit=3)
    chosen = None
    if cv_id:
        chosen = await db.get_cv(conn, cv_id)
    elif recs and recs[0]["score"] > 0:
        chosen = await db.get_cv(conn, recs[0]["cv_id"])
    app_id = await db.create_application(conn, job_id,
                                         (chosen or {}).get("id") if chosen else cv_id,
                                         notes)
    method, apply_url, method_cautions = _apply_method(job)
    steps = [
        "VERIFY THE VACANCY IS LIVE: open the job URL; if the page says "
        "'no longer accepting' / 'closed' → mark this application failed "
        "and inform the user (boards keep stale ads for days)",
        f"Open {apply_url} with browser_open (headed mode keeps logins)",
        "Check the browser tab URL matches THIS job — stale tabs from "
        "previous sessions may be open; use browser_tabs to switch",
        "Sign in / create the account if the board requires it (persist in the profile)",
        f"Upload CV: {(chosen or {}).get('path') or 'pick via list_cvs'}",
        "Fill the form with the applicant profile values (see applicant block)",
        "Write a short tailored cover letter referencing the job's key requirements",
        "Screenshot the confirmation page (browser_screenshot)",
        "Call record_application with status='submitted' and the screenshot as evidence",
    ]
    protocol = statement_mod.protocol_for(job)
    criteria = statement_mod.extract_criteria(job.get("description")) if protocol else []
    if protocol:
        steps = _protocol_steps(job, apply_url, criteria) + steps
    plan = ApplyPlan(
        application_id=app_id,
        job={k: job.get(k) for k in
             ("id", "title", "company", "location_text", "url", "apply_url",
              "salary_raw", "posted_at", "source", "contract_type", "description")},
        cv={"id": chosen["id"], "filename": chosen["filename"],
            "path": chosen["path"], "tags": chosen.get("tags")}
        if chosen else None,
        cv_alternatives=[r for r in recs if not chosen or r["cv_id"] != chosen.get("id")],
        apply_url=apply_url,
        apply_method=method,
        applicant={k: v for k, v in settings.applicant.items() if v},
        steps=steps,
        cautions=cautions + method_cautions,
    )
    result = {"ok": True, "already_exists": False,
              "plan": plan.model_dump(mode="json")}
    result["plan"]["playbook"] = PLAYBOOKS.get(method, PLAYBOOKS["website_form"])
    if protocol:
        result["plan"]["assessment_protocol"] = protocol
        result["plan"]["statement_plan"] = statement_mod.statement_plan(
            criteria=criteria)
    result["plan"]["location"] = {
        k: extra.get(k) for k in ("work_mode", "distance_miles",
                                  "location_status", "location_reason")
    }
    return result
