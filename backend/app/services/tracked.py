"""J7 Pipeline: jobs the owner saved, skipped or is pursuing (decision 111). Tracking only: nothing here applies, sends or submits.

The Inbox is not stored. It is Explore's result minus the tracked jobs (`explore.search(hide_tracked=True)`), so it can never disagree with Explore.
A tracked job is keyed by (source_id, external_job_id), the same pair as the J1 record, and keeps its own copy of title, company, link and
location so it still reads after retention scrubs the event. Every status change writes a tracked_job_events row."""
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tracked import STATES
from app.services import explore

TRIAGE = ("saved", "skipped")  # what an Inbox job can become; the rest of the states are moves of a tracked job
MAX_BULK = 200
FIELD_MAX = {"notes": 5000, "contact": 300, "salary_text": 300, "next_step": 300}
FUNNEL = ("saved", "applied", "responded", "assessment", "interview", "offer", "hired")
SORTS = {"updated": "t.status_changed_at DESC", "applied": "t.applied_at DESC NULLS LAST, t.status_changed_at DESC",
         "company": "lower(coalesce(t.company_name, '~')), t.title", "title": "lower(t.title)"}


class TrackError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _ids(ids: list[str]) -> list[str]:
    try:
        out = list(dict.fromkeys(str(uuid.UUID(i)) for i in ids))
    except (ValueError, AttributeError, TypeError) as e:
        raise TrackError(422, "malformed id") from e
    if not out or len(out) > MAX_BULK:
        raise TrackError(422, f"give 1 to {MAX_BULK} ids")
    return out


async def _rows(s: AsyncSession, sql: str, **p) -> list[dict]:
    return [dict(x._mapping) for x in await s.execute(text(sql), p)]


# ---- writes ----

# One job's copy, from its event (Explore's row id) or from its J1 record (an imported mark that names only the record).
EVENT_SNAP = f"""SELECT e.id::text AS ref, e.source_id, coalesce(left(e.source_event_id, 200), e.id::text) AS ext, e.title, e.location, e.source_url AS url,
    coalesce(c.name, e.company_name) AS company_name, coalesce(b.company_id, e.company_id) AS company_id, jp.id AS job_posting_id
    FROM {explore.JOB_FROM} LEFT JOIN companies c ON c.id = coalesce(b.company_id, e.company_id)
    LEFT JOIN job_postings jp ON jp.source_id = e.source_id AND jp.external_job_id = left(e.source_event_id, 200)
    WHERE e.event_type = 'job' AND e.id = ANY(CAST(:ids AS uuid[]))"""
POSTING_SNAP = """SELECT j.id::text AS ref, j.source_id, coalesce(j.external_job_id, j.id::text) AS ext, j.title, j.location, j.url,
    coalesce(c.name, j.company_name) AS company_name, j.company_id, j.id AS job_posting_id
    FROM job_postings j LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ANY(CAST(:ids AS uuid[]))"""
SNAP_KEYS = ("source_id", "ext", "job_posting_id", "company_id", "company_name", "title", "url", "location")


async def _insert(s: AsyncSession, snap: dict, status: str, applied_at: datetime | None = None, note: str | None = None) -> bool:
    """Track one job; False when it is already tracked (the first status stands)."""
    row = (await s.execute(text("""INSERT INTO tracked_jobs (source_id, external_job_id, job_posting_id, company_id, company_name, title, url, location, status, applied_at)
        VALUES (:source_id, :ext, :job_posting_id, :company_id, :company_name, coalesce(:title, '(untitled)'), :url, :location, :st, :ap)
        ON CONFLICT (source_id, external_job_id) DO NOTHING RETURNING id"""), {**{k: snap[k] for k in SNAP_KEYS}, "st": status, "ap": applied_at})).first()
    if row:
        await s.execute(text("INSERT INTO tracked_job_events (tracked_job_id, to_status, note, at) VALUES (:i, :st, :n, clock_timestamp())"), {"i": row[0], "st": status, "n": note})
    return bool(row)


async def track(s: AsyncSession, event_ids: list[str], status: str) -> dict:
    """Save or Skip jobs from Explore or the Inbox, by event id. The server reads title, company and link from the event; the client sends none."""
    if status not in TRIAGE:
        raise TrackError(422, f"an Inbox job can only be {' or '.join(TRIAGE)}")
    ids = _ids(event_ids)
    snaps = await _rows(s, EVENT_SNAP, ids=ids)
    new = 0
    for snap in snaps:
        new += await _insert(s, snap, status)
    return {"tracked": new, "already": len(snaps) - new, "missing": len(ids) - len(snaps)}


async def set_status(s: AsyncSession, ids: list[str], status: str, note: str | None = None) -> dict:
    if status not in STATES:
        raise TrackError(422, "unknown status")
    ids = _ids(ids)
    rows = await _rows(s, "SELECT id, status FROM tracked_jobs WHERE id = ANY(CAST(:ids AS uuid[])) FOR UPDATE", ids=ids)
    moved = [r for r in rows if r["status"] != status]
    for r in moved:
        await s.execute(text("""UPDATE tracked_jobs SET status = CAST(:st AS text), status_changed_at = now(), updated_at = now(),
            applied_at = CASE WHEN CAST(:st AS text) = 'applied' AND applied_at IS NULL THEN now() ELSE applied_at END WHERE id = :i"""), {"st": status, "i": r["id"]})
        await s.execute(text("INSERT INTO tracked_job_events (tracked_job_id, from_status, to_status, note, at) VALUES (:i, :f, :st, :n, clock_timestamp())"),
                        {"i": r["id"], "f": r["status"], "st": status, "n": (note or "").strip() or None})
    return {"changed": len(moved), "unchanged": len(rows) - len(moved), "missing": len(ids) - len(rows)}


async def edit(s: AsyncSession, tid: str, fields: dict, applied_at: datetime | None, set_applied: bool) -> None:
    """Notes, contact, salary text, next step and the applied date. `fields` holds only the keys the owner sent; "" clears one."""
    _ids([tid])
    sets, p = [], {"i": tid}
    for k, v in fields.items():
        if k not in FIELD_MAX:
            raise TrackError(422, f"unknown field {k}")
        if v is not None and len(v) > FIELD_MAX[k]:
            raise TrackError(422, f"{k} is longer than {FIELD_MAX[k]}")
        sets.append(f"{k} = :{k}")
        p[k] = (v or "").strip() or None
    if set_applied:
        sets.append("applied_at = :ap")
        p["ap"] = applied_at
    if not sets:
        return
    r = await s.execute(text(f"UPDATE tracked_jobs SET {', '.join(sets)}, updated_at = now() WHERE id = CAST(:i AS uuid)"), p)  # keys are whitelisted above
    if not r.rowcount:
        raise TrackError(404, "tracked job not found")


async def restore(s: AsyncSession, tid: str) -> None:
    """Back to the Inbox: the tracked row and its history are deleted (the job is then an Explore result again)."""
    _ids([tid])
    if not (await s.execute(text("DELETE FROM tracked_jobs WHERE id = CAST(:i AS uuid)"), {"i": tid})).rowcount:
        raise TrackError(404, "tracked job not found")


# ---- one-time import of the feedback marks ----

async def import_marks(s: AsyncSession, dry_run: bool = True) -> dict:
    """`save` and `applied` feedback marks become tracked jobs (saved, applied). A mark resolves through its alert's job event, or its job record; a mark
    on anything else (a funding alert) is reported, not guessed. A job with both marks is applied, dated by the earliest applied mark. Idempotent: a job
    already tracked is left alone. The dry run builds the same plan and writes nothing, so its count is what the real run then imports."""
    marks = await _rows(s, "SELECT action, created_at, source_event_id::text AS ev, job_posting_id::text AS jp FROM feedback WHERE action IN ('save', 'applied') ORDER BY created_at")
    evs = [m["ev"] for m in marks if m["ev"]]
    ev = {r["ref"]: r for r in await _rows(s, EVENT_SNAP, ids=evs)} if evs else {}
    jps = [m["jp"] for m in marks if m["jp"]]
    jp = {r["ref"]: r for r in await _rows(s, POSTING_SNAP, ids=jps)} if jps else {}
    plan: dict[tuple, dict] = {}
    unresolved = 0
    for m in marks:
        snap = ev.get(m["ev"]) or jp.get(m["jp"])
        if not snap:
            unresolved += 1
            continue
        p = plan.setdefault((snap["source_id"], snap["ext"]), {"snap": snap, "status": "saved", "applied_at": None})
        if m["action"] == "applied":
            p["status"] = "applied"
            p["applied_at"] = min(p["applied_at"] or m["created_at"], m["created_at"])
    have = {(r["source_id"], r["ext"]) for r in await _rows(s, "SELECT source_id, external_job_id AS ext FROM tracked_jobs")}
    todo = [p for k, p in plan.items() if k not in have]
    out = {"marks": len(marks), "unresolved": unresolved, "jobs": len(plan), "already_tracked": len(plan) - len(todo),
           "to_import": len(todo), "saved": sum(p["status"] == "saved" for p in todo), "applied": sum(p["status"] == "applied" for p in todo), "dry_run": dry_run}
    if not dry_run:
        out["imported"] = sum([await _insert(s, p["snap"], p["status"], p["applied_at"], "imported from your save/applied marks") for p in todo])
    return out


# ---- reads ----

async def overview(s: AsyncSession, tab: str = "inbox", q: str = "", sort: str = "updated", mode: str = "all", days: int = 7, offset: int = 0, limit: int = 50) -> dict:
    """Tab counts, the funnel and one page of rows. tab = inbox (Explore minus tracked) or a state. The badges are unfiltered; the rows honour q."""
    if tab != "inbox" and tab not in STATES:
        raise TrackError(422, "unknown tab")
    counts = {r["status"]: r["n"] for r in await _rows(s, "SELECT status, count(*) AS n FROM tracked_jobs GROUP BY 1")}
    on_inbox = tab == "inbox"
    inbox = await explore.search(s, mode, q if on_inbox else "", days=days, limit=limit if on_inbox else 1, offset=offset if on_inbox else 0,
                                 sort=sort if sort in ("newest", "fit", "company", "funded") else "newest", hide_tracked=True)
    inbox_n = inbox["counts"]["all"]
    if on_inbox and q:  # the badge ignores the search
        inbox_n = (await explore.search(s, mode, "", days=days, limit=1, hide_tracked=True))["counts"]["all"]
    funnel = {r["to_status"]: r["n"] for r in await _rows(s, "SELECT to_status, count(DISTINCT tracked_job_id) AS n FROM tracked_job_events GROUP BY 1")}
    out = {"tab": tab, "counts": {"inbox": inbox_n, **{st: counts.get(st, 0) for st in STATES}}, "funnel": [{"stage": st, "reached": funnel.get(st, 0)} for st in FUNNEL]}
    if on_inbox:
        return {**out, "total": inbox["total"], "items": inbox["items"], "truncated": inbox["truncated"], "sources": inbox["sources"]}
    words = [w.strip().lower() for w in q.split(",") if w.strip()]
    where = " AND ".join(["t.status = :tab", *[f"(lower(t.title) LIKE :w{i} OR lower(coalesce(t.company_name, '')) LIKE :w{i} OR lower(coalesce(t.notes, '')) LIKE :w{i})" for i in range(len(words))]])
    p = {"tab": tab, **{f"w{i}": f"%{w}%" for i, w in enumerate(words)}}
    total = (await _rows(s, f"SELECT count(*) AS n FROM tracked_jobs t WHERE {where}", **p))[0]["n"]
    items = await _rows(s, f"""SELECT t.id::text AS id, t.title, t.company_id::text AS company_id, t.company_name, t.url, t.location, t.status, t.next_step, t.salary_text,
        t.applied_at, t.status_changed_at, t.created_at, CASE WHEN jp.id IS NULL THEN NULL ELSE jp.closed_at IS NULL END AS open
        FROM tracked_jobs t LEFT JOIN job_postings jp ON jp.id = t.job_posting_id WHERE {where}
        ORDER BY {SORTS.get(sort, SORTS['updated'])}, t.id LIMIT :lim OFFSET :off""", lim=limit, off=offset, **p)  # SORTS is a fixed map, never the raw parameter
    return {**out, "total": total, "items": items, "truncated": False, "sources": []}


async def detail(s: AsyncSession, tid: str) -> dict:
    _ids([tid])
    rows = await _rows(s, """SELECT t.id::text AS id, t.title, t.company_id::text AS company_id, t.company_name, t.url, t.location, t.status, t.notes, t.contact, t.salary_text,
        t.next_step, t.applied_at, t.status_changed_at, t.created_at, jp.id IS NOT NULL AS has_record, jp.closed_at, jp.last_seen_at, jp.first_seen_at
        FROM tracked_jobs t LEFT JOIN job_postings jp ON jp.id = t.job_posting_id WHERE t.id = CAST(:i AS uuid)""", i=tid)
    if not rows:
        raise TrackError(404, "tracked job not found")
    job = rows[0]
    history = await _rows(s, "SELECT from_status, to_status, note, at FROM tracked_job_events WHERE tracked_job_id = CAST(:i AS uuid) ORDER BY at, id", i=tid)
    company, funding = None, []
    if job["company_id"]:
        company = (await _rows(s, "SELECT id::text AS id, name, watched FROM companies WHERE id = CAST(:c AS uuid)", c=job["company_id"]) or [None])[0]
        funding = await _rows(s, """SELECT stage, amount_usd, amount_original, currency, verification_status, coalesce(announced_at, first_detected_at) AS at
            FROM funding_rounds WHERE company_id = CAST(:c AS uuid) AND NOT is_rumour ORDER BY coalesce(announced_at, first_detected_at) DESC LIMIT 3""", c=job["company_id"])
    return {"job": job, "history": history, "company": company, "funding": funding,
            "closed": job["closed_at"] is not None if job["has_record"] else None}  # None: no J1 record, so liveness is unknown
