"""Run one collection cycle for a registered adapter, honouring the registry."""
import time

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.connectors.base import SourceAdapter
from app.services import registry
from app.services.ingest import ingest_batch, load_cursor

log = structlog.get_logger()


async def run_once(sm: async_sessionmaker, adapter: SourceAdapter, scope: str = "") -> dict:
    """Skips if the route is disabled. A failure here affects only this source."""
    async with sm() as s:
        row = (await s.execute(
            text("SELECT id, enabled FROM source_registry WHERE name = :n AND connector_type = :t"),
            {"n": adapter.name, "t": adapter.connector_type},
        )).first()
        if row is None:
            raise LookupError(f"{adapter.name}/{adapter.connector_type} is not registered")
        source_id = str(row.id)
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
        events = [e for e in [await adapter.normalize(i) for i in result.items] if e is not None]
        async with sm() as s:
            new, dup = await ingest_batch(s, source_id, events, result.cursor, scope)
            await registry.record_success(s, source_id)
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
            await s.execute(
                text("UPDATE source_runs SET status='error', finished_at=now(), error=:e WHERE id=:id"),
                {"e": str(exc)[:2000], "id": run_id},
            )
            await s.commit()
        return {"status": "error", "error": str(exc)}
