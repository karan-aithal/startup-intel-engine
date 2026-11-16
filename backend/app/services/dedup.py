"""Logical event matching (level 2). Level 1 is the unique keys on source_events.

Synchronous and rule-based so it can run before the alert: identical content from another source, or the same
company with the same amount. A *confirmed* duplicate (same label as the earlier event) suppresses the alert.
Semantic matching (embeddings) runs later in the LLM job and only records a relationship."""
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.claims import _same_amount, regex_claim
from app.services.companies import canonical

WINDOW_DAYS = 7


def _named_in(name: str | None, title: str | None) -> bool:
    return bool(name and len(name) >= 4 and title and re.search(rf"\b{re.escape(name)}\b", title, re.I))


def _same_company(a: str | None, a_title: str | None, b: str | None, b_title: str | None) -> bool:
    """Equal canonical names. When the extractor could not isolate a name from one headline ("Europe's first ... unicorn:
    Profitable Barcelona-based Stark Future raises"), the other article's company appearing in that headline counts. Two
    different extracted names never match here: amount and window alone are too weak to merge distinct companies."""
    if a and b:
        return canonical(a) == canonical(b)
    return _named_in(b, a_title) if b and not a else _named_in(a, b_title)


async def match_logical(s: AsyncSession, event_id: str, label: str) -> bool:
    """Record a relationship to the earliest matching event. Returns True when this event is a confirmed duplicate."""
    ev = (await s.execute(text("SELECT id, source_id, title, content, content_hash, detected_at FROM source_events WHERE id = :i"),
                          {"i": event_id})).one()
    alerted = bool(ev.content_hash and (await s.execute(text("SELECT 1 FROM alert_deliveries WHERE dedup_key = :k"),
                                                         {"k": f"event:{ev.content_hash}"})).first())
    found = None  # (related_id, related_label, kind, score)
    not_dependent = "NOT EXISTS (SELECT 1 FROM event_relationships x WHERE x.event_id = source_events.id AND x.related_event_id = :i)"
    if ev.content_hash:
        r = (await s.execute(text("""SELECT id, processing_status FROM source_events WHERE content_hash = :h AND id <> :i
            AND processing_status IN ('funding', 'rumour') AND source_id <> :s AND {nd} AND detected_at BETWEEN CAST(:d AS timestamptz) - make_interval(days => :w) AND CAST(:d AS timestamptz) + make_interval(days => :w)
            ORDER BY detected_at LIMIT 1""".replace("{nd}", not_dependent)), {"h": ev.content_hash, "i": event_id, "s": ev.source_id, "d": ev.detected_at, "w": WINDOW_DAYS})).first()
        if r:
            found = (r.id, r.processing_status, "syndicated_copy")
    if not found and not alerted:  # an event that already alerted is its group's primary: a retry must not make it a duplicate
        mine = regex_claim(ev.title, ev.content)
        if mine["amount_value"]:
            # Jobs run in queue order, not detection order, so a later-detected article may be processed first. Any article
            # already classified (status set at the end of process_event) counts, whichever was detected first.
            rows = (await s.execute(text("""SELECT id, title, content, processing_status FROM source_events
                WHERE id <> :i AND processing_status IN ('funding', 'rumour') AND {nd}
                AND detected_at BETWEEN CAST(:d AS timestamptz) - make_interval(days => :w) AND CAST(:d AS timestamptz) + make_interval(days => :w) ORDER BY detected_at""".replace("{nd}", not_dependent)),
                {"i": event_id, "d": ev.detected_at, "w": WINDOW_DAYS})).all()
            for r in rows:
                other = regex_claim(r.title, r.content)
                same_ccy = other["currency"] in (None, mine["currency"]) or mine["currency"] is None
                if (_same_company(mine["company"], ev.title, other["company"], r.title)
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
