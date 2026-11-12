"""process_event job: classify an event, queue at most one alert for it. Plus alert formatting."""
import html
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.outbox import add_alert
from app.services.prefilter import classify

INDIA = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai)\b", re.I)
JOB_MAX_AGE = timedelta(hours=48)  # older jobs are stored but not alerted, so a first poll doesn't flood Telegram
LABEL_TITLE = {"funding": "FUNDING", "rumour": "UNVERIFIED / RUMOUR", "hiring": "HIRING"}


def pick_topic(label: str, india: bool) -> str:
    if label == "rumour":
        return "Unverified and Risk"
    return f"{'India' if india else 'Global'} {'Funding' if label == 'funding' else 'Hiring'}"


async def process_event(s: AsyncSession, event_id: str) -> str:
    """Returns the label. The alert row is written in the caller's transaction (with job completion)."""
    ev = (await s.execute(text("""
        SELECT e.id, e.event_type, e.title, e.content, e.content_hash, e.canonical_url, e.published_at,
               e.detected_at, e.location, e.company_name, e.raw_metadata, r.name AS source
        FROM source_events e JOIN source_registry r ON r.id = e.source_id WHERE e.id = :id"""),
        {"id": event_id})).mappings().one()
    if ev["event_type"] == "form_d":
        c = {"label": "funding", "amount": None}
    elif ev["event_type"] == "job":
        c = {"label": "hiring", "amount": None}
    else:
        c = classify(ev["title"], ev["content"])
    label = c["label"]
    alertable = label in LABEL_TITLE
    if label == "hiring" and ev["event_type"] == "job":
        pub = ev["published_at"]
        alertable = pub is not None and datetime.now(UTC) - pub <= JOB_MAX_AGE
    if alertable:
        india = (ev["raw_metadata"] or {}).get("region") == "india" or bool(INDIA.search(ev["location"] or ""))
        await add_alert(
            s, f"event:{ev['content_hash'] or ev['id']}",
            {"label": label, "title": ev["title"], "url": ev["canonical_url"], "source": ev["source"],
             "amount": c["amount"], "company": ev["company_name"], "location": ev["location"],
             "published_at": ev["published_at"], "detected_at": ev["detected_at"]},
            topic=pick_topic(label, india))
    await s.execute(text("UPDATE source_events SET processing_status = :l WHERE id = :id"),
                    {"l": label, "id": event_id})
    return label


def _fmt(ts, tz: ZoneInfo) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(tz).strftime("%d %b %Y %H:%M %Z") if ts else "unknown"


def format_alert(topic: str | None, p: dict, tz_name: str = "Asia/Kolkata") -> str:
    """Telegram HTML. Both timestamps appear: when the source published it, and when we first saw it."""
    tz, e = ZoneInfo(tz_name), html.escape
    lines = [f"<b>{LABEL_TITLE.get(p['label'], p['label'].upper())}</b> · {e(topic or '')}", f"<b>{e(p['title'] or '')}</b>"]
    if p.get("company"):
        lines.append(f"Company: {e(p['company'])}")
    if p.get("amount"):
        lines.append(f"Amount: {e(p['amount'])}")
    if p.get("location"):
        lines.append(f"Location: {e(p['location'])}")
    if p["label"] == "rumour":
        lines.append("Unverified: single source, not a confirmed round.")
    lines += [f"Source: {e(p['source'])}", f"Published: {_fmt(p.get('published_at'), tz)}",
              f"Detected: {_fmt(p['detected_at'], tz)}"]
    if p.get("url"):
        lines.append(e(p["url"]))
    return "\n".join(lines)
