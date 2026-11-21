"""The Explore query (J5), shared by GET /api/explore and the saved searches of J6, so a scan applies exactly the rule the page shows."""
import re
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import jobfit
from app.services.geo import JOB_INDIA, REMOTE, classify_geo
from app.services.pipeline import INDIA

RX = INDIA.pattern.replace(r"\b", r"\y")  # Postgres ARE: \y is the word boundary, \b is backspace
EV_INDIA = "(coalesce(e.raw_metadata->>'region', '') = 'india' OR coalesce(e.location, '') ~* :rx)"  # coalesce: NULL would make NOT(...) drop the row
JOB_FROM = """source_events e JOIN source_registry r ON r.id = e.source_id
    LEFT JOIN ats_boards b ON r.connector_type = 'api' AND r.name = b.provider || ':' || b.board_token"""


async def _q(s: AsyncSession, sql: str, **p) -> list[dict]:
    return [dict(x._mapping) for x in await s.execute(text(sql), {"rx": RX, **p})]


EXPLORE_SCAN = 5000  # newest job events read per request; Python then applies the exact geo rule
REMOTE_RX = REMOTE.pattern.replace(r"\b", r"\y")
JOB_RX = JOB_INDIA.pattern.replace(r"\b", r"\y")  # explore's prefilter only: the wider job-location list (geo.PLACES)


FIT_ORDER = {"strong": 0, "related": 1, "weak": 2}
FUNDED = """SELECT DISTINCT ON (f.company_id) f.company_id::text AS cid, f.stage, f.verification_status, coalesce(f.announced_at, f.first_detected_at) AS at
    FROM funding_rounds f WHERE f.company_id::text = ANY(:ids) AND f.verification_status IN ('VERIFIED', 'PARTIALLY_VERIFIED') AND NOT f.is_rumour
      AND coalesce(f.announced_at, f.first_detected_at) > now() - make_interval(days => :d)
    ORDER BY f.company_id, coalesce(f.announced_at, f.first_detected_at) DESC"""
JOB_STATUS = """SELECT j.source_id::text || ':' || j.external_job_id AS k, j.closed_at IS NULL AS open, j.first_seen_at, j.years_min FROM job_postings j
    JOIN unnest(CAST(:sids AS text[]), CAST(:exts AS text[])) AS x(sid, ext) ON j.source_id::text = x.sid AND j.external_job_id = x.ext"""
# Reposts (career-ops detect-reposts.mjs, MIT): the same role listed again under a new id. Same company and same normalised title, two or more ids, first seen on
# dates at least REPOST_SPAN_DAYS apart, within REPOST_WINDOW_DAYS. A span of zero is a company listing sibling openings at once, not a repost, so a record
# that has only just been built flags nothing (first_seen is when WE first saw the job).
REPOST_SPAN_DAYS, REPOST_WINDOW_DAYS = 7, 90
TRACKED = """SELECT t.source_id::text || ':' || t.external_job_id AS k, t.status FROM tracked_jobs t
    JOIN unnest(CAST(:sids AS text[]), CAST(:exts AS text[])) AS x(sid, ext) ON t.source_id::text = x.sid AND t.external_job_id = x.ext"""
REPOSTS = f"""SELECT coalesce(company_id::text, lower(company_name)) AS ck, btrim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) AS tk
    FROM job_postings WHERE first_seen_at > now() - interval '{REPOST_WINDOW_DAYS} days' AND coalesce(company_id::text, company_name) IS NOT NULL
    GROUP BY 1, 2 HAVING count(DISTINCT source_id::text || external_job_id) >= 2 AND max(first_seen_at) - min(first_seen_at) >= interval '{REPOST_SPAN_DAYS} days'"""
_key = lambda company, title: (str(company or "").lower(), re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip())


async def search(s: AsyncSession, mode: Literal["all", "india", "remote"] = "all", q: str = "", exclude: str = "", location: str = "", source: str = "",
                 days: int = 7, limit: int = 50, offset: int = 0, sort: Literal["newest", "fit", "company", "funded"] = "newest",
                 status: Literal["open", "all"] = "open", hide_tracked: bool = False) -> dict:
    """India and remote-open-to-India jobs only. q / exclude match the title and location the job text; each takes comma-separated words (any match).
    SQL narrows by date and a loose India/remote test; services.geo then decides exactly, so alerts and this page share one rule.
    counts and sources ignore mode and source, so the tabs can show how many each would give.
    J5: each row also carries fit / role / seniority (services.jobfit, config/jobs.yaml), a funding badge (a verified or partially verified round in the last
    funding_window_days) and open (the J1 job record; `status=open` hides jobs it has closed, jobs with no record stay).
    J7: each row carries `tracked`, the pipeline status of the job (or None); `hide_tracked` drops tracked jobs, which is what the Pipeline Inbox is."""
    rows = await _q(s, f"""SELECT e.id, e.title, e.location, e.published_at, e.detected_at, e.source_url, r.name AS source, coalesce(b.company_id, e.company_id) AS company_id,
        coalesce(c.name, e.company_name) AS company, coalesce(e.raw_metadata->>'region', '') = 'india' AS region_india, e.raw_metadata->>'workplace' AS workplace,
        e.source_id::text AS sid, coalesce(left(e.source_event_id, 200), e.id::text) AS ext
        FROM {JOB_FROM} LEFT JOIN companies c ON c.id = coalesce(b.company_id, e.company_id)
        WHERE e.event_type = 'job' AND coalesce(e.published_at, e.detected_at) > now() - make_interval(days => :d)
          AND ({EV_INDIA} OR e.raw_metadata->>'remote' = 'true' OR e.raw_metadata->>'workplace' = 'remote' OR e.location ~* :rrx OR (coalesce(e.location, '') = '' AND e.title ~* :rrx))
        ORDER BY coalesce(e.published_at, e.detected_at) DESC LIMIT {EXPLORE_SCAN}""", d=days, rrx=REMOTE_RX, rx=JOB_RX)
    words = lambda s: [w.strip().lower() for w in s.split(",") if w.strip()]
    want, drop, where = words(q), words(exclude), words(location)
    found = []
    for r in rows:
        title, loc = (r["title"] or "").lower(), (r["location"] or "").lower()
        region_india = r.pop("region_india")  # a source marked region=india (Jooble) counts even without a location
        india, remote = classify_geo(r["location"], r["title"], r.pop("workplace"))
        india = india or region_india
        if (india or remote) and (not want or any(w in title for w in want)) and not any(w in title for w in drop) \
                and (not where or any(w in loc for w in where)):
            found.append({**r, "india": india, "remote": remote, "role": jobfit.role_category(r["title"]), "seniority": jobfit.seniority(r["title"])})
    ids = list({str(j["company_id"]) for j in found if j["company_id"]})
    funded = {x["cid"]: x for x in await _q(s, FUNDED, ids=ids, d=jobfit.cfg()["funding_window_days"])} if ids else {}
    state = {x["k"]: x for x in await _q(s, JOB_STATUS, sids=[j["sid"] for j in found], exts=[j["ext"] or "" for j in found])} if found else {}
    tracked = {x["k"]: x["status"] for x in await _q(s, TRACKED, sids=[j["sid"] for j in found], exts=[j["ext"] or "" for j in found])} if found else {}
    reposted = {(x["ck"], x["tk"]) for x in await _q(s, REPOSTS)} if found else set()
    for j in found:
        f = funded.get(str(j["company_id"]))
        j["funding"] = {"stage": f["stage"], "status": f["verification_status"], "at": f["at"]} if f else None
        k = f"{j.pop('sid')}:{j.pop('ext')}"
        j["tracked"] = tracked.get(k)
        rec = state.get(k)  # None: the job has no record
        j["open"], j["first_seen_at"], j["years_min"] = (rec["open"], rec["first_seen_at"], rec["years_min"]) if rec else (None, None, None)
        j["above_band"] = bool(j["years_min"] and j["years_min"] > jobfit.cfg()["targets"]["max_years"])
        j["fit"] = jobfit.fit(j["title"], j["location"], j["remote"], j["years_min"])
        j["reposted"] = _key(j["company_id"] or j["company"], j["title"]) in reposted
    found = [j for j in found if (status == "all" or j["open"] is not False) and not (hide_tracked and j["tracked"])]
    picked = [j for j in found if (mode == "all" or j[mode]) and (not source or j["source"] == source)]
    when = lambda j: (j["published_at"] or j["detected_at"]).timestamp() if (j["published_at"] or j["detected_at"]) else 0
    picked.sort(key=lambda j: -when(j))  # newest first; the stable sorts below keep it as the tie-break
    if sort == "fit":
        picked.sort(key=lambda j: FIT_ORDER[j["fit"]])
    elif sort == "company":
        picked.sort(key=lambda j: (j["company"] or "~").lower())
    elif sort == "funded":
        picked.sort(key=lambda j: -j["funding"]["at"].timestamp() if j["funding"] and j["funding"]["at"] else 0)
    by_source: dict[str, int] = {}
    for j in found:
        by_source[j["source"]] = by_source.get(j["source"], 0) + 1
    return {"total": len(picked), "items": picked[offset:offset + limit], "truncated": len(rows) == EXPLORE_SCAN,
            "counts": {"all": len(found), "india": sum(j["india"] for j in found), "remote": sum(j["remote"] for j in found)},
            "sources": [{"source": s, "jobs": n} for s, n in sorted(by_source.items(), key=lambda x: -x[1])]}
