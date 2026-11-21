"""J6 scheduled scans: an Explore search saved with an interval, re-run by the worker over data already collected (collection itself is the
scheduler's job). A run asks services.explore.search for the newest matches, keeps those detected after the search's watermark and queues ONE digest
through the outbox.

Crash safety: a run is a single transaction (run row + digest + watermark move), so a worker killed mid-run leaves nothing and the lease expiry
re-runs it with the same run_no, hence the same dedup key `scan:{id}:{run_no}`. Pause stops runs; a lease keeps two workers off one search.
Ceiling: the watermark is the run's start time, so a job whose ingest transaction began before it but committed after is missed (ingest is short)."""
import json
import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import explore
from app.services.outbox import add_alert
from app.services.portals import SOURCE_HEALTH_SQL

log = structlog.get_logger()
UNIT_S = {"minutes": 60, "hours": 3600, "days": 86400}
MAX_SEARCHES = 20
MIN_INTERVAL_S = 15 * 60
RUN_NOW_COOLDOWN_S = 60
LEASE_S = 300
DIGEST_TOP = 10
SCAN_ITEMS = 200  # newest matches read per run; a run reporting this many new says "200+"
TOPIC = "Job Scans"
DEFAULT_FILTERS = {"mode": "all", "q": "", "exclude": "", "location": "", "source": "", "days": 7, "sort": "newest", "status": "open"}
FIT_ORDER = explore.FIT_ORDER


class ScanError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def interval_s(value: int, unit: str) -> int:
    return value * UNIT_S[unit]


def next_after(start: datetime, iv: int, now: datetime) -> datetime:
    """The first slot start + k*iv strictly after `now` (a long outage skips missed slots instead of replaying them)."""
    if now < start:
        return start
    return start + timedelta(seconds=iv * (int((now - start).total_seconds() // iv) + 1))


def window_days(days: int, watermark: datetime, now: datetime) -> int:
    """Explore's date window, widened so a long gap since the last run cannot hide jobs detected in it (max 30)."""
    return min(30, max(days, math.ceil((now - watermark).total_seconds() / 86400) + 1))


def digest(name: str, filters: dict, items: list[dict], watermark: datetime, now: datetime) -> tuple[dict, int] | None:
    """The one alert for a run: the new matches (detected after the watermark), top DIGEST_TOP, plus a link to Explore with the saved filters."""
    new = [j for j in items if j["detected_at"] and j["detected_at"] > watermark]
    if not new:
        return None
    if filters.get("sort") == "fit":
        new.sort(key=lambda j: FIT_ORDER[j["fit"]])  # stable: newest first within a fit
    link = {k: v for k, v in {**filters, "days": window_days(filters["days"], watermark, now)}.items() if v not in ("", None)}
    return {"label": "scan", "search": name, "count": len(new), "capped": len(items) >= SCAN_ITEMS and len(new) == len(items),
            "title": f"{name}: {len(new)} new", "detected_at": now,
            "jobs": [{"title": j["title"], "company": j["company"], "location": j["location"], "url": j["source_url"], "fit": j["fit"]} for j in new[:DIGEST_TOP]],
            "url": f"{get_settings().cors_list[0]}/explore?{urlencode(link)}"}, len(new)


async def claim(s: AsyncSession, limit: int = 5) -> list[dict]:
    res = await s.execute(text("""
        UPDATE saved_searches SET lease_expires_at = now() + make_interval(secs => :lease)
        WHERE id IN (SELECT id FROM saved_searches WHERE status = 'active' AND next_run_at <= now()
                       AND (lease_expires_at IS NULL OR lease_expires_at < now()) ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT :n)
        RETURNING id, name, filters, interval_value, interval_unit, start_at, watermark"""), {"lease": LEASE_S, "n": limit})
    return [dict(r) for r in res.mappings()]


async def run_one(s: AsyncSession, r: dict) -> None:
    now = (await s.execute(text("SELECT now()"))).scalar_one()  # this transaction's start: the next watermark
    f = {**DEFAULT_FILTERS, **r["filters"]}
    d = await explore.search(s, f["mode"], f["q"], f["exclude"], f["location"], f["source"], window_days(f["days"], r["watermark"], now),
                             SCAN_ITEMS, 0, "newest", f["status"])
    out = digest(r["name"], f, d["items"], r["watermark"], now)
    run_no = (await s.execute(text("SELECT coalesce(max(run_no), 0) + 1 FROM saved_search_runs WHERE search_id = :i"), {"i": r["id"]})).scalar_one()
    payload, n_new = out if out else (None, 0)
    await s.execute(text("""INSERT INTO saved_search_runs (search_id, run_no, started_at, finished_at, state, total_matches, new_matches)
        VALUES (:i, :n, :now, clock_timestamp(), 'ok', :t, :new)"""), {"i": r["id"], "n": run_no, "now": now, "t": d["total"], "new": n_new})
    if payload:
        await add_alert(s, f"scan:{r['id']}:{run_no}", payload, topic=TOPIC)
    await advance(s, r, now, now)


async def advance(s: AsyncSession, r: dict, now: datetime, watermark: datetime) -> None:
    nxt = next_after(r["start_at"], interval_s(r["interval_value"], r["interval_unit"]), now)
    await s.execute(text("UPDATE saved_searches SET watermark = :w, last_run_at = :now, next_run_at = :nx, lease_expires_at = NULL WHERE id = :i"),
                    {"w": watermark, "now": now, "nx": nxt, "i": r["id"]})


async def fail_one(s: AsyncSession, r: dict, exc: Exception) -> None:
    """The watermark stays, so the next run still covers what this one missed."""
    now = (await s.execute(text("SELECT now()"))).scalar_one()
    await s.execute(text("""INSERT INTO saved_search_runs (search_id, run_no, started_at, finished_at, state, error)
        SELECT :i, coalesce(max(run_no), 0) + 1, :now, :now, 'error', :e FROM saved_search_runs WHERE search_id = :i"""),
                    {"i": r["id"], "now": now, "e": f"{type(exc).__name__}: {exc}"[:500]})
    await advance(s, r, now, r["watermark"])


async def run_due(sm) -> int:
    """The worker loop body: claim due searches, run each in its own transaction."""
    async with sm() as s:
        rows = await claim(s)
        await s.commit()
    for r in rows:
        try:
            async with sm() as s:
                await run_one(s, r)
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("scan_failed", search=str(r["id"]), error=str(exc)[:200])
            try:
                async with sm() as s:
                    await fail_one(s, r, exc)
                    await s.commit()
            except Exception:  # noqa: BLE001 deleted mid-run, or another worker finished it: nothing to record
                log.info("scan_failure_not_recorded", search=str(r["id"]))
    return len(rows)


# ---- writes for the API ----

async def create(s: AsyncSession, name: str, filters: dict, value: int, unit: str, start_at: datetime | None, tz: str) -> str:
    if value * UNIT_S[unit] < MIN_INTERVAL_S:
        raise ScanError(422, "interval must be at least 15 minutes")
    try:
        ZoneInfo(tz)
    except Exception as e:
        raise ScanError(422, "unknown timezone") from e
    if (await s.execute(text("SELECT count(*) FROM saved_searches"))).scalar_one() >= MAX_SEARCHES:
        raise ScanError(409, f"at most {MAX_SEARCHES} saved searches; delete one first")
    now = (await s.execute(text("SELECT now()"))).scalar_one()
    start = start_at.astimezone(UTC) if start_at else now
    nxt = next_after(start, interval_s(value, unit), now)  # watermark = now: only jobs detected from now on are new, so no flood
    return str((await s.execute(text("""INSERT INTO saved_searches (name, filters, interval_value, interval_unit, start_at, timezone, watermark, next_run_at)
        VALUES (:n, CAST(:f AS jsonb), :v, :u, :s, :tz, :now, :nx) RETURNING id"""),
        {"n": name, "f": json.dumps(filters), "v": value, "u": unit, "s": start, "tz": tz, "now": now, "nx": nxt})).scalar_one())


async def _get(s: AsyncSession, sid: str) -> dict:
    try:
        row = (await s.execute(text("SELECT id, status, last_run_at, lease_expires_at FROM saved_searches WHERE id = CAST(:i AS uuid)"), {"i": sid})).mappings().first()
    except Exception as e:  # malformed uuid
        raise ScanError(404, "saved search not found") from e
    if not row:
        raise ScanError(404, "saved search not found")
    return dict(row)


async def set_status(s: AsyncSession, sid: str, status: str) -> None:
    await _get(s, sid)
    # resuming runs it at the next tick; the watermark is kept, so what appeared while paused is reported (the digest is capped at the top 10)
    await s.execute(text("""UPDATE saved_searches SET status = CAST(:st AS text), lease_expires_at = NULL,
        next_run_at = CASE WHEN CAST(:st AS text) = 'active' THEN now() ELSE next_run_at END WHERE id = CAST(:i AS uuid)"""), {"st": status, "i": sid})


async def run_now(s: AsyncSession, sid: str) -> None:
    r = await _get(s, sid)
    if r["status"] != "active":
        raise ScanError(409, "resume the search first")
    if r["lease_expires_at"] and r["lease_expires_at"] > datetime.now(UTC):
        raise ScanError(409, "a run is in progress")
    if r["last_run_at"] and (datetime.now(UTC) - r["last_run_at"]).total_seconds() < RUN_NOW_COOLDOWN_S:
        raise ScanError(429, "ran less than a minute ago")
    await s.execute(text("UPDATE saved_searches SET next_run_at = now() WHERE id = CAST(:i AS uuid)"), {"i": sid})


async def remove(s: AsyncSession, sid: str) -> None:
    await _get(s, sid)
    await s.execute(text("DELETE FROM saved_searches WHERE id = CAST(:i AS uuid)"), {"i": sid})


# ---- read ----

def _rows(res) -> list[dict]:
    return [dict(x._mapping) for x in res]


async def overview(s: AsyncSession, search_id: str | None = None) -> dict:
    searches = _rows(await s.execute(text("""SELECT s.*, (SELECT count(*) FROM saved_search_runs r WHERE r.search_id = s.id) AS runs,
        (SELECT count(*) FROM saved_search_runs r WHERE r.search_id = s.id AND r.state = 'ok') AS runs_ok,
        (SELECT r.new_matches FROM saved_search_runs r WHERE r.search_id = s.id ORDER BY r.run_no DESC LIMIT 1) AS last_new
        FROM saved_searches s ORDER BY s.created_at""")))
    runs = _rows(await s.execute(text("""SELECT r.id, r.search_id, s.name, r.run_no, r.started_at, r.state, r.total_matches, r.new_matches, r.error,
        round(extract(epoch FROM r.finished_at - r.started_at)::numeric, 2) AS seconds
        FROM saved_search_runs r JOIN saved_searches s ON s.id = r.search_id WHERE (CAST(:sid AS text) IS NULL OR r.search_id::text = :sid)
        ORDER BY r.started_at DESC LIMIT 50"""), {"sid": search_id}))
    cards = _rows(await s.execute(text("""SELECT count(*) FILTER (WHERE r.started_at > now() - interval '24 hours') AS runs_24h,
        count(*) FILTER (WHERE r.started_at > now() - interval '7 days') AS runs_7d,
        count(*) FILTER (WHERE r.started_at > now() - interval '7 days' AND r.state = 'ok') AS ok_7d FROM saved_search_runs r""")))[0]
    beat, up = (await s.execute(text("SELECT max(last_seen_at), coalesce(max(last_seen_at) > now() - interval '2 minutes', false) FROM worker_heartbeats"))).one()
    schedule = [{k: h[k] for k in ("id", "name", "health_status", "consecutive_failures", "last_success_at", "next_poll_at", "poll_interval_seconds", "events_24h")}
                for h in _rows(await s.execute(text(SOURCE_HEALTH_SQL))) if h["enabled"]]
    return {"searches": searches, "runs": runs, "limits": {"max": MAX_SEARCHES, "min_interval_minutes": MIN_INTERVAL_S // 60, "cooldown_seconds": RUN_NOW_COOLDOWN_S},
            "cards": {"active": sum(x["status"] == "active" for x in searches), "paused": sum(x["status"] == "paused" for x in searches), **cards},
            "worker_last_seen": beat, "worker_up": up, "schedule": schedule}
