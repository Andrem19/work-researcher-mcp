"""Location intelligence: home base, geocoding, commute checks.

Rules the agent relies on:
- remote / UK-wide-travel jobs: distance is irrelevant — searched UK-wide;
- hybrid / on-site / field jobs: must be within max_commute_miles of home
  (or explicitly relocatable), otherwise flagged location_status=mismatch
  and heavily penalised in ranking;
- each candidate's home/relocate settings live in the active profile's
  config.toml [applicant] and can change any time.

Geocoding: postcodes.io (free, no key) — places endpoint for town/city
names, postcode endpoint when the text carries a postcode. Results cached
in the locations table; unknown places degrade to location_status=unknown
(never a hard failure).
"""

from __future__ import annotations

import math
import re

import httpx

from .config import Settings

UA = "work-researcher-mcp/0.1 (job-search agent; contact via config)"

REMOTE_PAT = re.compile(
    r"\b(remote|work from home|wfh|home[ -]based|fully remote|100% remote|"
    r"anywhere in the uk|uk[ -]wide|nationwide|national coverage)\b", re.I)
HYBRID_PAT = re.compile(r"\b(hybrid|part[ -]remote|2 days? (?:a week|per week) (?:in|office)|"
                        r"mixed (?:home|office))\b", re.I)
ONSITE_PAT = re.compile(r"\b(on[ -]site|office[ -]based|site[ -]based|in[ -]person|"
                        r"full[ -]time on site)\b", re.I)
FIELD_PAT = re.compile(r"\b(field[ -]based|site walk|site visits|fieldwork|"
                       r"field engineer|site investigation|travel(?:ling)? (?:across|"
                       r"throughout) (?:the )?(?:uk|region|country))\b", re.I)
POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
OUTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b")

UK_REGIONS = {
    "north west": 25, "lancashire": 30, "cumbria": 55, "greater manchester": 35,
    "merseyside": 30, "yorkshire": 60, "north east": 80, "midlands": 90,
    "west midlands": 85, "east midlands": 90, "east anglia": 140, "east": 140,
    "london": 210, "south east": 210, "south west": 190, "wales": 100,
    "scotland": 150, "northern ireland": 200,
}


def classify_work_mode(title: str | None, description: str | None) -> str | None:
    text = f"{title or ''} {description or ''}"
    if REMOTE_PAT.search(text):
        # "hybrid (2 days remote)" also matches remote-ish words — check hybrid first
        if HYBRID_PAT.search(text):
            return "hybrid"
        return "remote"
    if HYBRID_PAT.search(text):
        return "hybrid"
    if FIELD_PAT.search(text):
        return "field"
    if ONSITE_PAT.search(text):
        return "on_site"
    return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def geocode(settings: Settings, place: str | None,
                  conn=None) -> dict | None:
    """Best-effort UK geocode: {lat, lon, name}. Cached in the locations table.

    Tries: full postcode → outcode (district) → cleaned place name (parentheses
    like 'Glasgow (G1)' are stripped first).
    """
    if not place or not place.strip():
        return None
    place = re.sub(r"\s+", " ", place).strip()
    cleaned = re.sub(r"\s*\([^)]*\)", "", place).strip() or place
    cached = None
    if conn is not None:
        cur = await conn.execute(
            "SELECT lat, lon, resolved_name FROM locations WHERE place=?", (place.lower(),)
        )
        row = await cur.fetchone()
        if row and row["lat"] is not None:
            cached = {"lat": row["lat"], "lon": row["lon"], "name": row["resolved_name"]}
    if cached is not None:
        return cached

    async def _store(lat, lon, name):
        if conn is not None and lat is not None:
            await conn.execute(
                "INSERT OR REPLACE INTO locations (place, lat, lon, resolved_name, fetched_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (place.lower(), lat, lon, name),
            )

    full_pc = POSTCODE_RE.search(place)
    outcode = OUTCODE_RE.search(place) if not full_pc else None

    def _district_name(res: dict) -> str:
        # /postcodes returns admin_district as str; /outcodes as list
        d = res.get("admin_district")
        if isinstance(d, list):
            return d[0] if d else place
        return d or place

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=8.0, follow_redirects=True
        ) as client:
            if full_pc:
                resp = await client.get(
                    f"https://api.postcodes.io/postcodes/"
                    f"{full_pc.group(1).replace(' ', '').upper()}"
                )
                if resp.status_code == 200:
                    res = resp.json().get("result")
                    if res:
                        name = _district_name(res)
                        await _store(res["latitude"], res["longitude"], name)
                        return {"lat": res["latitude"], "lon": res["longitude"],
                                "name": name}
            if outcode and len(outcode.group(1)) >= 2:
                code = outcode.group(1).upper()
                if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", code):
                    resp = await client.get(f"https://api.postcodes.io/outcodes/{code}")
                    if resp.status_code == 200:
                        res = resp.json().get("result")
                        if res:
                            name = _district_name(res)
                            await _store(res["latitude"], res["longitude"], name)
                            return {"lat": res["latitude"], "lon": res["longitude"],
                                    "name": name}
            for candidate in (cleaned, cleaned.split(",")[0].strip()):
                if not candidate:
                    continue
                resp = await client.get(
                    "https://api.postcodes.io/places", params={"q": candidate, "limit": 1}
                )
                if resp.status_code == 200:
                    results = resp.json().get("result", {})
                    places = results.get("result") if isinstance(results, dict) else results
                    if places:
                        p0 = places[0] if isinstance(places, list) else places
                        await _store(p0["latitude"], p0["longitude"],
                                     p0.get("name_1") or place)
                        return {"lat": p0["latitude"], "lon": p0["longitude"],
                                "name": p0.get("name_1") or place}
    except Exception:  # noqa: BLE001 - geocoding is best-effort
        pass
    return None  # failures are NOT cached — retry on the next search


def region_distance_hint(location_text: str, home_region_hints: list[str]) -> int | None:
    """Fallback when geocoding fails: rough miles from known UK regions."""
    low = (location_text or "").lower()
    for region, miles in UK_REGIONS.items():
        if region in low:
            return miles
    return None


def evaluate_location(
    *,
    work_mode: str | None,
    job_lat: float | None,
    job_lon: float | None,
    job_location: str | None,
    home_lat: float | None,
    home_lon: float | None,
    home_location: str | None,
    max_commute_miles: int,
    willing_to_relocate: bool,
    relocate_areas: list[str] | None = None,
    location_policy: str = "auto",
    daily_commute_miles: int = 25,
    occasional_commute_miles: int = 50,
) -> dict:
    """Return {work_mode, distance_miles, location_status, reason}.

    The commute limit is work-mode-aware (the user's rule from the real run):
    - remote: distance irrelevant, searched UK-wide
    - on_site (daily office): must be within daily_commute_miles (default 25)
    - hybrid / field / unknown: within occasional_commute_miles (default 50)
    - 'uk_wide' policy ignores distance; 'commute_only' drops far non-remote jobs
    """
    relocate_areas = relocate_areas or []
    if work_mode in ("remote",):
        return {"work_mode": work_mode, "distance_miles": None, "location_status": "ok",
                "reason": "remote — location irrelevant, searched UK-wide"}
    if location_policy == "uk_wide":
        dist = None
        if job_lat is not None and home_lat is not None:
            dist = round(_haversine(home_lat, home_lon, job_lat, job_lon))
        return {"work_mode": work_mode, "distance_miles": dist,
                "location_status": "ok", "reason": "uk_wide policy — distance ignored"}

    # pick the commute threshold by work mode
    if work_mode == "on_site":
        limit = daily_commute_miles
        limit_label = f"daily commute {daily_commute_miles}"
    else:  # hybrid, field, unknown → occasional (1-2 days/week or travel)
        limit = occasional_commute_miles
        limit_label = f"occasional commute {occasional_commute_miles}"

    dist = None
    if job_lat is not None and home_lat is not None:
        dist = round(_haversine(home_lat, home_lon, job_lat, job_lon))
    else:
        dist = region_distance_hint(job_location, [home_location or ""])

    if dist is None:
        return {"work_mode": work_mode, "distance_miles": None,
                "location_status": "unknown",
                "reason": "could not geocode job location — ask the user if unsure"}

    near = dist <= limit
    if near:
        return {"work_mode": work_mode, "distance_miles": dist, "location_status": "ok",
                "reason": f"{dist} mi from {home_location} (within {limit_label})"}
    if relocate_areas:
        low = (job_location or "").lower()
        if any(a.lower() in low for a in relocate_areas):
            return {"work_mode": work_mode, "distance_miles": dist,
                    "location_status": "ok",
                    "reason": f"{dist} mi but in a planned relocation area"}
    if willing_to_relocate and work_mode in ("hybrid", "on_site", "field", None):
        return {"work_mode": work_mode, "distance_miles": dist,
                "location_status": "caution",
                "reason": f"{dist} mi from {home_location} — relocation required "
                          f"(beyond {limit_label}; user is willing, confirm)"}
    return {"work_mode": work_mode, "distance_miles": dist,
            "location_status": "mismatch",
            "reason": f"{dist} mi from {home_location} exceeds {limit_label} "
                      f"and job is not remote"}


async def home_geo(settings: Settings, conn=None) -> dict | None:
    home = settings.applicant.get("home_location") or ""
    pc = settings.applicant.get("home_postcode") or ""
    for candidate in (pc, home):
        if not candidate:
            continue
        geo = await geocode(settings, candidate, conn)
        if geo:
            return geo
    return None
