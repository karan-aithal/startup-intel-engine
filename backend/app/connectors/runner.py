"""Run one collection cycle for a registered adapter, honouring the registry."""
import time

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.connectors.base import SourceAdapter
from app.services import registry
from app.services.ingest import ingest_batch, load_cursor

log = structlog.get_logger()


async def record_poll(s, source_id: str, interval: int, *, ok: bool, items: int | None) -> None:
    """Persist poll status for discovered feeds and ATS boards: last poll, last success, next scheduled poll, job count."""
    p = {"i": source_id, "iv": interval, "ok": ok, "n": items}
    await s.execute(text("""UPDATE company_sources SET last_polled_at = now(), next_poll_at = now() + make_interval(secs => :iv),
        verified_at = CASE WHEN CAST(:ok AS boolean) THEN coalesce(verified_at, now()) ELSE verified_at END WHERE source_id = :i"""), p)
    await s.execute(text("""UPDATE ats_boards SET last_polled_at = now(), next_poll_at = now() + make_interval(secs => :iv),
        last_success_at = CASE WHEN CAST(:ok AS boolean) THEN now() ELSE last_success_at END,
        last_job_count = CASE WHEN CAST(:ok AS boolean) THEN CAST(:n AS integer) ELSE last_job_count END
        WHERE provider || ':' || board_token = (SELECT name FROM source_registry WHERE id = :i AND connector_type = 'api')"""), p)


async def run_once(sm: async_sessionmaker, adapter: SourceAdapter, scope: str = "") -> dict:
    """Skips if the route is disabled. A failure here affects only this source."""
    async with sm() as s:
        row = (await s.execute(
            text("SELECT id, enabled, poll_interval_seconds FROM source_registry WHERE name = :n AND connector_type = :t"),
            {"n": adapter.name, "t": adapter.connector_type},
        )).first()
        if row is None:
            raise LookupError(f"{adapter.name}/{adapter.connector_type} is not registered")
        source_id, interval = str(row.id), row.poll_interval_seconds or 900
        if not row.enabled:
            return {"status": "skipped", "reason": "disabled"}
        cursor = await load_cursor(s, source_id, scope)
        run_id = (await s.execute(
            text("INSERT INTO source_runs (source_id) VALUES (:id) RETURNING id"), {"id": source_id}
        )).scalar_one()
        await s.commit()

    t0 = time.monotonic()
    try:
        result = await adapter.fetch(cursor)
        events = []
        for i in result.items:
            out = await adapter.normalize(i)  # one item may yield several events (an alert email lists many jobs)
            events.extend(out if isinstance(out, list) else [out] if out else [])
        async with sm() as s:
            new, dup = await ingest_batch(s, source_id, events, result.cursor, scope)
            await registry.record_success(s, source_id)
            await record_poll(s, source_id, interval, ok=True, items=len(result.items))
            await s.execute(
                text("""UPDATE source_runs SET status='ok', finished_at=now(), items_fetched=:f, items_new=:n,
                        latency_ms=:ms WHERE id=:id"""),
                {"f": len(result.items), "n": new, "ms": int((time.monotonic() - t0) * 1000), "id": run_id},
            )
            await s.commit()
        return {"status": "ok", "fetched": len(result.items), "new": new, "duplicates": dup}
    except Exception as exc:  # noqa: BLE001 - isolate any connector failure
        log.warning("source_failed", source=adapter.name, error=str(exc))
        async with sm() as s:
            await registry.record_failure(s, source_id, f"{type(exc).__name__}: {exc}")
            await record_poll(s, source_id, interval, ok=False, items=None)
            await s.execute(
                text("UPDATE source_runs SET status='error', finished_at=now(), error=:e WHERE id=:id"),
                {"e": str(exc)[:2000], "id": run_id},
            )
            await s.commit()
        return {"status": "error", "error": str(exc)}
