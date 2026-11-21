"""Portals (J4, decision 107): which company boards and job boards feed the job data, their health, and the owner's controls over them.

One list for every route that collects jobs: company ATS boards (ats_boards rows, plus the seeded boards in source_registry that have no company row) and
the board-wide job boards (Himalayas, Remotive, Jobicy ...). State is derived here from fields that already exist; nothing new is stored.
Modelled on career-ops' Portals tab (verify-portals.mjs: live, live but empty, broken, no ATS). career-ops' portals.yml editing and its repair worker are not
ported. India and remote counts come from services.geo over the J1 job record, so this page, Explore and alerts share one rule."""
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.api import AtsAdapter, board_url
from app.connectors.feed import USER_AGENT
from app.services import queue
from app.services.aggregators import is_aggregator
from app.services.ats import SMALL_BOARD, active_boards, find_boards, register_board, watched_count
from app.services.companies import canonical, match_company
from app.services.geo import classify_geo
from app.services.registry import set_enabled
from app.services.safefetch import guarded_client

JOB_BOARD_KINDS = ("himalayas", "remotive", "jobicy", "workingnomads", "weworkremotely", "jooble")
STALE = timedelta(days=3)  # no success for this long: broken (owner, 2026-11-19)
FAILURES = 3  # consecutive failures: broken, and the number demote_stale already uses
RECHECK_WINDOW_S = 300  # one queued re-check per route per 5 minutes

# Shared with /api/source-health and /api/discovery so the three pages cannot drift apart.
SOURCE_HEALTH_SQL = """SELECT r.id, r.name, r.connector_type, r.category, r.enabled, r.health_status, r.consecutive_failures, r.last_success_at,
        r.last_error_at, left(r.last_error, 300) AS last_error, r.circuit_open_until, r.next_poll_at, r.activity_score, r.poll_interval_seconds,
        r.config->>'kind' AS kind,
        (SELECT count(*) FROM source_events e WHERE e.source_id = r.id AND e.detected_at > now() - interval '24 hours') AS events_24h,
        (SELECT count(*) FROM source_events e WHERE e.source_id = r.id) AS events_total
        FROM source_registry r ORDER BY r.enabled DESC, (r.health_status = 'HEALTHY'), r.category, r.name"""
BOARDS_SQL = """SELECT b.id, b.provider, b.board_token, b.board_url, b.status, b.discovered_via, b.last_job_count, b.last_success_at, b.last_polled_at,
        b.verification_status, (b.evidence->>'small_board')::boolean AS small_board, b.company_id, c.name AS company, b.created_at
        FROM ats_boards b LEFT JOIN companies c ON c.id = b.company_id ORDER BY b.created_at DESC"""


class PortalError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def rows(s: AsyncSession, sql: str, **p) -> list[dict]:
    return [dict(r) for r in (await s.execute(text(sql), p)).mappings()]


def board_state(*, enabled: bool, status: str | None, failures: int, circuit_open_until: datetime | None, last_success: datetime | None,
                jobs: int | None, now: datetime | None = None) -> str:
    """disabled | candidate | pending | broken | empty | live.
    candidate: stored, waiting for room under the caps. disabled: switched off (or paused by the owner). pending: enabled, no poll has succeeded yet.
    broken: the circuit is open, or 3 failures in a row, or no success for 3 days. empty: polls fine, lists no jobs. live: everything else."""
    now = now or datetime.now(UTC)
    if status == "candidate":
        return "candidate"
    if not enabled or status == "paused":
        return "disabled"
    if (circuit_open_until and circuit_open_until > now) or failures >= FAILURES or (last_success and now - last_success > STALE):
        return "broken"
    if last_success is None:
        return "pending"
    return "empty" if jobs == 0 else "live"


async def open_counts(s: AsyncSession) -> dict[str, dict]:
    """Open jobs per route from the J1 record: all open, and those the geo rule calls India or remote-open-to-India."""
    out: dict[str, dict] = {}
    for r in await rows(s, """SELECT source_id::text AS sid, title, location, workplace, count(*) AS n FROM job_postings
                              WHERE closed_at IS NULL AND source_id IS NOT NULL GROUP BY 1, 2, 3, 4"""):
        india, remote = classify_geo(r["location"], r["title"], r["workplace"])
        c = out.setdefault(r["sid"], {"open": 0, "india": 0, "remote": 0})
        c["open"] += r["n"]
        c["india"] += r["n"] if india else 0
        c["remote"] += r["n"] if remote else 0
    return out


async def portals(s: AsyncSession, company_cap: int, board_cap: int) -> dict:
    health = {r["name"]: r for r in await rows(s, SOURCE_HEALTH_SQL)}
    counts = await open_counts(s)
    none = {"open": 0, "india": 0, "remote": 0}
    company_boards, seen = [], set()

    def common(h: dict | None) -> dict:
        h = h or {}
        return {"route_id": str(h["id"]) if h.get("id") else None, "enabled": bool(h.get("enabled")), "health_status": h.get("health_status"),
                "failures": h.get("consecutive_failures") or 0, "circuit_open_until": h.get("circuit_open_until"), "next_poll_at": h.get("next_poll_at"),
                "poll_interval_seconds": h.get("poll_interval_seconds"), "last_error": h.get("last_error"), "last_success_at": h.get("last_success_at")}

    for b in await rows(s, BOARDS_SQL):
        name = f"{b['provider']}:{b['board_token']}"
        seen.add(name)
        h, c = common(health.get(name)), counts.get(str(health.get(name, {}).get("id")), none)
        success = b["last_success_at"] or h["last_success_at"]
        company_boards.append({**h, **c, "kind": "company", "name": name, "provider": b["provider"], "token": b["board_token"], "board_url": b["board_url"],
                               "company_id": str(b["company_id"]) if b["company_id"] else None, "company": b["company"], "status": b["status"],
                               "discovered_via": b["discovered_via"], "small_board": bool(b["small_board"]), "last_job_count": b["last_job_count"],
                               "last_success_at": success, "last_polled_at": b["last_polled_at"],
                               "state": board_state(enabled=h["enabled"], status=b["status"], failures=h["failures"], circuit_open_until=h["circuit_open_until"],
                                                    last_success=success, jobs=b["last_job_count"])})
    for name, hr in health.items():  # the seeded list in config/sources.yaml: routes with no company row
        if hr["kind"] != "ats" or name in seen:
            continue
        provider, _, token = name.partition(":")
        h, c = common(hr), counts.get(str(hr["id"]), none)
        try:
            url = board_url(provider, token)
        except (KeyError, ValueError):
            url = None
        company_boards.append({**h, **c, "kind": "seeded", "name": name, "provider": provider, "token": token, "board_url": url, "company_id": None,
                               "company": None, "status": None, "discovered_via": "seed list", "small_board": 0 < c["open"] <= SMALL_BOARD,
                               "last_job_count": c["open"], "last_polled_at": None,
                               "state": board_state(enabled=h["enabled"], status=None, failures=h["failures"], circuit_open_until=h["circuit_open_until"],
                                                    last_success=h["last_success_at"], jobs=c["open"])})
    job_boards = []
    for name, hr in sorted(health.items()):
        if hr["kind"] not in JOB_BOARD_KINDS:
            continue
        h, c = common(hr), counts.get(str(hr["id"]), none)
        job_boards.append({**h, **c, "kind": hr["kind"], "name": name, "events_24h": hr["events_24h"], "events_total": hr["events_total"],
                           "state": board_state(enabled=h["enabled"], status=None, failures=h["failures"], circuit_open_until=h["circuit_open_until"],
                                                last_success=h["last_success_at"], jobs=c["open"])})
    no_ats = await rows(s, """SELECT c.id::text AS company_id, c.name AS company, c.website_url, c.careers_url FROM companies c
        WHERE c.watched AND NOT c.muted AND NOT EXISTS (SELECT 1 FROM ats_boards b WHERE b.company_id = c.id AND b.status = 'active') ORDER BY c.name""")
    states: dict[str, int] = {}
    for b in [*company_boards, *job_boards]:
        states[b["state"]] = states.get(b["state"], 0) + 1
    states["no_ats"] = len(no_ats)
    company_boards.sort(key=lambda b: (b["state"] != "broken", b["state"], b["name"]))
    return {"states": states, "company_boards": company_boards, "job_boards": job_boards, "no_ats": no_ats,
            "caps": {"companies": {"used": await watched_count(s), "cap": company_cap}, "boards": {"used": await active_boards(s), "cap": board_cap}}}


async def _route(s: AsyncSession, route_id: str) -> dict:
    try:
        r = await rows(s, "SELECT id::text AS id, name, connector_type, enabled, config FROM source_registry WHERE id = CAST(:i AS uuid)", i=route_id)
    except Exception as e:
        await s.rollback()
        raise PortalError(404, "no such route") from e
    if not r:
        raise PortalError(404, "no such route")
    kind = (r[0]["config"] or {}).get("kind")
    if kind != "ats" and kind not in JOB_BOARD_KINDS:
        raise PortalError(422, f"{r[0]['name']} is not a job portal")
    return {**r[0], "kind": kind}


async def set_route_enabled(s: AsyncSession, route_id: str, enabled: bool, company_cap: int, board_cap: int) -> dict:
    """Enable or disable one route. A company board is switched on only when both growth caps have room; switching it off pauses it, frees its slot
    and, if it was the company's only active board, the company's watched flag."""
    r = await _route(s, route_id)
    if enabled and r["kind"] == "jooble":
        raise PortalError(409, "Jooble is gated: it needs terms_confirmed in config/sources.yaml and JOOBLE_API_KEY, not a switch here")
    board = (await rows(s, """SELECT b.id::text AS id, b.company_id::text AS company_id, b.status, b.verification_status, b.provider, b.board_token
        FROM ats_boards b WHERE b.provider || ':' || b.board_token = :n""", n=r["name"]) or [None])[0]
    if enabled and board and board["status"] != "active":
        if await active_boards(s) >= board_cap:
            raise PortalError(409, f"the active-board cap is reached ({board_cap}); disable another board first")
        if board["company_id"]:
            watched = (await s.execute(text("SELECT watched FROM companies WHERE id = CAST(:c AS uuid)"), {"c": board["company_id"]})).scalar_one()
            if not watched and await watched_count(s) >= company_cap:
                raise PortalError(409, f"the watched-company cap is reached ({company_cap})")
        if board["verification_status"] != "verified":
            try:
                await AtsAdapter(board["provider"], board["board_token"]).fetch()
            except Exception as e:
                raise PortalError(422, f"the board did not list jobs: {str(e)[:150]}") from e
            await s.execute(text("UPDATE ats_boards SET verification_status = 'verified', verified_at = now() WHERE id = CAST(:i AS uuid)"), {"i": board["id"]})
        await s.execute(text("UPDATE ats_boards SET status = 'active', promoted_at = now(), promotion_reason = 'enabled by owner' WHERE id = CAST(:i AS uuid)"),
                        {"i": board["id"]})
        if board["company_id"]:
            await s.execute(text("UPDATE companies SET watched = true WHERE id = CAST(:c AS uuid)"), {"c": board["company_id"]})
    await set_enabled(s, r["name"], r["connector_type"], enabled)
    if not enabled and board:
        await s.execute(text("UPDATE ats_boards SET status = 'paused', promoted_at = NULL, promotion_reason = 'paused by owner' WHERE id = CAST(:i AS uuid)"),
                        {"i": board["id"]})
        if board["company_id"]:
            await s.execute(text("""UPDATE companies SET watched = false WHERE id = CAST(:c AS uuid)
                AND NOT EXISTS (SELECT 1 FROM ats_boards WHERE company_id = CAST(:c AS uuid) AND status = 'active')"""), {"c": board["company_id"]})
    return {"route_id": route_id, "name": r["name"], "enabled": enabled}


async def request_recheck(s: AsyncSession, route_id: str) -> dict:
    """Queue one poll of an enabled route for the worker (never run inside the request). One per route per 5 minutes; the poll itself is the
    route's normal run, so its failure and success are recorded the usual way and a success closes an open circuit."""
    r = await _route(s, route_id)
    if not r["enabled"]:
        raise PortalError(409, f"{r['name']} is disabled; enable it first")
    key = f"{route_id}:{int(time.time() // RECHECK_WINDOW_S)}"
    job = await queue.enqueue(s, "portal_check", {"route_id": route_id, "name": r["name"]}, key, priority=40, max_attempts=2)
    return {"route_id": route_id, "name": r["name"], "queued": job is not None}


async def add_portal(s: AsyncSession, company_id: str | None, url: str, company_cap: int, board_cap: int, client: httpx.AsyncClient | None = None,
                     company_name: str | None = None) -> list[dict]:
    """Find ATS boards in a careers URL (the URL itself, then the page it serves) and register each under the caps: register_board verifies that the
    board lists jobs, stores it with its evidence and lets promote_candidates decide whether it is polled now or waits as a candidate."""
    u = url.strip()
    if not u.startswith(("http://", "https://")) or len(u) > 500:
        raise PortalError(422, "enter a full http(s) careers URL")
    if is_aggregator(u):
        raise PortalError(422, "that is a job aggregator, not the company's own careers page")
    name = (company_name or "").strip()
    if company_id:
        if not await rows(s, "SELECT 1 FROM companies WHERE id = CAST(:c AS uuid)", c=company_id):
            raise PortalError(404, "no such company")
    elif not name:
        raise PortalError(422, "give a company id or a company name")
    new_company = False
    own = client is None
    c = client or guarded_client(timeout=15, follow_redirects=True)
    try:
        found, found_on = find_boards(u), u
        if not found:
            try:
                r = await c.get(u, headers={"User-Agent": USER_AGENT})
            except httpx.HTTPError as e:
                raise PortalError(422, f"could not fetch that page: {str(e)[:120]}") from e
            found_on = str(r.url)
            found = find_boards(r.text) if r.status_code == 200 else []
            found += [b for b in find_boards(found_on) if b not in found]
        if not found:
            raise PortalError(422, "no supported ATS board found at that URL (Greenhouse, Lever, Ashby, Keka, Darwinbox, TurboHire, Zoho Recruit, SmartRecruiters)")
        if not company_id:  # a company not tracked yet: reuse an exact name/alias match, else create it (rolled back below if no board works)
            company_id, conf = await match_company(s, name)
            if conf < 100:
                company_id = str((await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :c) RETURNING id"),
                                                  {"n": name[:200], "c": canonical(name)})).scalar_one())
                new_company = True
        out = []
        for provider, token in found[:5]:
            ev = {"method": "added in Portals", "url": u, "found_on": found_on, "provider": provider, "token": token}
            out.append({"provider": provider, "token": token,
                        "result": await register_board(s, company_id, provider, token, "added in Portals", company_cap, board_cap, c, ev)})
        if new_company and all(x["result"] == "invalid" for x in out):
            raise PortalError(422, "none of the boards found at that URL lists jobs, so the company was not added")
        return out
    finally:
        if own:
            await c.aclose()
