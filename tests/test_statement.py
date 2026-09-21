"""Tests for the statement-quality gate (statement.py).

Regression source: the 2026-08-30 DWP Data Engineer Level I rejection —
technical 4/7 (the pass mark), personal statement 3/7 (below it), employment
history "not assessed". The statement used 441 of 750 words and the criteria
were taken from a search snippet instead of the advert. These tests pin the
detection, the review rules and the feedback loop that must catch each of
those mistakes before a submission.
"""

from work_researcher.statement import (
    CIVIL_SERVICE_PROTOCOL,
    band_name,
    extract_criteria,
    is_civil_service,
    is_statement_assessed,
    lessons_from_scores,
    protocol_for,
    review_statement,
    statement_plan,
    word_count,
)

# ------------------------------------------------------------- detection ----

LEVEL_I = {
    "title": "Data Engineer Level I",
    "company": "DWP Digital",
    "source": "adzuna",
    "url": "https://www.jobs.service.gov.uk/jobs/6a85efb1123dc116db91a7e2",
    "description": "DWP. Digital with Purpose. We need a Data Engineer to join "
                   "our Debt Solutions team.",
}


def test_detects_dwp_vacancy_on_work_hub() -> None:
    assert is_civil_service(LEVEL_I) is True


def test_detects_csj_hosts_and_eform_urls() -> None:
    assert is_civil_service({"url": "https://cshr.tal.net/vx/candidate/eform/58286740"})
    assert is_civil_service(
        {"url": "https://www.civilservicejobs.service.gov.uk/csr/jobs.cgi?jcode=2009916"})
    assert is_civil_service({"url": "https://www.jobs.service.gov.uk/x",
                             "description": "Government Recruitment Service"}) is True


def test_ignores_ordinary_private_sector_jobs() -> None:
    adria = {"title": "Junior SQL Data Analyst", "company": "Adria Solutions",
             "url": "https://www.totaljobs.com/job/x", "description": "SQL, Excel"}
    assert is_civil_service(adria) is False
    assert protocol_for(adria) is None


def test_statement_hint_flags_non_civil_service_adverts() -> None:
    nhs = {"title": "Data Analyst", "company": "NHS Trust",
           "description": "Please attach a supporting statement addressing the "
                          "essential criteria."}
    assert is_civil_service(nhs) is False
    assert is_statement_assessed(nhs) is True
    assert protocol_for(nhs)["kind"] == "statement_assessed"


def test_protocol_carries_the_mandatory_reading_list() -> None:
    protocol = protocol_for(LEVEL_I)
    assert protocol is not None and protocol["required"] is True
    joined = " ".join(protocol["must_read_before_writing"])
    assert "Selection process details" in joined
    assert "essential criteria" in joined.lower()
    assert "AI guidance" in joined
    for key in ("statement_rules", "form_sections", "after_submission"):
        assert CIVIL_SERVICE_PROTOCOL[key]


# --------------------------------------------------------------- planning ----

def test_plan_sets_90_percent_targets_and_per_criterion_slots() -> None:
    plan = statement_plan(ps_limit=750, tech_limit=250,
                          criteria=["SQL data transformation", "ETL pipelines",
                                    "dashboards and reports"])
    ps = plan["personal_statement"]
    assert ps["limit"] == 750 and ps["target_min_words"] == 675
    assert [s["target_words"] for s in ps["slots"]] == [225, 225, 225]
    assert plan["technical_statement"]["target_min_words"] == 225
    assert plan["technical_statement"]["examples_expected"] == 2


def test_extract_criteria_reads_bullets_from_the_advert() -> None:
    advert = """DWP Digital. About the role...
Essential criteria
- Experience of SQL-based data transformation
- Experience of developing ETL pipelines
- Ability to build user-facing dashboards and reports
Desirable
- Experience of AWS
"""
    criteria = extract_criteria(advert)
    assert len(criteria) == 3
    assert criteria[0].startswith("Experience of SQL-based data transformation")
    assert all("AWS" not in c for c in criteria)


def test_extract_criteria_returns_nothing_for_a_truncated_snippet() -> None:
    assert extract_criteria("You'll need experience of SQL-based data transf") == []


# ----------------------------------------------------------- review gate ----

def _good_personal_statement() -> str:
    return (
        "SQL-based data transformation. In my most recent role I rebuilt the "
        "monthly reporting extracts in SQL, moving 14 manual spreadsheets into "
        "9 parameterised queries that reconciled 1.2m records per month; "
        "validation checks caught 37 upstream defects in the first quarter and "
        "cut preparation time from 6 hours to 20 minutes.\n\n"
        "ETL pipeline development. I designed and operated 5 Python/SQL "
        "pipelines on PostgreSQL with Git-based version control and CI checks, "
        "covering 3 source systems and 240k rows a day; the team of 4 analysts "
        "re-used them and reporting disputes fell to zero.\n\n"
        "Dashboards and reports. I delivered 12 recurring reports to 30 "
        "non-technical colleagues and replaced a manual monthly pack with a "
        "self-service view built in Power BI.\n"
    )


def test_good_statement_passes() -> None:
    text = _good_personal_statement()
    review = review_statement(
        text, kind="personal_statement", word_limit=130,
        criteria=["SQL-based data transformation", "ETL pipeline development",
                  "Dashboards and reports"])
    assert review["verdict"] == "pass", review["issues"]
    assert review["stats"]["budget_pct"] >= 90


def test_underfilled_and_unquantified_statement_is_flagged() -> None:
    """The exact 2026-08-30 shape: 59% of the limit and no numbers."""
    text = ("Over the past four years I have worked at the intersection of "
            "data analysis and software engineering. I analysed large "
            "time-series datasets and built reproducible workflows. I treated "
            "pipelines as products: versioned, documented, re-runnable.")
    review = review_statement(text, kind="personal_statement", word_limit=100)
    codes = {i["code"] for i in review["issues"]}
    assert "under_budget" in codes
    assert "no_quantification" in codes
    assert "filler" in codes          # "treated pipelines as products"
    assert review["verdict"] == "revise"


def test_over_limit_blocks() -> None:
    review = review_statement("word " * 260, word_limit=250)
    assert review["verdict"] == "block"
    assert any(i["code"] == "over_limit" for i in review["issues"])


def test_name_blind_violations_block() -> None:
    text = ("I studied at Abertay University and can be reached at "
            "andrew@example.com or +44 7838 228012; I live in FY2 9JF.")
    review = review_statement(text, word_limit=200)
    codes = {i["code"] for i in review["issues"]}
    assert "name_blind" in codes
    assert review["verdict"] == "block"
    hits = " ".join(review["stats"]["name_blind_hits"])
    assert "email address" in hits and "postcode" in hits


def test_forbidden_terms_catch_employers_and_universities() -> None:
    review = review_statement(
        "At CharBT I built pipelines for Abertay University coursework.",
        word_limit=100, forbidden_terms=["CharBT", "Abertay University"])
    assert review["verdict"] == "block"
    assert len(review["stats"]["name_blind_hits"]) == 2


def test_technical_statement_needs_star_and_descriptor_verbs() -> None:
    text = ("I made data better. Reports improved and people were happier "
            "with the numbers they received every month.")
    review = review_statement(text, kind="technical_statement", word_limit=250)
    codes = {i["code"] for i in review["issues"]}
    assert "no_star" in codes
    assert "thin_descriptor" in codes


def test_reused_story_between_sibling_texts_is_flagged() -> None:
    shared = ("I designed and built Python and SQL pipelines following an "
              "agreed internal standard with a validation layer that failed "
              "loudly rather than passing bad data forward to reporting.")
    personal = shared + " I also delivered dashboards to 30 colleagues."
    technical = shared + " The result: one-command re-runs."
    review = review_statement(personal, kind="personal_statement",
                              word_limit=200, other_text=technical)
    assert any(i["code"] == "duplication" for i in review["issues"])


def test_uncovered_criteria_are_reported() -> None:
    review = review_statement(
        "I have strong SQL skills from 4 years of analytics work with 1.2m rows.",
        word_limit=100,
        criteria=["SQL experience", "Kubernetes container orchestration"])
    gap = [i for i in review["issues"] if i["code"] == "criteria_gap"]
    assert gap and "Kubernetes" in gap[0]["message"]


def test_word_count_matches_form_style_counting() -> None:
    assert word_count("one two  three\nfour") == 4


# ----------------------------------------------------------- feedback loop ----

def test_band_names_map_to_the_civil_service_scale() -> None:
    assert band_name(4) == "acceptable"
    assert band_name(3) == "moderate"
    assert band_name(0) == "not assessed / not demonstrated"
    assert band_name(None) is None


def test_lessons_reproduce_the_level_i_fixes() -> None:
    lessons = lessons_from_scores(technical=4, personal_statement=3, cv=0,
                                  threshold=4)
    joined = " ".join(lessons)
    assert ">=90%" in joined                     # statement length fix
    assert "zero margin" in joined               # technical at the bar
    assert "not assessed" in joined              # wasted employment history
    assert "Selection process details" in joined


def test_lessons_are_quiet_when_everything_passed() -> None:
    lessons = lessons_from_scores(technical=5, personal_statement=6, cv=5)
    assert "keep the format" in lessons[0]


# ------------------------------------------------- integration: apply plan ----

import asyncio  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import aiosqlite  # noqa: E402

from work_researcher.config import Settings  # noqa: E402
from work_researcher.domain import JobCard  # noqa: E402
from work_researcher.persistence import SCHEMA, upsert_jobs  # noqa: E402
from work_researcher.tracker import PLAYBOOKS, start_application  # noqa: E402

DWP_ADVERT = """DWP Digital. Digital with Purpose.
Essential criteria
- Experience of SQL-based data transformation
- Experience of developing ETL pipelines
- Ability to build user-facing dashboards and reports
Desirable
- Experience of AWS
Selection process details
The sift panel will use your employment history, personal statement and
technical statement. An initial sift will be conducted using the technical
statement.
"""


@asynccontextmanager
async def _db(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "plan.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    try:
        yield conn
    finally:
        await conn.close()


def test_plan_for_dwp_job_carries_protocol_and_prepends_reading_steps(tmp_path) -> None:
    async def _run():
        async with _db(tmp_path) as conn:
            await upsert_jobs(conn, [JobCard(
                source="adzuna", url="https://www.jobs.service.gov.uk/jobs/6a85efb1123dc116db91a7e2",
                title="Data Engineer Level I", company="DWP Digital",
                location_text="Blackpool, Lancashire", salary_raw="£38,772 a year",
                description=DWP_ADVERT)])
            cur = await conn.execute("SELECT id FROM jobs LIMIT 1")
            job_id = (await cur.fetchone())[0]
            settings = Settings(db_path=tmp_path / "plan.db",
                                applicant={"full_name": "Test Candidate"})
            result = await start_application(conn, settings, job_id)
            await conn.commit()
            return result
    result = asyncio.run(_run())
    plan = result["plan"]
    assert plan["apply_method"] == "civil_service_form"
    protocol = plan["assessment_protocol"]
    assert protocol["required"] is True
    assert plan["statement_plan"]["personal_statement"]["target_min_words"] == 675
    # the advert's criteria were extracted from the stored description
    assert plan["statement_plan"]["personal_statement"]["slots"][0]["criterion"].startswith(
        "Experience of SQL-based data transformation")
    assert "Selection process details" in plan["steps"][0]
    assert "check_statement" in " ".join(plan["steps"])
    assert PLAYBOOKS["civil_service_form"] == plan["playbook"]
    assert any("STATEMENT-ASSESSED" in c for c in plan["cautions"])


def test_plan_for_ordinary_job_carries_no_protocol(tmp_path) -> None:
    async def _run():
        async with _db(tmp_path) as conn:
            await upsert_jobs(conn, [JobCard(
                source="totaljobs", url="https://www.totaljobs.com/job/adria/1",
                title="Junior SQL Data Analyst", company="Adria Solutions",
                location_text="Manchester", salary_raw="£28,000 a year",
                description="SQL, Excel, reporting.")])
            cur = await conn.execute("SELECT id FROM jobs LIMIT 1")
            job_id = (await cur.fetchone())[0]
            settings = Settings(db_path=tmp_path / "plan.db")
            return await start_application(conn, settings, job_id)
    plan = asyncio.run(_run())["plan"]
    assert "assessment_protocol" not in plan
    assert "statement_plan" not in plan
    assert not any("STATEMENT-ASSESSED" in c for c in plan["cautions"])


def test_same_project_told_twice_is_detected_even_in_different_words() -> None:
    """The real 2026-08-30 pair: the CharBT story appears in both texts."""
    personal = (
        "ETL pipeline development. In the same role I built reproducible "
        "Python/SQL workflows for data collection, transformation, historical "
        "validation and reporting. I treated pipelines as products: versioned, "
        "documented, re-runnable, with checks that fail loudly when the data "
        "drifts, and reconciliation against historical figures.")
    technical = (
        "Situation: analysts depended on manually refreshed extracts and "
        "figures frequently disagreed between reports.\n"
        "Action: I designed and built Python/SQL pipelines with a validation "
        "layer - row counts and reconciliation against historical figures - "
        "documented and version-controlled, so colleagues could re-run the "
        "chain.\nResult: recurring analysis became a one-command re-run.")
    review = review_statement(personal, kind="personal_statement",
                              word_limit=200, other_text=technical)
    assert review["stats"]["story_overlap_pct"] >= 30
    assert any(i["code"] == "same_example" for i in review["issues"])


def test_different_examples_do_not_trip_the_story_check() -> None:
    personal = (
        "Dashboards and reports. I delivered a monthly management pack in "
        "Power BI to thirty non-technical colleagues, replacing a manual "
        "spreadsheet cycle and cutting the reporting lag from nine days to "
        "two.")
    technical = (
        "Situation: our geotechnical field programme produced handwritten "
        "borehole logs that were transcribed twice.\nAction: I designed the "
        "digital capture sheet and the checks that rejected impossible "
        "readings before upload.\nResult: transcription errors fell to zero "
        "across 240 boreholes.\nThe programme passed its audit with no "
        "findings.")
    review = review_statement(personal, kind="personal_statement",
                              word_limit=200, other_text=technical)
    assert not any(i["code"] == "same_example" for i in review["issues"])


# ------------------------------------------------- AI-detector (Sapling) ----

GOOD_TEXT = (
    "SQL-based data transformation. In my most recent role I rebuilt the "
    "monthly reporting extracts in SQL, moving 14 manual spreadsheets into "
    "9 parameterised queries that reconciled 1.2m records per month; "
    "validation checks caught 37 upstream defects in the first quarter and "
    "cut preparation time from 6 hours to 20 minutes.")


def test_non_zero_ai_score_blocks_the_text() -> None:
    review = review_statement(GOOD_TEXT, word_limit=100, ai_score=18)
    assert review["verdict"] == "block"
    hit = [i for i in review["issues"] if i["code"] == "ai_score"]
    assert hit and "18" in hit[0]["message"]
    assert review["stats"]["ai_score"] == 18


def test_zero_ai_score_clears_the_gate() -> None:
    review = review_statement(GOOD_TEXT, word_limit=100, ai_score=0)
    assert not [i for i in review["issues"] if i["code"].startswith("ai_")]


def test_statement_assessed_text_cannot_pass_without_sapling() -> None:
    """ai_score_required is how a CSJ text is forced through the detector."""
    review = review_statement(GOOD_TEXT, word_limit=50,
                              ai_score_required=True)
    assert review["verdict"] == "revise"
    assert any(i["code"] == "ai_check_missing" for i in review["issues"])
    passed = review_statement(GOOD_TEXT, word_limit=50, ai_score=0,
                              ai_score_required=True)
    assert passed["verdict"] == "pass", passed["issues"]


def test_plan_demands_a_sapling_zero_for_every_text() -> None:
    ai = statement_plan()["ai_detection"]
    assert ai["tool"] == "mcp__sapling__aidetect" and ai["target_pct"] == 0
    assert "cover letter" in ai["applies_to"]
