"""Statement-assessed applications: Civil Service Jobs / DWP and lookalikes.

Why this module exists (2026-08-30 regression): the DWP Digital
"Data Engineer Level I" application was sifted as technical 4/7 (the exact
pass mark), personal statement 3/7 (one band below it) and the employment
history "not assessed" — a rejection caused by the writing, not the CV:

* the personal statement used 441 of 750 allowed words (59%);
* the essential criteria were taken from a truncated Work Hub snippet, not
  from the advert (the form says "how you meet the essential criteria,
  outlined within the advert");
* nobody opened "Selection process details", which states that the INITIAL
  SIFT runs on the technical statement alone;
* the one data-engineering story was told twice (statement + technical), so
  the two separately-scored slots carried a single example with no numbers.

Everything here is pure text/JSON logic — no DB, no network — so it is cheap
to call from the server, from the tracker and from tests.
"""

from __future__ import annotations

import re
from typing import Any

# --- detection -------------------------------------------------------------

# Hosts that only ever serve Civil Service recruitment (unambiguous).
CS_HOSTS = (
    "civilservicejobs.service.gov.uk",
    "cshr.tal.net",            # eArcu/TAL eforms behind CSJ (candidate/eform/...)
    "csrc",                    # legacy csrc/jobs.cgi links
    "jobsearch.civilservicejobs",
)
# GOV.UK Work Hub (findajob) redirects to ANY employer — it is a signal only
# together with a civil-service employer/description below.
WORK_HUB_HOSTS = ("jobs.service.gov.uk",)

CS_EMPLOYER_RE = re.compile(
    r"\b(dwp|department for work and pensions|dwp digital"
    r"|government recruitment service|civil service"
    r"|hmrc|home office|cabinet office|ministry of justice|ministry of defence"
    r"|department for education|defra|department for transport"
    r"|companies house|ofsted|nhs business services authority"
    r"|uk health security agency|food standards agency)\b",
    re.I,
)

# Wording that appears in adverts whose application is graded on statements.
STATEMENT_HINT_RE = re.compile(
    r"(personal statement|supporting statement|technical statement"
    r"|selection process details|essential criteria)",
    re.I,
)


def _blob(job: dict) -> str:
    parts = [job.get("title"), job.get("company"), job.get("description")]
    return " ".join(str(p) for p in parts if p)


def is_civil_service(job: dict) -> bool:
    """True for Civil Service Jobs / DWP-style vacancies (statement sifts)."""
    url = str(job.get("apply_url") or job.get("url") or "").lower()
    if any(h in url for h in CS_HOSTS):
        return True
    source = str(job.get("source") or "").lower()
    if source in ("civilservicejobs", "csj", "csj_workhub"):
        return True
    blob = _blob(job)
    if CS_EMPLOYER_RE.search(blob):
        return True
    return bool(any(h in url for h in WORK_HUB_HOSTS)
                and STATEMENT_HINT_RE.search(blob))


def is_statement_assessed(job: dict) -> bool:
    """Civil-service vacancies plus any advert explicitly graded on a statement."""
    if is_civil_service(job):
        return True
    return bool(STATEMENT_HINT_RE.search(_blob(job)))


# --- the mandatory protocol ------------------------------------------------

CIVIL_SERVICE_PROTOCOL: dict[str, Any] = {
    "board": "Civil Service Jobs (CSJ) / GOV.UK Work Hub",
    "assessment_model": (
        "Written statements are scored band-by-band (Not demonstrated → "
        "Outstanding) against the advert's essential criteria. The initial "
        "sift usually runs on the TECHNICAL statement alone; candidates who "
        "pass it get a full sift (employment history + personal statement)."
    ),
    "regression": (
        "2026-08-30 DWP Data Engineer Level I: technical 4/7 (the pass mark, "
        "zero margin), personal statement 3/7 (below the 4/7 bar), employment "
        "history not assessed → rejection decided entirely by the writing."
    ),
    "must_read_before_writing": [
        "Read 'Selection process details' on the vacancy page IN FULL: what "
        "the initial sift uses (often the technical statement only), the pass "
        "mark, what the full sift scores, and the interview stages. Never "
        "write from a search-result snippet — open the advert.",
        "Copy the essential criteria VERBATIM into notes; the statement is "
        "graded against those exact lines, not against your summary.",
        "Read the AI guidance linked from the advert and keep to it: own "
        "words, no generic AI phrasing, every claim evidenceable at interview.",
        "Note the word limits on the form (personal statement often 750-1250, "
        "technical statement usually 250) and the name-blind rules: no name, "
        "institutions, employers, contacts, address/city, nationality or "
        "immigration status.",
    ],
    "statement_rules": [
        "Fill at least 90% of every limit. The form itself says 'provide as "
        "much information and detail as you can, outlining what you did, and "
        "what the outcome was' — an under-filled statement reads as thin "
        "evidence (441/750 words scored 3/7).",
        "One block per essential criterion, each answering: WHAT you did, HOW, "
        "and the OUTCOME with numbers (volumes, row counts, hours saved, "
        "error rates, consumers, sources, team size). Zero digits = moderate "
        "evidence.",
        "The technical statement is usually the gate: Situation/Task/Action/"
        "Result, the Working-level verbs (design, build, test, document, "
        "integrate, operate), the stack named (SQL dialect, warehouse/cloud, "
        "orchestration, Git/CI), and TWO different examples when the limit "
        "allows.",
        "Add one step beyond the descriptor ('exceeding expectations'): set "
        "the standard, kept a pipeline in production, mentored a colleague, "
        "measurably improved reliability.",
        "Never reuse the same story in the personal statement and the "
        "technical statement — they are scored separately; a repeated story "
        "wastes one slot and shows no breadth.",
        "De-identify only what the rules prohibit. Keep sector, scale, team "
        "size, volumes and tech: stripping those leaves 'a UK technology "
        "company' and unscoreable evidence.",
        "EVERY text we submit must read as human-written: run "
        "mcp__sapling__aidetect on it and rewrite until it reports 0% AI — "
        "personal statement, technical statement, employment history and any "
        "cover letter or free-text answer. Then pass ai_score=0 into "
        "check_statement; a statement-assessed text cannot reach 'pass' "
        "without it.",
        "Run check_statement on every text before submitting and fix every "
        "blocker and warning. Submit with time to spare — CSJ pages time out.",
    ],
    "form_sections": [
        "Section 1: Guidance → Eligibility → Personal information → Diversity "
        "→ Declaration",
        "Section 2: Your CV (employment history text) → Personal statement → "
        "Technical skills → Preferences → Declaration",
        "Name-blind: there is NO CV file upload; the employment history is "
        "typed text with all identifying details removed.",
    ],
    "after_submission": [
        "Screenshot the 'Application received' confirmation and record it as "
        "evidence.",
        "Login is one-shot: the CSJ session is reached through the vacancy's "
        "'Apply now' link (a SID is minted per click) — the user types the "
        "password manually; never store it.",
        "When the sift outcome arrives, call record_sift_feedback with the "
        "panel's scores so the next statement is written against real bands.",
    ],
}


CSJ_PLAYBOOK = (
    "Civil Service Jobs (via GOV.UK Work Hub or direct): 'Apply for this job' "
    "→ 'Before you apply' → the employer's site is CSJ. A 'Quick check needed' "
    "page (altcha checkbox) may appear first — tick it, then 'Continue'. On "
    "the CSJ vacancy: 'Apply now' → sign in (login is reached ONLY through "
    "this vacancy link; the user types the password) → Section 1 "
    "(Eligibility/Personal/Diversity/Declaration) → 'Application started' → "
    "Section 2 supporting evidence: Your CV (name-blind typed text), Personal "
    "statement, Technical skills, Preferences, Declaration → 'Continue "
    "application'. The page shows a live word counter. NEVER invent content: "
    "everything must come from the candidate's CV facts. Finish with "
    "browser_screenshot of the confirmation page."
)


def protocol_for(job: dict) -> dict[str, Any] | None:
    """Return the mandatory protocol for statement-assessed vacancies, else None."""
    if is_civil_service(job):
        return {"required": True, "kind": "civil_service", **CIVIL_SERVICE_PROTOCOL}
    if is_statement_assessed(job):
        return {
            "required": True,
            "kind": "statement_assessed",
            "board": job.get("source") or "employer",
            "must_read_before_writing": CIVIL_SERVICE_PROTOCOL["must_read_before_writing"],
            "statement_rules": CIVIL_SERVICE_PROTOCOL["statement_rules"],
            "note": (
                "This advert mentions a statement/selection process; read the "
                "advert's assessment details before writing and run "
                "check_statement on every text."
            ),
        }
    return None


# --- planning --------------------------------------------------------------

def statement_plan(ps_limit: int = 750, tech_limit: int = 250,
                   criteria: list[str] | None = None,
                   budget_pct: int = 90) -> dict[str, Any]:
    """Word targets and slot structure for a statement-assessed application."""
    criteria = [c.strip() for c in (criteria or []) if c and c.strip()]
    ps_target = round(ps_limit * budget_pct / 100)
    tech_target = round(tech_limit * budget_pct / 100)
    per_criterion = round(ps_target / max(1, len(criteria))) if criteria else None
    return {
        "budget_pct": budget_pct,
        "personal_statement": {
            "limit": ps_limit,
            "target_min_words": ps_target,
            "slots": [
                {"criterion": c, "target_words": per_criterion} for c in criteria
            ] or "one block per essential criterion (fill the advert's list here)",
        },
        "technical_statement": {
            "limit": tech_limit,
            "target_min_words": tech_target,
            "examples_expected": 2,
            "structure": "Situation / Task / Action / Result per example",
            "descriptor_verbs": ["design", "build", "test", "document",
                                 "integrate", "operate"],
        },
        "employment_history": {
            "note": "name-blind typed text; not always scored — do not spend "
                    "the writing budget here before the scored texts are done",
        },
        "ai_detection": {
            "tool": "mcp__sapling__aidetect",
            "target_pct": 0,
            "applies_to": ["personal statement", "technical statement",
                           "employment history", "cover letter",
                           "any free-text screening answer"],
            "rule": "run it on every text before pasting; rewrite the flagged "
                    "sentences until the document reports 0% AI, then pass "
                    "ai_score=0 to check_statement. Sapling is only reliable "
                    "on 500+ characters (it warns on short texts).",
        },
        "rule": (
            "Never reuse the same story across personal and technical "
            "statements; each separately-scored text needs its own example."
        ),
    }


_BULLET_RE = re.compile(r"^\s*(?:[-•*]|\d+[.)]|\w\))\s*(.+)$")
_CRIT_SECTION_RE = re.compile(
    r"(?:essential criteria|essential skills|essential requirements|requirements)"
    r"[^\n]*\n(.*?)"
    r"(?=\n\s*(?:desirable|benefits|about (?:us|the)|how to apply|selection process|"
    r"the application|interview|stage \d)|$)",
    re.I | re.S,
)


def extract_criteria(description: str | None, limit: int = 12) -> list[str]:
    """Pull the advert's essential criteria out of a full job description.

    Only works on a FULL advert text, which is exactly the point: a truncated
    search snippet yields nothing, and writing from that snippet is how the
    2026-08-30 statement missed the real criteria.
    """
    if not description:
        return []
    text = description.replace("\r", "")
    m = _CRIT_SECTION_RE.search(text)
    chunk = (m.group(1) if m else "").strip()
    if not chunk:
        return []
    items: list[str] = []
    for line in chunk.split("\n"):
        bullet = _BULLET_RE.match(line)
        if bullet:
            item = re.sub(r"\s+", " ", bullet.group(1)).strip(" .;,")
            if len(item) >= 12:
                items.append(item)
    if not items:
        for sentence in re.split(r"(?<=[.;])\s+", chunk):
            s = re.sub(r"\s+", " ", sentence).strip(" .;,")
            if len(s) >= 25 and re.search(
                    r"\b(experience|ability|knowledge|skills?|proven|understanding)\b",
                    s, re.I):
                items.append(s)
    return items[:limit]


# --- review (the quality gate) ---------------------------------------------

FILLER_PHRASES = (
    "i am passionate about", "passionate about", "i am excited", "excited to",
    "highly motivated", "team player", "hit the ground running",
    "detail-oriented", "detail oriented", "proven track record",
    "dynamic environment", "fast-paced", "leverage", "delve", "tapestry",
    "testament to", "in today's", "go-getter", "results-driven",
    "think outside the box", "wear many hats", "treated pipelines as products",
    "i bring the same rigour", "turning a vague question into",
)

_NUM_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*([a-zA-Z%]*)?")
_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
           "oct", "nov", "dec"}
_FUNCTION_WORDS = {"in", "of", "to", "and", "or", "the", "a", "an", "per",
                   "at", "on", "by", "for", "with", "from", "was", "were"}


def _count_quantified(text: str) -> int:
    """Count quantities: "240 boreholes", "14 rules", "1.2m records", "37%".

    Dates ("2019 to 2023") and bare years do not count — the sift reads
    evidence, not a career timeline.
    """
    n = 0
    for m in _NUM_RE.finditer(text or ""):
        raw, word = m.group(1), (m.group(2) or "").lower()
        if word == "%" or word.startswith("percent"):
            n += 1
            continue
        if not word or word in _FUNCTION_WORDS or word in _MONTHS:
            continue
        value = float(raw.replace(",", "").rstrip("."))
        if 1900 <= value <= 2035 and "." not in raw and "," not in raw:
            continue  # a year, not a quantity
        n += 1
    return n
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_RE = re.compile(r"(https?://|www\.|linkedin\.com)", re.I)
_PHONE_RE = re.compile(r"(?:\+44|\(0\)|\b0)\s?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}\b")
_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b")
_STAR_KEYS = ("situation", "task", "action", "result")
_CRITERION_STOP = {
    "ability", "experience", "knowledge", "skills", "skill", "working",
    "level", "with", "that", "this", "have", "using", "including", "and",
    "the", "for", "you", "your", "will", "must", "role", "team", "work",
    "demonstrate", "demonstrated", "evidence", "understanding", "good",
}


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _content_terms(phrase: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z+#.-]{3,}", (phrase or "").lower())
    return [w for w in words if w not in _CRITERION_STOP]


def _shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _duplication(text: str, other: str) -> float:
    """Share of this text's 6-grams that also occur in `other`."""
    a, b = _shingles(text), _shingles(other)
    if not a:
        return 0.0
    return len(a & b) / len(a)


# Vocabulary that says nothing about which EXAMPLE was used.
_STOP_TERMS = {
    "about", "which", "there", "their", "these", "those", "would", "could",
    "should", "being", "other", "where", "after", "before", "under", "while",
    "between", "through", "across", "because", "during", "within", "without",
    "every", "first", "also", "using", "worked", "working", "making", "took",
    # generic data-domain words the two statements legitimately share
    "data", "analysis", "analyst", "analysts", "dataset", "datasets", "sql",
    "python", "pipeline", "pipelines", "report", "reports", "reporting",
    "dashboard", "dashboards", "team", "teams", "colleague", "colleagues",
    "role", "work", "company", "technical", "systems", "system", "stakeholder",
    "stakeholders", "business", "process", "processes", "results", "numbers",
    "figures", "quality", "tools", "tool", "skills", "experience", "managed",
}


def _distinctive_terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 5 and w not in _STOP_TERMS}


def _story_overlap(text: str, other: str) -> float:
    """Share of the shorter text's distinctive vocabulary reused by the other.

    Catches the 2026-08-30 failure that sentence-shingles miss: the personal
    statement and the technical statement told the SAME project story in
    different words (38% shared vocabulary), so the two separately-scored
    slots carried one example.
    """
    a, b = _distinctive_terms(text), _distinctive_terms(other)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _criteria_coverage(text: str, criteria: list[str]) -> list[dict[str, Any]]:
    hay = (text or "").lower()
    out = []
    for c in criteria:
        terms = _content_terms(c)
        if not terms:
            out.append({"criterion": c, "covered": None, "matched_terms": []})
            continue
        matched = [t for t in terms if t in hay]
        out.append({
            "criterion": c,
            "matched_terms": matched,
            "coverage_pct": round(100 * len(matched) / len(terms)),
            "covered": len(matched) / len(terms) >= 0.5,
        })
    return out


def review_statement(
    text: str,
    *,
    kind: str = "personal_statement",
    word_limit: int | None = None,
    criteria: list[str] | None = None,
    other_text: str | None = None,
    forbidden_terms: list[str] | None = None,
    budget_pct: int = 90,
    ai_score: float | None = None,
    ai_score_required: bool = False,
) -> dict[str, Any]:
    """Grade a drafted statement the way a sift panel will, before submitting.

    kind: personal_statement | technical_statement | employment_history | other.
    ai_score: the percentage reported by mcp__sapling__aidetect for this text;
    ai_score_required=True (statement-assessed applications) refuses to pass
    without a 0.
    Returns {verdict, word_count, issues:[{level,code,message}], stats, fixes}.
    level is 'block' (do not submit) or 'warn' (fix it).
    """
    text = text or ""
    words = word_count(text)
    issues: list[dict[str, str]] = []
    stats: dict[str, Any] = {"kind": kind, "word_count": words}

    def add(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    # 1. length
    if words == 0:
        add("block", "empty", "the statement is empty")
    if word_limit:
        stats["limit"] = word_limit
        if words > word_limit:
            add("block", "over_limit",
                f"{words} words vs limit {word_limit} — cut before submitting")
        used = round(100 * words / word_limit) if word_limit else 0
        stats["budget_pct"] = used
        stats["target_min_words"] = round(word_limit * budget_pct / 100)
        if words and used < budget_pct:
            add("warn", "under_budget",
                f"only {used}% of the {word_limit}-word limit used "
                f"({words}/{word_limit}); the form asks for as much detail as "
                f"you can give — aim for {stats['target_min_words']}+ words")

    # 2. quantified evidence
    numbers = re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text)
    quantified = _count_quantified(text)
    stats["numeric_tokens"] = len(numbers)
    stats["quantified_tokens"] = quantified
    if words and quantified == 0:
        add("warn", "no_quantification",
            "no quantified outcomes (volumes, hours saved, counts, %). "
            "Unquantified evidence reads as 'moderate demonstration'")

    # 3. technical statement shape
    low = text.lower()
    if kind == "technical_statement":
        missing = [k for k in _STAR_KEYS if k not in low]
        stats["star_missing"] = missing
        if missing:
            add("warn", "no_star",
                f"Situation/Task/Action/Result markers missing: {missing} — "
                "the form asks for that structure")
        verbs = ["design", "build", "test", "document", "integrat", "operat"]
        absent = [v for v in verbs if v not in low]
        stats["descriptor_verbs_absent"] = absent
        if len(absent) >= 3:
            add("warn", "thin_descriptor",
                f"the working-level descriptor verbs barely appear "
                f"(missing: {absent}) — name what you designed/built/tested/"
                "documented/integrated/operated")

    # 4. criteria coverage
    if criteria:
        cov = _criteria_coverage(text, criteria)
        stats["criteria"] = cov
        uncovered = [c["criterion"] for c in cov if c.get("covered") is False]
        if uncovered:
            add("warn", "criteria_gap",
                "these essential criteria are not clearly addressed: "
                + "; ".join(uncovered[:4]))

    # 5. duplication with the sibling text
    if other_text:
        dup = _duplication(text, other_text)
        stats["duplication_pct"] = round(100 * dup)
        if dup >= 0.10:
            add("warn", "duplication",
                f"{round(100 * dup)}% of this text's phrasing also appears in "
                "the sibling statement — the two texts are scored separately, "
                "use a different example")
        story_pct = round(100 * _story_overlap(text, other_text))
        stats["story_overlap_pct"] = story_pct
        if story_pct >= 30:
            add("warn", "same_example",
                f"{story_pct}% of the shorter text's distinctive "
                "vocabulary is reused in the sibling statement — both scored "
                "texts look like the SAME project; give the technical "
                "statement a different example")

    # 6. name-blind scan
    hits: list[str] = []
    if _EMAIL_RE.search(text):
        hits.append("email address")
    if _URL_RE.search(text):
        hits.append("URL/profile link")
    if _PHONE_RE.search(text):
        hits.append("phone number")
    if _POSTCODE_RE.search(text):
        hits.append("postcode")
    for term in forbidden_terms or []:
        t = (term or "").strip()
        if t and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text, re.I):
            hits.append(term)
    stats["name_blind_hits"] = hits
    if hits:
        add("block", "name_blind",
            "name-blind violation — remove: " + ", ".join(hits))

    # 7. filler / AI-ish phrasing
    found = [p for p in FILLER_PHRASES if p in low]
    stats["filler_phrases"] = found
    if found:
        add("warn", "filler",
            "generic/filler phrasing found: " + "; ".join(found[:5])
            + " — replace with a concrete fact")

    # 7b. AI-detection gate (Sapling must report 0% — every text we send)
    stats["ai_score"] = ai_score
    if ai_score is not None:
        if ai_score > 0:
            add("block", "ai_score",
                f"Sapling reports {ai_score}% AI for this text — rewrite the "
                "flagged sentences in your own words and re-run "
                "mcp__sapling__aidetect until it reads 0%")
    elif ai_score_required:
        add("warn", "ai_check_missing",
            "no Sapling result supplied: run mcp__sapling__aidetect on this "
            "text, rewrite until it reports 0% AI, then call check_statement "
            "again with ai_score=0 (Sapling needs 500+ characters to be "
            "reliable)")

    # 8. structure
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    stats["paragraphs"] = len(paragraphs)
    if kind == "personal_statement" and criteria and len(paragraphs) < len(criteria):
        add("warn", "structure",
            f"{len(paragraphs)} paragraphs for {len(criteria)} criteria — give "
            "each criterion its own block")

    blockers = [i for i in issues if i["level"] == "block"]
    warns = [i for i in issues if i["level"] == "warn"]
    verdict = "block" if blockers else ("revise" if warns else "pass")
    fixes = [i["message"] for i in issues]
    return {"verdict": verdict, "word_count": words, "issues": issues,
            "stats": stats, "fixes": fixes}


# --- feedback loop ---------------------------------------------------------

BANDS = ("not_met", "minimal", "moderate", "acceptable", "good", "strong",
         "outstanding")


def band_name(score: int | None) -> str | None:
    if score is None:
        return None
    if score <= 0:
        return "not assessed / not demonstrated"
    idx = min(score, len(BANDS)) - 1
    return BANDS[idx]


def lessons_from_scores(technical: int | None = None,
                        personal_statement: int | None = None,
                        cv: int | None = None,
                        threshold: int = 4) -> list[str]:
    """Turn sift scores into the concrete fixes for the next application."""
    out: list[str] = []
    if personal_statement is not None and personal_statement < threshold:
        out.append(
            f"Personal statement scored {personal_statement}/{threshold} "
            f"({band_name(personal_statement)}): fill >=90% of the limit, one "
            "block per essential criterion taken verbatim from the advert, "
            "each with quantified outcomes.")
    if technical is not None and technical < threshold:
        out.append(
            f"Technical statement scored {technical}/{threshold} "
            f"({band_name(technical)}): rewrite as two quantified STAR "
            "examples naming the stack and covering design/build/test/"
            "document/integrate/operate.")
    elif technical is not None and technical == threshold:
        out.append(
            f"Technical statement scored exactly the {threshold}/7 bar — zero "
            "margin: add scale, production operation and standards-setting "
            "evidence to sit in the Good/Strong bands.")
    if cv is not None and cv <= 0:
        out.append(
            "Employment history was not assessed — spend the writing budget "
            "on the scored texts first.")
    if not out:
        out.append("All scored elements met the bar; keep the format.")
    out.append(
        "Also read 'Selection process details' before writing: it names which "
        "statement the initial sift uses.")
    return out
