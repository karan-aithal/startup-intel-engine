"""J8 follow-ups (decision 112): when to follow up on an application, a log of what the owner did, and one reminder per due episode.
Reminders only: nothing here sends email or messages, and contacts are typed by the owner, never scraped.

`verdict` is pure (dates in, verdict out, no database), like `feedback.wilson`. The cadence is ported from career-ops `followup-cadence.mjs`
(MIT, read not copied) with three changes the owner approved: days count from when the job entered its CURRENT status (career-ops counts from the
application date, so a reply 10 days after applying was "overdue" at once), each status counts its own follow-ups, and urgent means "due, no
follow-up yet, inside the first repeat window" so the label agrees with the next date.

An episode is (tracked job, due date). The outbox row `followup:{job}:{due date}` is unique per channel, so a restarted worker or a second pass
queues nothing new; logging a follow-up or pinning a date moves the due date, which starts a new episode."""
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.outbox import add_alert

CONFIG = Path(__file__).resolve().parents[3] / "config" / "followups.yaml"
ACTIVE = ("applied", "responded", "assessment", "interview")
ORDER = {"urgent": 0, "overdue": 1, "waiting": 2, "cold": 3, "stopped": 4}
TOPIC = "Follow-ups"
CADENCE = ("applied_first_days", "applied_repeat_days", "applied_max_followups", "responded_first_days", "responded_repeat_days", "interview_thankyou_days")


class FollowupError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def load_cfg(path: Path | None = None) -> dict:
    """Read and check the file on every call (it is small), so an edit needs no restart. A bad value raises ValueError naming it."""
    c = yaml.safe_load((path or CONFIG).read_text()) or {}
    for k in CADENCE:
        if not isinstance(c.get("cadence", {}).get(k), int) or not 0 <= c["cadence"][k] <= 365:
            raise ValueError(f"followups.yaml: cadence.{k} must be a whole number from 0 to 365")
    r = c.get("reminders", {})
    for k in ("hour", "quiet_from", "quiet_until"):
        if not isinstance(r.get(k), int) or not 0 <= r[k] <= 23:
            raise ValueError(f"followups.yaml: reminders.{k} must be an hour from 0 to 23")
    ZoneInfo(r.get("timezone", ""))  # raises on an unknown zone
    if not c.get("channels") or not all(isinstance(x, str) and x.strip() for x in c["channels"]):
        raise ValueError("followups.yaml: channels must list at least one name")
    c["enabled"] = c.get("enabled", True) is not False
    return c


def window_open(local: datetime, c: dict) -> bool:
    """Whether a reminder may be queued at this local time: from `hour` on, and outside the quiet hours (which may cross midnight)."""
    h, r = local.hour, c["reminders"]
    f, u = r["quiet_from"], r["quiet_until"]
    quiet = (h >= f or h < u) if f > u else (f <= h < u)
    return h >= r["hour"] and not quiet


def verdict(status: str, since: date, logs: list[date], pin: date | None, stopped: bool, today: date, c: dict) -> dict | None:
    """`logs`: the dates of the follow-ups made while the job had this status. Returns None for a status that has no follow-ups."""
    if status not in ACTIVE:
        return None
    cd, last, d = c["cadence"], max(logs, default=None), timedelta
    if status == "applied":
        nxt = None if len(logs) >= cd["applied_max_followups"] else (last + d(days=cd["applied_repeat_days"]) if last else since + d(days=cd["applied_first_days"]))
    elif status == "interview":
        nxt = last + d(days=cd["responded_repeat_days"]) if last else since + d(days=cd["interview_thankyou_days"])
    else:  # responded, assessment
        nxt = last + d(days=cd["responded_repeat_days"]) if last else since + d(days=cd["responded_first_days"])
    if pin:  # an explicit date beats the cadence and revives a cold job
        nxt = pin
    if stopped:
        urgency, nxt = "stopped", None
    elif nxt is None:
        urgency = "cold"
    elif today < nxt:
        urgency = "waiting"
    elif status in ("responded", "assessment") and not last and not pin and (today - since).days < cd["responded_repeat_days"]:
        urgency = "urgent"
    else:
        urgency = "overdue"
    return {"urgency": urgency, "next_on": nxt, "days_since": (today - since).days, "count": len(logs), "last_on": last}


def _uuid(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError) as e:
        raise FollowupError(422, "malformed id") from e


def _tz(c: dict) -> ZoneInfo:
    return ZoneInfo(c["reminders"]["timezone"])


async def _rows(s: AsyncSession, sql: str, **p) -> list[dict]:
    return [dict(x._mapping) for x in await s.execute(text(sql), p)]


async def entries(s: AsyncSession, c: dict, now: datetime) -> list[dict]:
    """Every job in a follow-up status with its verdict and log, most urgent first."""
    tz = _tz(c)
    today = now.astimezone(tz).date()
    jobs = await _rows(s, """SELECT id::text AS id, title, company_id::text AS company_id, company_name, url, status, contact, applied_at, status_changed_at,
        next_pin_on, reminders_stopped_at FROM tracked_jobs WHERE status = ANY(CAST(:st AS text[]))""", st=list(ACTIVE))
    logs: dict[str, list[dict]] = {}
    for r in await _rows(s, """SELECT id::text AS id, tracked_job_id::text AS tid, logged_on, status, channel, contact, notes FROM followup_log
            WHERE tracked_job_id = ANY(CAST(:ids AS uuid[])) ORDER BY logged_on DESC, created_at DESC""", ids=[j["id"] for j in jobs]):
        logs.setdefault(r.pop("tid"), []).append(r)
    out = []
    for j in jobs:
        began = j["applied_at"] if j["status"] == "applied" and j["applied_at"] else j["status_changed_at"]
        mine = logs.get(j["id"], [])
        v = verdict(j["status"], began.astimezone(tz).date(), [x["logged_on"] for x in mine if x["status"] == j["status"]], j["next_pin_on"],
                    j["reminders_stopped_at"] is not None, today, c)
        out.append({**{k: j[k] for k in ("id", "title", "company_id", "company_name", "url", "status", "contact")}, **v, "pinned": j["next_pin_on"] is not None,
                    "since": began.astimezone(tz).date(), "log": mine})
    out.sort(key=lambda e: (ORDER[e["urgency"]], e["next_on"] or date.max, e["title"].lower()))
    return out


async def overview(s: AsyncSession, c: dict | None = None, now: datetime | None = None) -> dict:
    c, now = c or load_cfg(), now or datetime.now(UTC)
    if not c["enabled"]:
        return {"enabled": False}
    items = await entries(s, c, now)
    return {"enabled": True, "today": now.astimezone(_tz(c)).date(), "tiles": {k: sum(e["urgency"] == k for e in items) for k in ORDER}, "items": items,
            "settings": {"file": "config/followups.yaml", "cadence": c["cadence"], "channels": c["channels"], "reminders": c["reminders"]}}


# ---- the worker ----

def reminder(e: dict, now: datetime) -> dict:
    return {"label": "followup", "title": f"Follow up: {e['title']}", "company": e["company_name"], "status": e["status"], "urgency": e["urgency"],
            "due": e["next_on"].isoformat(), "days_since": e["days_since"], "count": e["count"], "contact": e["contact"], "job_url": e["url"],
            "url": f"{get_settings().cors_list[0]}/followups", "detected_at": now}


async def remind_due(sm, c: dict | None = None, now: datetime | None = None) -> int:
    """One worker pass: queue ONE outbox reminder for each job that is due and not yet reminded for this due date. One transaction, so a crash
    queues nothing and the next pass redoes it; the unique dedup key makes the redo harmless. Returns how many were new."""
    c, now = c or load_cfg(), now or datetime.now(UTC)
    if not c["enabled"] or not window_open(now.astimezone(_tz(c)), c):
        return 0
    async with sm() as s:
        new = 0
        for e in await entries(s, c, now):
            if e["urgency"] in ("urgent", "overdue") and await add_alert(s, f"followup:{e['id']}:{e['next_on']}", reminder(e, now), topic=TOPIC):
                new += 1
        await s.commit()
    return new


# ---- writes for the API ----

async def _job(s: AsyncSession, tid: str) -> str:
    rows = await _rows(s, "SELECT status FROM tracked_jobs WHERE id = CAST(:i AS uuid) FOR UPDATE", i=_uuid(tid))
    if not rows:
        raise FollowupError(404, "tracked job not found")
    if rows[0]["status"] not in ACTIVE:
        raise FollowupError(422, f"follow-ups apply to {', '.join(ACTIVE)} jobs only")
    return rows[0]["status"]


async def log(s: AsyncSession, tid: str, channel: str, contact: str | None, notes: str | None, on: date | None, next_on: date | None, c: dict, now: datetime) -> str:
    """Log a follow-up. It moves the next date (the cadence restarts from `on`), drops any older pin, lifts a stop, and may pin the next date in one go."""
    if channel not in c["channels"]:
        raise FollowupError(422, f"channel must be one of {', '.join(c['channels'])}")
    today = now.astimezone(_tz(c)).date()
    on = on or today
    if on > today:
        raise FollowupError(422, "the follow-up date is in the future")
    if next_on and next_on < on:
        raise FollowupError(422, "the next date is before the follow-up")
    status = await _job(s, tid)
    new = (await s.execute(text("""INSERT INTO followup_log (tracked_job_id, logged_on, status, channel, contact, notes)
        VALUES (CAST(:i AS uuid), :on, :st, :ch, :ct, :n) RETURNING id::text"""), {"i": tid, "on": on, "st": status, "ch": channel, "ct": (contact or "").strip()[:300] or None,
                                                                                    "n": (notes or "").strip()[:2000] or None})).scalar_one()
    await s.execute(text("UPDATE tracked_jobs SET next_pin_on = :p, reminders_stopped_at = NULL, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"p": next_on, "i": tid})
    return new


async def pin(s: AsyncSession, tid: str, on: date | None) -> None:
    """Pin the next follow-up date (None clears the pin). Pinning also lifts a stop: you asked to be reminded."""
    await _job(s, tid)
    await s.execute(text("UPDATE tracked_jobs SET next_pin_on = :p, reminders_stopped_at = NULL, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"p": on, "i": tid})


async def stop(s: AsyncSession, tid: str, stopped: bool) -> None:
    await _job(s, tid)
    await s.execute(text("UPDATE tracked_jobs SET reminders_stopped_at = CASE WHEN :st THEN now() END, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"st": stopped, "i": tid})


async def delete_log(s: AsyncSession, lid: str) -> None:
    """Undo a mistaken entry; the cadence then reads as if it had not been logged."""
    if not (await s.execute(text("DELETE FROM followup_log WHERE id = CAST(:i AS uuid)"), {"i": _uuid(lid)})).rowcount:
        raise FollowupError(404, "log entry not found")
