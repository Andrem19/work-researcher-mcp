"""Regression tests for cross-board dedup.

Guards against the 2026-09-06 incident: Indeed URLs carry the job id in the
query string (?jk=...), and the old _url_key stripped the whole query — so
every Indeed observation canonicalised to the same URL and merged into the
most recent Indeed row, overwriting its title/company/salary (Evolve-Energy
was rewritten into Perch Group cards). Two layers must hold:
1. distinct job ids never resolve to one canonical row (dedup.py),
2. even a wrong merge cannot rewrite a stored row's identity (persistence.py).
"""

from contextlib import asynccontextmanager

import aiosqlite

from work_researcher.dedup import _url_key, resolution_map, resolve
from work_researcher.domain import JobCard
from work_researcher.persistence import SCHEMA, upsert_jobs
from work_researcher.textutils import job_hash

EVOLVE_HASH = "evolvehash"
PERCH_HASH = "perchhash"


def _pool_row(chash: str, url: str, title: str, company: str,
              source: str = "indeed", source_job_id: str | None = None) -> dict:
    return {"content_hash": chash, "source": source, "source_job_id": source_job_id,
            "url": url, "title": title, "company": company,
            "location_text": "Somewhere", "salary_min": None}


def _card(url: str, title: str, company: str, source: str = "indeed",
          source_job_id: str | None = None) -> JobCard:
    return JobCard(source=source, source_job_id=source_job_id, url=url,
                   title=title, company=company, location_text="Somewhere")


# ------------------------------------------------------------------ urls ----

def test_url_key_keeps_indeed_jk_identity() -> None:
    base = "https://uk.indeed.com/viewjob?jk=e4984e2dc3940cf7"
    assert _url_key(base) == "https://uk.indeed.com::jk=e4984e2dc3940cf7"
    # tracking params around it must not change identity
    noisy = "https://uk.indeed.com/viewjob?jk=e4984e2dc3940cf7&from=app&utm_medium=api"
    assert _url_key(noisy) == _url_key(base)


def test_url_key_distinct_indeed_jk_means_distinct_jobs() -> None:
    assert _url_key("https://uk.indeed.com/viewjob?jk=21d35007767e2066") \
        != _url_key("https://uk.indeed.com/viewjob?jk=e4984e2dc3940cf7")


def test_url_key_indeed_vjk_matches_viewjob_jk() -> None:
    search = "https://uk.indeed.com/jobs?q=data+analyst&l=Blackpool&vjk=e4984e2dc3940cf7"
    view = "https://uk.indeed.com/viewjob?jk=e4984e2dc3940cf7"
    assert _url_key(search) == _url_key(view)


def test_url_key_strips_tracking_keeps_identity_params() -> None:
    with_utm = "https://www.adzuna.co.uk/jobs/details/123?utm_medium=api&utm_source=x"
    assert _url_key(with_utm) == "https://www.adzuna.co.uk/jobs/details/123"
    plain = "https://www.adzuna.co.uk/jobs/details/123"
    assert _url_key(plain) == _url_key(with_utm)


def test_url_key_generic_unknown_id_stays_distinct() -> None:
    assert _url_key("https://example.com/jobs/aaa") != _url_key("https://example.com/jobs/bbb")


def test_url_key_none() -> None:
    assert _url_key(None) is None


# ---------------------------------------------------------------- resolve ----

def test_resolve_does_not_merge_indeed_cards_with_different_jk() -> None:
    """The exact incident: an Indeed card must not merge into a previous
    Indeed row just because both URLs canonicalise to /viewjob."""
    pool = [_pool_row(EVOLVE_HASH, "https://uk.indeed.com/viewjob?jk=21d35007767e2066",
                      "Quantitative Analyst", "Evolve-Energy")]
    perch = _card("https://uk.indeed.com/viewjob?jk=e4984e2dc3940cf7",
                  "Junior Data Engineer", "Perch Group")
    assert resolve(perch, PERCH_HASH, pool) is None


def test_resolve_merges_same_indeed_jk_with_different_tracking() -> None:
    pool = [_pool_row(EVOLVE_HASH, "https://uk.indeed.com/viewjob?jk=abc123",
                      "Junior Data Engineer", "Perch Group")]
    noisy = _card("https://uk.indeed.com/viewjob?jk=abc123&from=mobile",
                  "Junior Data Engineer", "Perch Group")
    assert resolve(noisy, job_hash("Junior Data Engineer", "Perch Group",
                                   "Somewhere", None), pool) == EVOLVE_HASH


def test_resolve_url_pass_skips_conflicting_company() -> None:
    pool = [_pool_row(EVOLVE_HASH, "https://uk.indeed.com/viewjob?jk=abc123",
                      "Analyst", "Evolve-Energy")]
    other = _card("https://uk.indeed.com/viewjob?jk=abc123", "Analyst", "Perch Group")
    assert resolve(other, "somehash", pool) is None


def test_resolve_fuzzy_still_merges_cross_board_duplicates() -> None:
    pool = [_pool_row(EVOLVE_HASH, "https://www.reed.co.uk/jobs/x/1",
                      "Junior Data Engineer", "Perch Group", source="reed")]
    twin = _card("https://www.totaljobs.com/job/x/2",
                 "Junior Data Engineer", "Perch Group Ltd", source="totaljobs")
    assert resolve(twin, "freshhash", pool) == EVOLVE_HASH


def test_resolve_fuzzy_blocks_different_company() -> None:
    pool = [_pool_row(EVOLVE_HASH, "https://www.reed.co.uk/jobs/x/1",
                      "Data Analyst", "Evolve-Energy", source="reed")]
    other = _card("https://www.totaljobs.com/job/x/2",
                  "Data Analyst", "Perch Group", source="totaljobs")
    assert resolve(other, "freshhash", pool) is None


# ------------------------------------------------------------------ db ----

@asynccontextmanager
async def _fresh_db(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "dedup.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    try:
        yield conn
    finally:
        await conn.close()


EVOLVE = JobCard(source="indeed", url="https://uk.indeed.com/viewjob?jk=21d35007767e2066",
                 title="Quantitative Analyst", company="Evolve-Energy",
                 location_text="Lytham St Annes FY8", salary_raw="£30,000 - £50,000 a year",
                 salary_min=30000, description="short")
PERCH = JobCard(source="totaljobs", url="https://www.totaljobs.com/job/perch/1",
                title="Junior Data Engineer", company="Perch Group",
                location_text="Blackpool FY4 5LW", salary_raw="£35,000 a year",
                salary_min=35000, description="a much longer description of the same "
                "merged row, because the longer description should win")


async def test_upsert_merge_cannot_rewrite_identity(tmp_path) -> None:
    async with _fresh_db(tmp_path) as conn:
        await upsert_jobs(conn, [EVOLVE])
        perch_hash = job_hash(PERCH.title, PERCH.company, PERCH.location_text,
                              PERCH.salary_min)
        evolve_hash = job_hash(EVOLVE.title, EVOLVE.company, EVOLVE.location_text,
                               EVOLVE.salary_min)
        # force the (wrong) merge the old bug would have produced
        await upsert_jobs(conn, [PERCH], {perch_hash: evolve_hash})
        cur = await conn.execute(
            "SELECT title, company, salary_raw, url, description FROM jobs WHERE id=?",
            (f"job_{evolve_hash}",))
        row = await cur.fetchone()
        assert row["title"] == "Quantitative Analyst"
        assert row["company"] == "Evolve-Energy"
        assert row["salary_raw"] == "£30,000 - £50,000 a year"
        assert row["url"].endswith("jk=21d35007767e2066")
        # backfill rules still work: longer description wins
        assert row["description"].startswith("a much longer description")
        cur = await conn.execute(
            "SELECT source FROM job_sources WHERE content_hash=?", (evolve_hash,))
        assert {r["source"] for r in await cur.fetchall()} == {"indeed", "totaljobs"}


async def test_upsert_merge_backfills_null_fields(tmp_path) -> None:
    sparse = JobCard(source="reed", url="https://www.reed.co.uk/jobs/x/1",
                     title="Data Analyst", company="Acme",
                     location_text="Preston", salary_raw=None, salary_min=None,
                     description=None)
    richer = JobCard(source="totaljobs", url="https://www.totaljobs.com/job/x/2",
                     title="Data Analyst", company="Acme",
                     location_text="Preston", salary_raw="£30,000 a year",
                     salary_min=30000, description="real description")
    async with _fresh_db(tmp_path) as conn:
        await upsert_jobs(conn, [sparse])
        rhash = job_hash("Data Analyst", "Acme", "Preston", None)
        # the incoming card's own hash (salary_min set) must be the resolution
        # KEY; rhash is the canonical row it is forced to merge into
        richer_hash = job_hash("Data Analyst", "Acme", "Preston", 30000.0)
        assert richer_hash != rhash
        await upsert_jobs(conn, [richer], {richer_hash: rhash})
        cur = await conn.execute(
            "SELECT salary_raw, salary_min, description FROM jobs WHERE id=?",
            (f"job_{rhash}",))
        row = await cur.fetchone()
        assert row["salary_raw"] == "£30,000 a year"
        assert row["salary_min"] == 30000
        assert row["description"] == "real description"
        # identity survives even when the incoming card would disagree
        clashing = JobCard(source="indeed", url="https://uk.indeed.com/viewjob?jk=zzz",
                           title="Totally Different Job", company="Other Ltd",
                           location_text="Nowhere", salary_raw="£1 a year",
                           salary_min=1, description=None)
        clash_hash = job_hash(clashing.title, clashing.company,
                              clashing.location_text, clashing.salary_min)
        await upsert_jobs(conn, [clashing], {clash_hash: rhash})
        cur = await conn.execute(
            "SELECT title, company, salary_raw FROM jobs WHERE id=?", (f"job_{rhash}",))
        row = await cur.fetchone()
        assert (row["title"], row["company"], row["salary_raw"]) \
            == ("Data Analyst", "Acme", "£30,000 a year")


async def test_end_to_end_indeed_observations_get_separate_rows(tmp_path) -> None:
    """Full incident replay through the observation path: two Indeed cards
    with different jk params must land on two rows, whatever was ingested
    before them."""
    async with _fresh_db(tmp_path) as conn:
        earlier = JobCard(source="indeed",
                          url="https://uk.indeed.com/viewjob?jk=olderjob",
                          title="Quantitative Analyst", company="Evolve-Energy",
                          location_text="Lytham St Annes FY8", salary_min=30000)
        await upsert_jobs(conn, [earlier])
        pool_rows = await conn.execute(
            "SELECT id, content_hash, source, source_job_id, url, title, company, "
            "location_text, salary_min FROM jobs")
        pool = [dict(r) for r in await pool_rows.fetchall()]
        cards = [
            JobCard(source="indeed", url="https://uk.indeed.com/viewjob?jk=perchjk",
                    title="Junior Data Engineer", company="Perch Group",
                    location_text="Blackpool FY4 5LW", salary_min=35000),
            JobCard(source="indeed", url="https://uk.indeed.com/viewjob?jk=anotherjk",
                    title="Data Warehouse Developer", company="Perch Group",
                    location_text="Blackpool FY4 5LW", salary_min=55000),
        ]
        res, merged = resolution_map(cards, pool)
        assert merged == 0
        perch_hash = job_hash("Junior Data Engineer", "Perch Group",
                              "Blackpool FY4 5LW", 35000.0)
        dwd_hash = job_hash("Data Warehouse Developer", "Perch Group",
                            "Blackpool FY4 5LW", 55000.0)
        earlier_hash = job_hash("Quantitative Analyst", "Evolve-Energy",
                                "Lytham St Annes FY8", 30000.0)
        # each new card maps to itself; neither touches the earlier row
        assert res[perch_hash] == perch_hash
        assert res[dwd_hash] == dwd_hash
        assert perch_hash != dwd_hash != earlier_hash
        await upsert_jobs(conn, cards, res)
        cur = await conn.execute("SELECT id, title, company FROM jobs ORDER BY title")
        rows = {r["id"]: (r["title"], r["company"]) for r in await cur.fetchall()}
        assert rows[f"job_{job_hash('Junior Data Engineer', 'Perch Group', 'Blackpool FY4 5LW', 35000.0)}"] \
            == ("Junior Data Engineer", "Perch Group")
        assert rows[f"job_{job_hash('Data Warehouse Developer', 'Perch Group', 'Blackpool FY4 5LW', 55000.0)}"] \
            == ("Data Warehouse Developer", "Perch Group")
        assert rows[f"job_{job_hash('Quantitative Analyst', 'Evolve-Energy', 'Lytham St Annes FY8', 30000.0)}"] \
            == ("Quantitative Analyst", "Evolve-Energy")
