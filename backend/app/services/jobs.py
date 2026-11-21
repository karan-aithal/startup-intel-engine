"""The job record (`job_postings`): one durable row per job a route lists, kept after the event loses its text (decision 75).

Key: (source_id, external_job_id), so ATS boards and remote boards dedupe the same way. Written at ingest for every job the route lists,
new or repeated (that is what `last_seen_at` means). Closing: an ATS board lists everything it has on every poll, so a job missing from a
non-empty listing is closed at once; any other open job closes when it has not been listed for `STALE_DAYS` (remote boards re-list only recent
jobs, so for them "closed" means "stale"). A job that is listed again reopens. Geo flags are not stored (decision 69): `is_remote` is only what the
source itself said."""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import NormalizedEvent

STALE_DAYS = 30

# Shared by ingest (one event's fields) and backfill (the same fields read from source_events), so both write identical rows.
UPSERT = """
INSERT INTO job_postings (source_id, ats_board_id, company_id, company_name, source_event_id, external_job_id, title, url, location, country,
                          is_remote, workplace, years_min, posted_at, first_seen_at, last_seen_at, status)
SELECT r.id, b.id, b.company_id, coalesce(c.name, CAST(:cname AS text)), CAST(:eid AS uuid), left(CAST(:ext AS text), 200), CAST(:title AS text),
       CAST(:url AS text), CAST(:loc AS text), CAST(:ctry AS text), CAST(:rem AS boolean), CAST(:wp AS text), CAST(:yrs AS smallint), CAST(:pub AS timestamptz), CAST(:seen AS timestamptz),
       CAST(:seen AS timestamptz), 'open'
FROM source_registry r
LEFT JOIN ats_boards b ON r.connector_type = 'api' AND b.provider || ':' || b.board_token = r.name
LEFT JOIN companies c ON c.id = b.company_id
WHERE r.id = :sid
ON CONFLICT (source_id, external_job_id) DO UPDATE SET
    last_seen_at = greatest(job_postings.last_seen_at, EXCLUDED.last_seen_at), status = 'open', closed_at = NULL,
    workplace = coalesce(EXCLUDED.workplace, job_postings.workplace), years_min = coalesce(EXCLUDED.years_min, job_postings.years_min), title = EXCLUDED.title, url = coalesce(EXCLUDED.url, job_postings.url), location = EXCLUDED.location, updated_at = now(),
    source_event_id = coalesce(job_postings.source_event_id, EXCLUDED.source_event_id),
    company_id = coalesce(job_postings.company_id, EXCLUDED.company_id)
RETURNING (xmax = 0) AS inserted"""


async def upsert_job(s: AsyncSession, source_id: str, ev: NormalizedEvent, event_id: str | None, seen: datetime) -> bool:
    """Returns True when the record is new."""
    if not ev.source_event_id:
        return False
    remote = (ev.raw_metadata or {}).get("remote")
    row = (await s.execute(text(UPSERT), {
        "sid": source_id, "cname": ev.company_name, "eid": event_id, "ext": ev.source_event_id, "title": ev.title,
        "url": ev.canonical_url or ev.source_url, "loc": ev.location, "ctry": ev.country, "rem": True if remote else None, "wp": (ev.raw_metadata or {}).get("workplace"), "yrs": (ev.raw_metadata or {}).get("years_min"),
        "pub": ev.published_at, "seen": seen})).first()
    return bool(row and row.inserted)


async def close_unlisted(s: AsyncSession, source_id: str, seen: datetime) -> int:
    """After a full listing: open jobs of this route that the listing did not mention."""
    return (await s.execute(text("""UPDATE job_postings SET status = 'closed', closed_at = now(), updated_at = now()
        WHERE source_id = :sid AND status = 'open' AND last_seen_at < :seen"""), {"sid": source_id, "seen": seen})).rowcount


async def close_stale(s: AsyncSession) -> int:
    return (await s.execute(text("""UPDATE job_postings SET status = 'closed', closed_at = now(), updated_at = now()
        WHERE status = 'open' AND last_seen_at < now() - make_interval(days => :d)"""), {"d": STALE_DAYS})).rowcount


async def link_companies(s: AsyncSession) -> int:
    """Remote-board jobs get their company when the event's company is resolved (a later worker step)."""
    return (await s.execute(text("""UPDATE job_postings j SET company_id = e.company_id, updated_at = now() FROM source_events e
        WHERE j.source_event_id = e.id AND j.company_id IS NULL AND e.company_id IS NOT NULL"""))).rowcount


async def maintain(s: AsyncSession) -> dict:
    return {"closed_stale": await close_stale(s), "linked": await link_companies(s)}


BACKFILL = """
INSERT INTO job_postings (source_id, ats_board_id, company_id, company_name, source_event_id, external_job_id, title, url, location, country,
                          is_remote, workplace, posted_at, first_seen_at, last_seen_at, status)
SELECT e.source_id, b.id, coalesce(b.company_id, e.company_id), coalesce(c.name, e.company_name), e.id, left(e.source_event_id, 200), e.title,
       coalesce(e.canonical_url, e.source_url), e.location, e.country, CASE WHEN e.raw_metadata->>'remote' = 'true' THEN true END, e.raw_metadata->>'workplace', e.published_at,
       e.detected_at, e.detected_at, 'open'
FROM source_events e JOIN source_registry r ON r.id = e.source_id
LEFT JOIN ats_boards b ON r.connector_type = 'api' AND b.provider || ':' || b.board_token = r.name
LEFT JOIN companies c ON c.id = b.company_id
WHERE e.event_type = 'job' AND e.source_event_id IS NOT NULL
ON CONFLICT (source_id, external_job_id) DO NOTHING"""

AUDIT = """
SELECT (SELECT count(*) FROM source_events WHERE event_type = 'job') AS job_events,
       (SELECT count(*) FROM source_events WHERE event_type = 'job' AND source_event_id IS NULL) AS no_event_id,
       (SELECT count(*) FROM (SELECT DISTINCT source_id, left(source_event_id, 200) FROM source_events
            WHERE event_type = 'job' AND source_event_id IS NOT NULL) d) AS expected_records,
       (SELECT count(*) FROM job_postings) AS records,
       (SELECT count(*) FROM source_events e WHERE e.event_type = 'job' AND e.source_event_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM job_postings j WHERE j.source_id = e.source_id AND j.external_job_id = left(e.source_event_id, 200))) AS events_without_record,
       (SELECT count(*) FROM job_postings WHERE status = 'open') AS open,
       (SELECT count(*) FROM job_postings WHERE status = 'closed') AS closed"""


async def backfill(s: AsyncSession) -> int:
    return (await s.execute(text(BACKFILL))).rowcount


async def audit(s: AsyncSession) -> dict:
    return dict((await s.execute(text(AUDIT))).mappings().one())
