"""Phase 9 read-only intelligence endpoints for the dashboard. Nothing here writes.

India is derived at read time with the same rule alerts use (pipeline.INDIA over event location, or raw_metadata.region == 'india').
It is never stored: companies.is_india_linked feeds scoring and stays untouched. A round is India when any of its evidence events is;
a company is India when any of its rounds or any job on its ATS boards is."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.services.geo import REMOTE, classify_geo
from app.services.pipeline import INDIA

router = APIRouter(prefix="/api", tags=["intel"])
Region = Literal["all", "india", "global"]
RX = INDIA.pattern.replace(r"\b", r"\y")  # Postgres ARE: \y is the word boundary, \b is backspace
EV_INDIA = "(coalesce(e.raw_metadata->>'region', '') = 'india' OR coalesce(e.location, '') ~* :rx)"  # coalesce: NULL would make NOT(...) drop the row
ROUND_INDIA = f"EXISTS (SELECT 1 FROM evidence ev JOIN source_events e ON e.id = ev.source_event_id WHERE ev.funding_round_id = f.id AND {EV_INDIA})"
JOB_FROM = """source_events e JOIN source_registry r ON r.id = e.source_id
    LEFT JOIN ats_boards b ON r.connector_type = 'api' AND r.name = b.provider || ':' || b.board_token"""
COMPANY_INDIA = f"""(EXISTS (SELECT 1 FROM funding_rounds f WHERE f.company_id = c.id AND {ROUND_INDIA})
    OR EXISTS (SELECT 1 FROM {JOB_FROM} WHERE b.company_id = c.id AND e.event_type = 'job' AND {EV_INDIA}))"""


def _region(region: str, expr: str) -> str:
    return {"india": expr, "global": f"NOT {expr}"}.get(region, "TRUE")


def _rows(r) -> list[dict]:
    return [dict(x._mapping) for x in r]


async def _q(sql: str, **p) -> list[dict]:
    async with get_sessionmaker()() as s:
        return _rows(await s.execute(text(sql), {"rx": RX, **p}))


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


@router.get("/overview")
async def overview(region: Region = "all") -> dict:
    sc = "score_india" if region == "india" else "score_global"
    rounds = await _q(f"""SELECT f.id, f.company_id, c.name, f.stage, f.amount_usd, f.amount_original, f.currency, f.announced_at, f.verification_status, f.confidence, f.{sc} AS score
        FROM funding_rounds f JOIN companies c ON c.id = f.company_id
        WHERE f.verification_status IN ('VERIFIED', 'PARTIALLY_VERIFIED') AND c.excluded_reason IS NULL AND {_region(region, ROUND_INDIA)}
        ORDER BY f.{sc} DESC NULLS LAST, f.announced_at DESC NULLS LAST LIMIT 8""")
    em = "emerging_india" if region == "india" else "emerging_global"
    emerging = await _q(f"""SELECT c.id, c.name, c.{em} AS score FROM companies c
        WHERE c.{em} IS NOT NULL AND NOT c.muted AND c.excluded_reason IS NULL AND {_region(region, COMPANY_INDIA)} ORDER BY c.{em} DESC, c.name LIMIT 8""")
    stats = (await _q(f"""SELECT
        (SELECT count(*) FROM companies c WHERE NOT c.muted AND c.excluded_reason IS NULL AND {_region(region, COMPANY_INDIA)}) AS companies,
        (SELECT count(*) FROM funding_rounds f WHERE NOT EXISTS (SELECT 1 FROM companies x WHERE x.id = f.company_id AND x.excluded_reason IS NOT NULL) AND {_region(region, ROUND_INDIA)}) AS rounds,
        (SELECT count(*) FROM funding_rounds f WHERE f.verification_status = 'VERIFIED' AND NOT EXISTS (SELECT 1 FROM companies x WHERE x.id = f.company_id AND x.excluded_reason IS NOT NULL) AND {_region(region, ROUND_INDIA)}) AS verified_rounds,
        (SELECT count(*) FROM {JOB_FROM} WHERE e.event_type = 'job' AND e.published_at > now() - interval '7 days' AND {_region(region, EV_INDIA)}) AS jobs_7d,
        (SELECT count(*) FROM source_registry WHERE enabled) AS sources_enabled,
        (SELECT count(*) FROM source_registry WHERE enabled AND health_status <> 'HEALTHY') AS sources_unhealthy"""))[0]
    alerts = await _q("""SELECT topic, count(*) AS n FROM alert_deliveries WHERE created_at > now() - interval '24 hours' GROUP BY topic ORDER BY n DESC""")
    return {"region": region, "stats": stats, "top_funding": rounds, "top_emerging": emerging, "alerts_24h": alerts}


@router.get("/companies")
async def companies(q: str = "", region: Region = "all", sort: Literal["emerging", "name", "recent"] = "emerging",
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    em = "c.emerging_india" if region == "india" else "c.emerging_global"
    order = {"emerging": f"{em} DESC NULLS LAST, c.name", "name": "c.name", "recent": "c.created_at DESC"}[sort]
    rows = await _q(f"""SELECT c.id, c.name, c.website_url, c.watched, c.emerging_global, c.emerging_india, c.created_at, {COMPANY_INDIA} AS india,
        (SELECT count(*) FROM funding_rounds f WHERE f.company_id = c.id) AS rounds,
        (SELECT max(f.announced_at) FROM funding_rounds f WHERE f.company_id = c.id) AS last_round_at,
        count(*) OVER () AS total
        FROM companies c WHERE NOT c.muted AND c.excluded_reason IS NULL AND (:q = '' OR c.name ILIKE :p) AND {_region(region, COMPANY_INDIA)}
        ORDER BY {order} LIMIT :l OFFSET :o""", q=q, p=_like(q), l=limit, o=offset)
    return {"total": rows[0]["total"] if rows else 0, "items": [{k: v for k, v in r.items() if k != "total"} for r in rows]}


@router.get("/companies/{company_id}")
async def company(company_id: str) -> dict:
    try:
        c = await _q(f"SELECT c.*, {COMPANY_INDIA} AS india FROM companies c WHERE c.id = CAST(:i AS uuid)", i=company_id)
    except Exception:  # malformed uuid
        raise HTTPException(404, "company not found")
    if not c:
        raise HTTPException(404, "company not found")
    rounds = await _q(f"""SELECT f.id, f.stage, f.amount_usd, f.amount_original, f.currency, f.announced_at, f.first_detected_at, f.verification_status,
        f.confidence, f.is_rumour, f.risk_flags, f.score_global, f.score_india, {ROUND_INDIA} AS india,
        coalesce((SELECT json_agg(json_build_object('name', i.name, 'role', ci.role) ORDER BY i.name) FROM company_investors ci
                  JOIN investors i ON i.id = ci.investor_id WHERE ci.funding_round_id = f.id), '[]') AS investors
        FROM funding_rounds f WHERE f.company_id = CAST(:i AS uuid) ORDER BY f.announced_at DESC NULLS LAST, f.first_detected_at DESC""", i=company_id)
    founders = await _q("SELECT name, role, profile_url FROM founders WHERE company_id = CAST(:i AS uuid) ORDER BY name", i=company_id)
    boards = await _q("""SELECT id, provider, board_token, board_url, status, last_job_count, last_success_at FROM ats_boards
        WHERE company_id = CAST(:i AS uuid) ORDER BY provider, board_token""", i=company_id)
    sources = await _q("""SELECT kind, url, status, verified_at, last_polled_at FROM company_sources WHERE company_id = CAST(:i AS uuid) ORDER BY kind, url""", i=company_id)
    jobs = await _q(f"""SELECT e.id, e.title, e.location, e.published_at, e.source_url, {EV_INDIA} AS india FROM {JOB_FROM}
        WHERE e.event_type = 'job' AND b.company_id = CAST(:i AS uuid) ORDER BY e.published_at DESC NULLS LAST LIMIT 50""", i=company_id)
    jobs_total = (await _q(f"""SELECT count(*) AS n FROM {JOB_FROM} WHERE e.event_type = 'job' AND b.company_id = CAST(:i AS uuid)""", i=company_id))[0]["n"]
    signals = await _q("SELECT signal_type, occurred_at, data FROM company_signals WHERE company_id = CAST(:i AS uuid) ORDER BY occurred_at DESC", i=company_id)
    evidence = await _q("""SELECT ev.id, ev.funding_round_id, ev.evidence_type, ev.weight, ev.supports, ev.url, ev.snippet, ev.syndication_group,
        ev.created_at, e.published_at, e.detected_at, e.title, r.name AS source
        FROM evidence ev LEFT JOIN source_events e ON e.id = ev.source_event_id LEFT JOIN source_registry r ON r.id = e.source_id
        WHERE ev.company_id = CAST(:i AS uuid) ORDER BY coalesce(e.published_at, ev.created_at) DESC""", i=company_id)
    timeline = sorted(
        [{"at": x["published_at"] or x["created_at"], "kind": "evidence", "label": f"{x['evidence_type']} · {x['source'] or 'unknown source'}",
          "detail": x["title"] or x["snippet"], "url": x["url"], "round_id": x["funding_round_id"], "supports": x["supports"]} for x in evidence]
        + [{"at": x["occurred_at"], "kind": "signal", "label": x["signal_type"], "detail": None, "url": None, "round_id": None, "supports": None} for x in signals]
        + [{"at": x["first_detected_at"], "kind": "round", "label": f"{x['stage'] + ' ' if x['stage'] else ''}{x['verification_status']}", "detail": None, "url": None,
            "round_id": x["id"], "supports": None} for x in rounds],
        key=lambda t: t["at"].timestamp() if t["at"] else 0, reverse=True)
    return {"company": c[0], "rounds": rounds, "founders": founders, "ats_boards": boards, "sources": sources, "jobs": jobs, "jobs_total": jobs_total,
            "signals": signals, "evidence": evidence, "timeline": timeline}


@router.get("/funding")
async def funding(region: Region = "all", status: str = "", stage: str = "", sort: Literal["recent", "score"] = "recent",
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    sc = "f.score_india" if region == "india" else "f.score_global"
    order = f"{sc} DESC NULLS LAST, f.announced_at DESC NULLS LAST" if sort == "score" else "f.announced_at DESC NULLS LAST, f.first_detected_at DESC"
    rows = await _q(f"""SELECT f.id, f.company_id, c.name AS company, f.stage, f.amount_usd, f.amount_original, f.currency, f.announced_at, f.first_detected_at,
        f.verification_status, f.confidence, f.is_rumour, f.risk_flags, f.score_global, f.score_india, {ROUND_INDIA} AS india, count(*) OVER () AS total
        FROM funding_rounds f JOIN companies c ON c.id = f.company_id
        WHERE c.excluded_reason IS NULL AND (:st = '' OR f.verification_status = :st) AND (:sg = '' OR lower(f.stage) = lower(:sg)) AND {_region(region, ROUND_INDIA)}
        ORDER BY {order} LIMIT :l OFFSET :o""", st=status.upper(), sg=stage, l=limit, o=offset)
    return {"total": rows[0]["total"] if rows else 0, "items": [{k: v for k, v in r.items() if k != "total"} for r in rows]}


@router.get("/hiring")
async def hiring(region: Region = "all", q: str = "", company_id: str = "",
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    where = f"""e.event_type = 'job' AND (:q = '' OR e.title ILIKE :p) AND (:cid = '' OR b.company_id::text = :cid) AND {_region(region, EV_INDIA)}"""
    rows = await _q(f"""SELECT e.id, e.title, e.location, e.published_at, e.detected_at, e.source_url, r.name AS source, b.company_id,
        coalesce(c.name, e.company_name) AS company, (SELECT l.result->>'seniority' FROM llm_runs l WHERE l.source_event_id = e.id AND l.task = 'job'
        AND l.valid LIMIT 1) AS seniority, {EV_INDIA} AS india, count(*) OVER () AS total
        FROM {JOB_FROM} LEFT JOIN companies c ON c.id = b.company_id WHERE {where}
        ORDER BY e.published_at DESC NULLS LAST, e.detected_at DESC LIMIT :l OFFSET :o""", q=q, p=_like(q), cid=company_id, l=limit, o=offset)
    top = await _q(f"""SELECT coalesce(c.name, e.company_name) AS company, b.company_id, count(*) AS jobs FROM {JOB_FROM}
        LEFT JOIN companies c ON c.id = b.company_id WHERE {where} GROUP BY 1, 2 ORDER BY jobs DESC LIMIT 15""", q=q, p=_like(q), cid=company_id)
    return {"total": rows[0]["total"] if rows else 0, "items": [{k: v for k, v in r.items() if k != "total"} for r in rows], "top_companies": top}


EXPLORE_SCAN = 5000  # newest job events read per request; Python then applies the exact geo rule
REMOTE_RX = REMOTE.pattern.replace(r"\b", r"\y")


@router.get("/explore")
async def explore(mode: Literal["all", "india", "remote"] = "all", q: str = "", exclude: str = "", location: str = "", source: str = "",
                  days: int = Query(7, ge=1, le=30), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    """India and remote-open-to-India jobs only. q / exclude match the title and location the job text; each takes comma-separated words (any match).
    SQL narrows by date and a loose India/remote test; services.geo then decides exactly, so alerts and this page share one rule.
    counts and sources ignore mode and source, so the tabs can show how many each would give."""
    rows = await _q(f"""SELECT e.id, e.title, e.location, e.published_at, e.detected_at, e.source_url, r.name AS source, b.company_id,
        coalesce(c.name, e.company_name) AS company, coalesce(e.raw_metadata->>'region', '') = 'india' AS region_india
        FROM {JOB_FROM} LEFT JOIN companies c ON c.id = b.company_id
        WHERE e.event_type = 'job' AND coalesce(e.published_at, e.detected_at) > now() - make_interval(days => :d)
          AND ({EV_INDIA} OR e.raw_metadata->>'remote' = 'true' OR e.location ~* :rrx OR (coalesce(e.location, '') = '' AND e.title ~* :rrx))
        ORDER BY coalesce(e.published_at, e.detected_at) DESC LIMIT {EXPLORE_SCAN}""", d=days, rrx=REMOTE_RX)
    words = lambda s: [w.strip().lower() for w in s.split(",") if w.strip()]
    want, drop, where = words(q), words(exclude), words(location)
    found = []
    for r in rows:
        title, loc = (r["title"] or "").lower(), (r["location"] or "").lower()
        region_india = r.pop("region_india")  # a source marked region=india (Jooble) counts even without a location
        india, remote = classify_geo(r["location"], r["title"])
        india = india or region_india
        if (india or remote) and (not want or any(w in title for w in want)) and not any(w in title for w in drop) \
                and (not where or any(w in loc for w in where)):
            found.append({**r, "india": india, "remote": remote})
    picked = [j for j in found if (mode == "all" or j[mode]) and (not source or j["source"] == source)]
    by_source: dict[str, int] = {}
    for j in found:
        by_source[j["source"]] = by_source.get(j["source"], 0) + 1
    return {"total": len(picked), "items": picked[offset:offset + limit], "truncated": len(rows) == EXPLORE_SCAN,
            "counts": {"all": len(found), "india": sum(j["india"] for j in found), "remote": sum(j["remote"] for j in found)},
            "sources": [{"source": s, "jobs": n} for s, n in sorted(by_source.items(), key=lambda x: -x[1])]}


@router.get("/discovery")
async def discovery() -> dict:
    return {
        "candidates": await _q("""SELECT d.id, d.name, d.domain, d.status, d.match_confidence, d.matched_company_id, c.name AS matched_company, d.created_at
            FROM company_discovery_candidates d LEFT JOIN companies c ON c.id = d.matched_company_id ORDER BY d.created_at DESC LIMIT 100"""),
        "candidate_status": await _q("SELECT status, count(*) AS n FROM company_discovery_candidates GROUP BY status ORDER BY n DESC"),
        "boards": await _q("""SELECT b.provider, b.board_token, b.status, b.discovered_via, b.last_job_count, b.company_id, c.name AS company, b.created_at
            FROM ats_boards b JOIN companies c ON c.id = b.company_id ORDER BY b.created_at DESC LIMIT 100"""),
        "board_status": await _q("SELECT status, count(*) AS n FROM ats_boards GROUP BY status ORDER BY n DESC"),
        "company_sources": await _q("SELECT kind, status, count(*) AS n FROM company_sources GROUP BY kind, status ORDER BY kind, n DESC"),
    }


@router.get("/source-health")
async def source_health() -> list[dict]:
    return await _q("""SELECT r.id, r.name, r.connector_type, r.category, r.enabled, r.health_status, r.consecutive_failures, r.last_success_at,
        r.last_error_at, left(r.last_error, 300) AS last_error, r.circuit_open_until, r.next_poll_at, r.activity_score, r.poll_interval_seconds,
        (SELECT count(*) FROM source_events e WHERE e.source_id = r.id AND e.detected_at > now() - interval '24 hours') AS events_24h,
        (SELECT count(*) FROM source_events e WHERE e.source_id = r.id) AS events_total
        FROM source_registry r ORDER BY r.enabled DESC, (r.health_status = 'HEALTHY'), r.category, r.name""")


@router.get("/search")
async def search(q: str = Query(..., min_length=2, max_length=100), region: Region = "all") -> dict:
    p = _like(q)
    return {
        "companies": await _q(f"""SELECT c.id, c.name, c.website_url, similarity(c.name, :q) AS sim FROM companies c
            WHERE NOT c.muted AND c.excluded_reason IS NULL AND (c.name ILIKE :p OR similarity(c.name, :q) > 0.3) AND {_region(region, COMPANY_INDIA)} ORDER BY sim DESC, c.name LIMIT 20""", q=q, p=p),
        "events": await _q(f"""SELECT e.id, e.event_type, e.title, e.source_url, e.published_at, e.company_name, e.location, r.name AS source
            FROM source_events e JOIN source_registry r ON r.id = e.source_id
            WHERE (e.title ILIKE :p OR e.company_name ILIKE :p OR e.content ILIKE :p) AND {_region(region, EV_INDIA)}
            ORDER BY e.published_at DESC NULLS LAST LIMIT 30""", p=p),
    }
