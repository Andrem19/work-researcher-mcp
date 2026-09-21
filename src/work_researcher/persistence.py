"""SQLite persistence: jobs, searches, applications, CVs, observations.

One connection per operation (aiosqlite); WAL mode keeps concurrent harness
sessions safe. Migrations are idempotent CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .domain import JobCard
from .textutils import annualise, job_hash, now_iso, parse_dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source TEXT NOT NULL,
    source_job_id TEXT,
    url TEXT,
    apply_url TEXT,
    title TEXT,
    company TEXT,
    location_text TEXT,
    salary_raw TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_period TEXT,
    contract_type TEXT,
    work_from_home INTEGER,
    description TEXT,
    posted_at TEXT,
    fetched_at TEXT,
    latitude REAL,
    longitude REAL,
    extra TEXT
);
CREATE TABLE IF NOT EXISTS job_sources (
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (content_hash, source)
);
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    query TEXT NOT NULL,
    params TEXT NOT NULL,
    stats TEXT
);
CREATE TABLE IF NOT EXISTS search_results (
    search_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (search_id, job_id)
);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    cv_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submitted_at TEXT,
    notes TEXT,
    cover_letter TEXT,
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS cvs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT,
    size INTEGER,
    mtime TEXT,
    text_preview TEXT,
    full_text TEXT,
    tags TEXT,
    language TEXT,
    name_guess TEXT,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    task_id TEXT,
    observation_type TEXT,
    source TEXT,
    count INTEGER,
    search_id TEXT
);
CREATE TABLE IF NOT EXISTS locations (
    place TEXT PRIMARY KEY,
    lat REAL,
    lon REAL,
    resolved_name TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS blocklist (
    kind TEXT NOT NULL,            -- company | keyword
    value TEXT NOT NULL,
    normalized TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, normalized)
);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash);
CREATE INDEX IF NOT EXISTS idx_apps_job ON applications(job_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class connect:  # noqa: N801 - async context manager factory
    """`async with connect(db_path) as conn:` — opens WAL, applies the schema,
    commits and closes on exit."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        assert self._conn is not None
        try:
            await self._conn.commit()
        finally:
            await self._conn.close()
        return False


async def init_db(db_path: Path) -> None:
    async with connect(db_path):
        pass


# ---------------------------------------------------------------- jobs ----
def _card_row(card: JobCard, chash: str) -> tuple:
    extra = json.dumps(card.extra, ensure_ascii=False, default=str)
    return (
        f"job_{chash}", chash, now_iso(), now_iso(),
        card.source, card.source_job_id, card.url, card.apply_url,
        card.title, card.company, card.location_text, card.salary_raw,
        card.salary_min, card.salary_max, card.salary_period, card.contract_type,
        1 if card.work_from_home else 0 if card.work_from_home is not None else None,
        card.description,
        card.posted_at.isoformat() if card.posted_at else None,
        now_iso(),
        card.extra.get("latitude"), card.extra.get("longitude"),
        extra,
    )


_COLS = (
    "id, content_hash, first_seen, last_seen, source, source_job_id, url, apply_url, "
    "title, company, location_text, salary_raw, salary_min, salary_max, salary_period, "
    "contract_type, work_from_home, description, posted_at, fetched_at, "
    "latitude, longitude, extra"
)


async def upsert_jobs(conn: aiosqlite.Connection, cards: list[JobCard],
                      resolution: dict[str, str] | None = None) -> dict[str, Any]:
    """Store cards, merging duplicates per the dedup resolution map.

    resolution maps each card's content_hash -> canonical content_hash (itself
    when new). All cards sharing a canonical hash land on one job row; extra
    sources are recorded in job_sources. Returns {card_hash: (job_id, is_new)}.
    """
    out: dict[str, Any] = {}
    resolution = resolution or {}
    created_here: dict[str, JobCard] = {}  # canonical_hash -> representative card
    for card in cards:
        chash = job_hash(card.title, card.company, card.location_text, card.salary_min)
        canon = resolution.get(chash, chash)
        if canon not in created_here:
            created_here[canon] = card
        rep = created_here[canon]
        cur = await conn.execute("SELECT id FROM jobs WHERE content_hash=?", (canon,))
        existing = await cur.fetchone()
        if existing is not None:
            job_id, is_new = existing[0], False
        else:
            job_id, is_new = f"job_{canon}", True
        if is_new:
            row = _card_row(rep, canon)
            await conn.execute(
                f"INSERT INTO jobs ({_COLS}) VALUES ({','.join('?' * 23)})", row
            )
        else:
            # Non-destructive merge: a duplicate card may only FILL fields the
            # canonical row is missing (description: keep the longer text).
            # Existing identity fields are never overwritten — a wrong dedup
            # match must not be able to rewrite a stored vacancy (that once
            # turned one employer's row into another's). extra JSON still
            # refreshes location intel in place and backfills the rest.
            cur2 = await conn.execute("SELECT extra FROM jobs WHERE id=?", (job_id,))
            old_row = await cur2.fetchone()
            old_extra = {}
            if old_row and old_row["extra"]:
                try:
                    old_extra = json.loads(old_row["extra"])
                except (TypeError, ValueError):
                    old_extra = {}
            merged_extra = {**old_extra}
            for k, v in card.extra.items():
                if v is None:
                    continue
                # location intel refreshes in place (unknown → resolved later)
                if k in ("work_mode", "distance_miles", "location_status",
                         "location_reason", "posted_by", "posted_by_reason",
                         "training_offer", "training_reason") \
                        or old_extra.get(k) is None:
                    merged_extra[k] = v
            await conn.execute(
                """UPDATE jobs SET last_seen=?,
                   url=COALESCE(url,?), apply_url=COALESCE(apply_url,?),
                   title=CASE WHEN COALESCE(title,'')='' THEN ? ELSE title END,
                   company=CASE WHEN COALESCE(company,'')='' THEN ? ELSE company END,
                   location_text=CASE WHEN COALESCE(location_text,'')=''
                                      THEN ? ELSE location_text END,
                   salary_raw=CASE WHEN COALESCE(salary_raw,'')=''
                                   THEN ? ELSE salary_raw END,
                   salary_min=COALESCE(salary_min,?), salary_max=COALESCE(salary_max,?),
                   salary_period=COALESCE(salary_period,?),
                   contract_type=CASE WHEN COALESCE(contract_type,'')=''
                                      THEN ? ELSE contract_type END,
                   work_from_home=COALESCE(work_from_home,?),
                   description=CASE WHEN length(?)>length(COALESCE(description,''))
                                    THEN ? ELSE description END,
                   posted_at=COALESCE(posted_at,?), fetched_at=?, extra=?
                   WHERE id=?""",
                (now_iso(), card.url, card.apply_url, card.title, card.company,
                 card.location_text, card.salary_raw, card.salary_min, card.salary_max,
                 card.salary_period, card.contract_type,
                 1 if card.work_from_home else None,
                 card.description or "", card.description,
                 card.posted_at.isoformat() if card.posted_at else None,
                 now_iso(), json.dumps(merged_extra, ensure_ascii=False, default=str),
                 job_id),
            )
        out[chash] = (job_id, is_new)
        await conn.execute(
            """INSERT INTO job_sources (content_hash, source, source_url, first_seen, last_seen)
               VALUES (?,?,?,?,?)
               ON CONFLICT(content_hash, source) DO UPDATE SET
                   last_seen=excluded.last_seen,
                   source_url=COALESCE(excluded.source_url, source_url)""",
            (canon, card.source, card.url, now_iso(), now_iso()),
        )
    return out


async def create_search(conn: aiosqlite.Connection, search_id: str, params, stats: dict) -> None:
    await conn.execute(
        "INSERT INTO searches (id, created_at, query, params, stats) VALUES (?,?,?,?,?)",
        (search_id, now_iso(), params.query, params.model_dump_json(), json.dumps(stats)),
    )


async def add_search_results(conn: aiosqlite.Connection, search_id: str,
                              rows: list[tuple[str, float, int]]) -> None:
    await conn.executemany(
        "INSERT OR REPLACE INTO search_results (search_id, job_id, score, rank) VALUES (?,?,?,?)",
        [(search_id, j, s, r) for j, s, r in rows],
    )


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["work_from_home"] = bool(d["work_from_home"]) if d["work_from_home"] is not None else None
    return d


async def get_search_results(conn: aiosqlite.Connection, search_id: str, limit: int,
                              offset: int = 0) -> tuple[list[dict], int]:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM search_results WHERE search_id=?", (search_id,)
    )
    total = (await cur.fetchone())[0]
    cur = await conn.execute(
        """SELECT j.*, sr.score, sr.rank,
                  (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS app_count,
                  (SELECT a.status FROM applications a WHERE a.job_id=j.id
                    ORDER BY a.updated_at DESC LIMIT 1) AS app_status,
                  (SELECT a.submitted_at FROM applications a WHERE a.job_id=j.id
                    ORDER BY a.updated_at DESC LIMIT 1) AS app_date
           FROM search_results sr JOIN jobs j ON j.id=sr.job_id
           WHERE sr.search_id=? ORDER BY sr.rank LIMIT ? OFFSET ?""",
        (search_id, limit, offset),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    return rows, total


async def find_applications_like(conn: aiosqlite.Connection, url: str | None = None,
                                 title: str | None = None,
                                 company: str | None = None) -> list[dict]:
    """Application history lookup by URL and/or fuzzy title+company.

    Used by the agent to check 'have we already applied here?' before
    starting a browser application on any site.
    """
    from rapidfuzz import fuzz

    def norm(s: str | None) -> str:
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()

    cur = await conn.execute(
        """SELECT a.*, j.title AS job_title, j.company, j.url AS job_url,
                  j.location_text, j.content_hash
           FROM applications a JOIN jobs j ON j.id=a.job_id
           ORDER BY a.updated_at DESC LIMIT 1000"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    url_key = _url_norm(url)
    t, c = norm(title), norm(company)
    if not (url_key or t):
        return []
    out = []
    for r in rows:
        if url_key and _url_norm(r.get("job_url")) == url_key:
            out.append(r)
            continue
        if t:
            rt, rc = norm(r.get("job_title")), norm(r.get("company"))
            title_r = fuzz.token_set_ratio(t, rt) if rt else 0
            company_r = fuzz.token_set_ratio(c, rc) if (c and rc) else (
                100 if not (c or rc) else 0
            )
            if title_r >= 80 and company_r >= 70:
                out.append(r)
    return out[:20]


def _url_norm(url: str | None) -> str | None:
    if not url:
        return None
    u = re.sub(r"[?#].*$", "", url.strip().rstrip("/")).lower()
    return u


async def get_job(conn: aiosqlite.Connection, job_id: str) -> dict | None:
    cur = await conn.execute(
        """SELECT j.*, (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS app_count
           FROM jobs j WHERE j.id=?""",
        (job_id,),
    )
    row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def job_sources(conn: aiosqlite.Connection, content_hash: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT source, source_url, first_seen, last_seen FROM job_sources WHERE content_hash=?",
        (content_hash,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def sources_for_hashes(conn: aiosqlite.Connection,
                             hashes: list[str]) -> dict[str, list[dict]]:
    if not hashes:
        return {}
    marks = ",".join("?" * len(hashes))
    cur = await conn.execute(
        f"SELECT content_hash, source, source_url FROM job_sources WHERE content_hash IN ({marks})",  # noqa: S608
        hashes,
    )
    out: dict[str, list[dict]] = {}
    for r in await cur.fetchall():
        out.setdefault(r["content_hash"], []).append(
            {"source": r["source"], "source_url": r["source_url"]}
        )
    return out


# -------------------------------------------------------- applications ----
APP_STATUSES = {
    "planned", "applying", "submitted", "interview", "offer",
    "rejected", "withdrawn", "failed",
}


async def create_application(conn: aiosqlite.Connection, job_id: str,
                              cv_id: str | None, notes: str | None) -> str:
    app_id = new_id("app")
    await conn.execute(
        """INSERT INTO applications (id, job_id, cv_id, status, created_at, updated_at, notes)
           VALUES (?,?,?,?,?,?,?)""",
        (app_id, job_id, cv_id, "planned", now_iso(), now_iso(), notes),
    )
    return app_id


async def update_application(conn: aiosqlite.Connection, app_id: str,
                              status: str | None = None, notes: str | None = None,
                              cover_letter: str | None = None,
                              evidence: dict | None = None) -> dict | None:
    cur = await conn.execute("SELECT * FROM applications WHERE id=?", (app_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    sets, vals = ["updated_at=?"], [now_iso()]
    if status:
        if status not in APP_STATUSES:
            raise ValueError(f"status must be one of {sorted(APP_STATUSES)}")
        sets.append("status=?")
        vals.append(status)
        if status == "submitted" and not d.get("submitted_at"):
            sets.append("submitted_at=?")
            vals.append(now_iso())
    if notes is not None:
        sets.append("notes=?")
        vals.append(notes)
    if cover_letter is not None:
        sets.append("cover_letter=?")
        vals.append(cover_letter)
    if evidence is not None:
        merged = json.loads(d.get("evidence") or "{}")
        merged.update(evidence)
        sets.append("evidence=?")
        vals.append(json.dumps(merged, ensure_ascii=False))
    vals.append(app_id)
    await conn.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id=?", vals)
    cur = await conn.execute(
        """SELECT a.*, j.title AS job_title, j.company AS company
           FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?""",
        (app_id,),
    )
    return dict(await cur.fetchone())


async def list_applications(conn: aiosqlite.Connection,
                            status: str | None = None, limit: int = 50) -> list[dict]:
    q = (
        "SELECT a.*, j.title AS job_title, j.company AS company, j.url AS job_url "
        "FROM applications a JOIN jobs j ON j.id=a.job_id"
    )
    args: list = []
    if status:
        q += " WHERE a.status=?"
        args.append(status)
    q += " ORDER BY a.updated_at DESC LIMIT ?"
    args.append(limit)
    cur = await conn.execute(q, args)
    return [dict(r) for r in await cur.fetchall()]


async def application_for_job(conn: aiosqlite.Connection, job_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM applications WHERE job_id=? ORDER BY updated_at DESC LIMIT 1",
        (job_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_application(conn: aiosqlite.Connection,
                          app_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT a.*, j.title AS job_title, j.company AS company "
        "FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (app_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------- cvs ----
async def upsert_cv(conn: aiosqlite.Connection, rec: dict) -> str:
    cv_id = rec.get("id") or new_id("cv")
    await conn.execute(
        """INSERT INTO cvs (id, filename, path, sha256, size, mtime,
                            text_preview, full_text, tags, language,
                            name_guess, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
               filename=excluded.filename, sha256=excluded.sha256, size=excluded.size,
               mtime=excluded.mtime,
               text_preview=excluded.text_preview, full_text=excluded.full_text,
               tags=excluded.tags, language=excluded.language,
               name_guess=excluded.name_guess, indexed_at=excluded.indexed_at""",
        (cv_id, rec["filename"], rec["path"], rec.get("sha256"), rec.get("size"),
         rec.get("mtime"), rec.get("text_preview"), rec.get("full_text"),
         rec.get("tags"), rec.get("language"), rec.get("name_guess"), now_iso()),
    )
    return cv_id


async def list_cvs(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, filename, path, sha256, size, mtime, text_preview, tags, "
        "language, name_guess, indexed_at FROM cvs ORDER BY filename"
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        d.pop("full_text", None)  # keep listings light
        rows.append(d)
    return rows


async def get_cv(conn: aiosqlite.Connection, cv_id: str) -> dict | None:
    cur = await conn.execute("SELECT * FROM cvs WHERE id=? OR path=?", (cv_id, cv_id))
    row = await cur.fetchone()
    return dict(row) if row else None


# --------------------------------------------------------- observations ----
async def log_observation(conn: aiosqlite.Connection, task_id: str | None,
                          observation_type: str, source: str | None, count: int,
                          search_id: str | None) -> None:
    await conn.execute(
        """INSERT INTO observations (observed_at, task_id, observation_type, source, count, search_id)
           VALUES (?,?,?,?,?,?)""",
        (now_iso(), task_id, observation_type, source, count, search_id),
    )


# ------------------------------------------------------------ blocklist ----
_COMPANY_NOISE = re.compile(
    r"\b(ltd|limited|plc|llp|inc|co|company|uk|group|recruitment|consultancy|"
    r"consulting|solutions|services|staffing|partners)\b", re.I)


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    low = name.lower()
    low = _COMPANY_NOISE.sub(" ", low)
    return re.sub(r"[^a-z0-9 ]", " ", low).strip()


async def ensure_seed_blocklist(conn: aiosqlite.Connection, settings) -> int:
    """Merge [blocklist] config entries into the DB once (idempotent)."""
    added = 0
    for value in settings.blocklist_companies or []:
        value = str(value).strip()
        if not value:
            continue
        cur = await conn.execute(
            "SELECT 1 FROM blocklist WHERE kind='company' AND normalized=?",
            (normalize_company(value),),
        )
        if await cur.fetchone() is None:
            await conn.execute(
                "INSERT OR IGNORE INTO blocklist (kind, value, normalized, reason, created_at) "
                "VALUES ('company', ?, ?, 'config.toml seed', ?)",
                (value, normalize_company(value), now_iso()),
            )
            added += 1
    return added


async def blocklist_add(conn: aiosqlite.Connection, kind: str, value: str,
                        reason: str | None) -> bool:
    norm = normalize_company(value) if kind == "company" else value.lower().strip()
    if not norm:
        return False
    cur = await conn.execute(
        "SELECT 1 FROM blocklist WHERE kind=? AND normalized=?", (kind, norm)
    )
    exists = await cur.fetchone() is not None
    await conn.execute(
        "INSERT OR IGNORE INTO blocklist (kind, value, normalized, reason, created_at) "
        "VALUES (?,?,?,?,?)",
        (kind, value.strip(), norm, reason, now_iso()),
    )
    return not exists


async def blocklist_remove(conn: aiosqlite.Connection, kind: str, value: str) -> bool:
    norm = normalize_company(value) if kind == "company" else value.lower().strip()
    cur = await conn.execute(
        "DELETE FROM blocklist WHERE kind=? AND normalized=?", (kind, norm)
    )
    return cur.rowcount > 0


async def blocklist_list(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        "SELECT kind, value, reason, created_at FROM blocklist ORDER BY kind, value"
    )
    return [dict(r) for r in await cur.fetchall()]


async def load_blocked_norms(conn: aiosqlite.Connection) -> tuple[set[str], set[str]]:
    cur = await conn.execute("SELECT kind, normalized FROM blocklist")
    companies: set[str] = set()
    keywords: set[str] = set()
    for r in await cur.fetchall():
        (companies if r["kind"] == "company" else keywords).add(r["normalized"])
    return companies, keywords


def is_blocked(company: str | None, text: str | None,
               companies: set[str], keywords: set[str]) -> str | None:
    """Return the matching blocklist entry reason-key if blocked, else None."""
    norm = normalize_company(company)
    if norm and norm in companies:
        return f"company:{norm}"
    if text:
        low = text.lower()
        for kw in keywords:
            if kw in low:
                return f"keyword:{kw}"
    return None


# ------------------------------------------------------------- helpers ----
def brief_from_row(row: dict, rank: int, is_new: bool, sources: list[str]) -> dict:
    posted = parse_dt(row.get("posted_at"))
    extra = {}
    if row.get("extra"):
        try:
            extra = json.loads(row["extra"])
        except (TypeError, ValueError):
            extra = {}
    return {
        "job_id": row["id"], "rank": rank, "score": round(row.get("score", 0.0), 1),
        "is_new": is_new, "title": row.get("title"), "company": row.get("company"),
        "location_text": row.get("location_text"), "salary_raw": row.get("salary_raw"),
        "salary_annum": annualise(row.get("salary_min"), row.get("salary_period"))
        if row.get("salary_min") else None,
        "work_from_home": row.get("work_from_home"), "posted_at": posted,
        "sources": sources, "url": row.get("apply_url") or row.get("url"),
        "apply_method": row.get("extra_apply_method"),
        "already_applied": bool(row.get("app_count")),
        "application_status": row.get("app_status"),
        "application_date": row.get("app_date"),
        "work_mode": extra.get("work_mode"),
        "distance_miles": extra.get("distance_miles"),
        "location_status": extra.get("location_status"),
        "location_reason": extra.get("location_reason"),
        "posted_by": extra.get("posted_by"),
        "posted_by_reason": extra.get("posted_by_reason"),
        "training_offer": extra.get("training_offer", False) or False,
        "requirements_status": extra.get("requirements_status"),
        "requirements_unmet": extra.get("requirements_unmet"),
        "description_excerpt": (extra.get("description") or "")[:200] or None
        if extra.get("description") else None,
    }


async def count_rows(conn: aiosqlite.Connection, table: str) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed table list
    return (await cur.fetchone())[0]


def days_ago_iso(days: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")
