"""Logical event matching (level 2). Level 1 is the unique keys on source_events.

Synchronous and rule-based so it can run before the alert: identical content from another source, or the same
company with the same amount. A *confirmed* duplicate (same label as the earlier event) suppresses the alert.
Semantic matching (embeddings) runs later in the LLM job and only records a relationship."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.claims import _same_amount, regex_claim
from app.services.companies import canonical

WINDOW_DAYS = 7


async def match_logical(s: AsyncSession, event_id: str, label: str) -> bool:
    """Record a relationship to the earliest matching event. Returns True when this event is a confirmed duplicate."""
    ev = (await s.execute(text("SELECT id, source_id, title, content, content_hash, detected_at FROM source_events WHERE id = :i"),
                          {"i": event_id})).one()
    found = None  # (related_id, related_label, kind, score)
    if ev.content_hash:
        r = (await s.execute(text("""SELECT id, processing_status FROM source_events WHERE content_hash = :h AND id <> :i
            AND source_id <> :s AND detected_at <= :d AND detected_at > :d - make_interval(days => :w)
            ORDER BY detected_at LIMIT 1"""), {"h": ev.content_hash, "i": event_id, "s": ev.source_id, "d": ev.detected_at, "w": WINDOW_DAYS})).first()
        if r:
            found = (r.id, r.processing_status, "syndicated_copy")
    if not found:
        mine = regex_claim(ev.title, ev.content)
        if mine["company"] and mine["amount_value"]:
            rows = (await s.execute(text("""SELECT id, title, content, processing_status FROM source_events
                WHERE id <> :i AND processing_status IN ('funding', 'rumour') AND detected_at <= :d
                AND detected_at > :d - make_interval(days => :w) ORDER BY detected_at"""),
                {"i": event_id, "d": ev.detected_at, "w": WINDOW_DAYS})).all()
            for r in rows:
                other = regex_claim(r.title, r.content)
                same_ccy = other["currency"] in (None, mine["currency"]) or mine["currency"] is None
                if (other["company"] and canonical(other["company"]) == canonical(mine["company"])
                        and _same_amount(other["amount_value"], mine["amount_value"]) and same_ccy):
                    found = (r.id, r.processing_status, "same_event")
                    break
    if not found:
        return False
    related, related_label, kind = found
    confirmed = related_label == label  # a rumour followed by the real announcement is a follow-up, not a duplicate
    await s.execute(text("""INSERT INTO event_relationships (event_id, related_event_id, kind, score, method)
        VALUES (:e, :r, :k, 1, 'sync_rule') ON CONFLICT DO NOTHING"""),
        {"e": event_id, "r": related, "k": kind if confirmed else "follow_up"})
    return confirmed
