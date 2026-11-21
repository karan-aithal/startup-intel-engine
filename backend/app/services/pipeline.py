"""process_event job: classify an event, queue at most one alert for it. Plus alert formatting."""
import html
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.claims import rebuild_claims
from app.services.companies import add_candidate, extract_company
from app.services.dedup import match_logical
from app.services.geo import INDIA, wanted
from app.services.outbox import add_alert
from app.services.prefilter import classify
from app.services.queue import enqueue

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
    elif ev["event_type"] == "repo":  # GitHub launch signal: stored and correlated, never alerted (a repo blurb must not match funding/hiring keywords)
        # 'ignored' = kept for audit but not a launch (an organisation's .github profile repo, or marked by hand with raw_metadata.ignored_reason).
        # Decided here, not only at ingest, so a retried job can never turn an ignored event back into a counted one.
        ignored = (ev["raw_metadata"] or {}).get("ignored_reason") or (ev["title"] or "").rsplit("/", 1)[-1].startswith(".")
        c = {"label": "ignored" if ignored else "none", "amount": None}
    else:
        c = classify(ev["title"], ev["content"])
    label = c["label"]
    alertable = label in LABEL_TITLE
    if label == "hiring" and ev["event_type"] == "job":
        pub = ev["published_at"]
        in_scope = (ev["raw_metadata"] or {}).get("region") == "india" or wanted(ev["location"], ev["title"], (ev["raw_metadata"] or {}).get("workplace"))  # India or remote-open-to-India jobs only
        alertable = in_scope and pub is not None and datetime.now(UTC) - pub <= JOB_MAX_AGE
    duplicate = label in ("funding", "rumour") and ev["event_type"] != "form_d" and await match_logical(s, event_id, label)
    if duplicate:
        alertable = False  # confirmed duplicate of an event already alerted: linked in event_relationships, not re-sent
    if label == "funding" and ev["event_type"] != "form_d":  # heavy resolution runs in its own worker loop, never here
        if name := ev["company_name"] or extract_company(ev["title"]):
            await add_candidate(s, name, str(ev["id"]))
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
    if label in ("funding", "rumour"):
        await rebuild_claims(s, event_id)  # cheap regex claims now; the LLM job merges its result in later
    if get_settings().llm_enabled and (label in ("funding", "rumour") or ev["event_type"] == "job"):
        await enqueue(s, "llm_enrich", {"event_id": event_id}, event_id, priority=90)  # own queue kind: never delays alerts
    return label


def _fmt(ts, tz: ZoneInfo) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(tz).strftime("%d %b %Y %H:%M %Z") if ts else "unknown"


def format_alert(topic: str | None, p: dict, tz_name: str = "Asia/Kolkata", resent: bool = False) -> str:
    """Telegram HTML. Both timestamps appear: when the source published it, and when we first saw it."""
    tz, e = ZoneInfo(tz_name), html.escape
    lines = [f"<b>{LABEL_TITLE.get(p['label'], p['label'].upper())}</b> · {e(topic or '')}" + (" (resent)" if resent else ""), f"<b>{e(p['title'] or '')}</b>"]
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
