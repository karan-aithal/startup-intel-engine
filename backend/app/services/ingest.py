"""Store normalized events idempotently, advance the cursor, and enqueue processing."""
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import NormalizedEvent
from app.services.queue import enqueue


def content_hash(ev: NormalizedEvent) -> str | None:
    body = " ".join((ev.title or "", ev.content or "")).strip().lower()
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest() if body else None


async def store_event(s: AsyncSession, source_id: str, ev: NormalizedEvent) -> str | None:
    """Insert one event. Returns its id, or None if it duplicates (source, event id) or canonical URL."""
    res = await s.execute(
        text("""
        INSERT INTO source_events (source_id, source_event_id, source_url, canonical_url, event_type, title,
            content, content_hash, published_at, detected_at, company_name, company_domain, author_name,
            author_url, location, country, raw_metadata, permissions_metadata, retention_expires_at)
        SELECT :sid, :eid, :surl, :curl, :etype, :title, :content, :hash, :pub, :det, :cname, :cdom, :an,
            :au, :loc, :ctry, CAST(:raw AS jsonb), CAST(:perm AS jsonb),
            CAST(:det AS timestamptz) + make_interval(days => r.retention_days)
        FROM source_registry r WHERE r.id = :sid
        ON CONFLICT DO NOTHING
        RETURNING id
        """),
        {"sid": source_id, "eid": ev.source_event_id, "surl": ev.source_url, "curl": ev.canonical_url,
         "etype": ev.event_type, "title": ev.title, "content": ev.content, "hash": content_hash(ev),
         "pub": ev.published_at, "det": ev.detected_at, "cname": ev.company_name, "cdom": ev.company_domain,
         "an": ev.author_name, "au": ev.author_url, "loc": ev.location, "ctry": ev.country,
         "raw": json.dumps(ev.raw_metadata, default=str), "perm": json.dumps(ev.permissions_metadata, default=str)},
    )
    new_id = res.scalar_one_or_none()
    if new_id is None:
        return None
    await enqueue(s, "process_event", {"event_id": str(new_id)}, str(new_id))
    return str(new_id)


async def save_cursor(s: AsyncSession, source_id: str, cursor: dict, scope: str = "") -> None:
    await s.execute(
        text("""INSERT INTO sync_cursors (source_id, scope, cursor) VALUES (:sid, :scope, CAST(:c AS jsonb))
                ON CONFLICT (source_id, scope) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()"""),
        {"sid": source_id, "scope": scope, "c": json.dumps(cursor, default=str)},
    )


async def load_cursor(s: AsyncSession, source_id: str, scope: str = "") -> dict | None:
    r = await s.execute(
        text("SELECT cursor FROM sync_cursors WHERE source_id = :sid AND scope = :scope"),
        {"sid": source_id, "scope": scope},
    )
    return r.scalar_one_or_none()


async def ingest_batch(
    s: AsyncSession, source_id: str, events: list[NormalizedEvent], cursor: dict | None = None, scope: str = "",
) -> tuple[int, int]:
    """Events, queue jobs and cursor commit together (caller commits). Returns (new, duplicates)."""
    new = 0
    for ev in events:
        if await store_event(s, source_id, ev):
            new += 1
    if cursor is not None:
        await save_cursor(s, source_id, cursor, scope)
    return new, len(events) - new
