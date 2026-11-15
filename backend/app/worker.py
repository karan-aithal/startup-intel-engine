"""Worker process: scheduler (poll due sources) + job worker (classify events) + alert delivery.

Run: cd backend && uv run python -m app.worker
"""
import asyncio
import os
import socket

import structlog
from sqlalchemy import text

from app.api.watchlist import ensure_bluesky
from app.connectors.bluesky import stream_forever
from app.connectors.factory import build_adapter
from app.connectors.runner import run_once
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine, get_sessionmaker
from app.services import queue
from app.services.discovery import discover_pending
from app.services.notify import deliver_pending, make_sink
from app.services.pipeline import process_event

log = structlog.get_logger()
WORKER = f"{socket.gethostname()}-{os.getpid()}"

DUE_SQL = text("""
    SELECT r.name, r.config FROM source_registry r
    WHERE r.enabled AND r.connector_type <> 'stream' AND coalesce((SELECT max(started_at) FROM source_runs WHERE source_id = r.id), 'epoch')
          + make_interval(secs => coalesce(r.poll_interval_seconds, 900)) <= now()""")


async def poll_due(sm, sec_email: str, imap: dict | None = None, bot: dict | None = None, concurrency: int = 5) -> list[dict]:
    """One scheduler pass. Each source runs isolated; run_once records its own failure."""
    async with sm() as s:
        due = (await s.execute(DUE_SQL)).all()
    sem = asyncio.Semaphore(concurrency)

    async def one(name, config):
        async with sem:
            try:
                return await run_once(sm, build_adapter(name, config, sec_email=sec_email, imap=imap, bot=bot))
            except Exception as exc:  # noqa: BLE001 - bad config for one source must not stop the rest
                log.warning("source_build_failed", source=name, error=str(exc))
                return {"status": "error", "error": str(exc)}

    return await asyncio.gather(*(one(n, c) for n, c in due))


def imap_cfg(cfg) -> dict:
    return {"host": cfg.imap_host, "user": cfg.imap_user, "password": cfg.imap_password, "folder": cfg.imap_folder}


def bot_cfg(cfg) -> dict:
    return {"token": cfg.telegram_bot_token, "allowed_ids": {i for i in (cfg.telegram_owner_id,) if i}}


async def process_jobs(sm, limit: int = 100) -> int:
    """Claim, handle and complete in one transaction per job, so a crash re-queues instead of losing it."""
    async with sm() as s:
        jobs = await queue.claim(s, WORKER, kinds=["process_event"], limit=limit)
        await s.commit()
    for j in jobs:
        try:
            async with sm() as s:
                await process_event(s, j["payload"]["event_id"])
                await queue.complete(s, str(j["id"]), WORKER)
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            async with sm() as s:
                await queue.fail(s, str(j["id"]), WORKER, f"{type(exc).__name__}: {exc}")
                await s.commit()
    return len(jobs)


async def _loop(name: str, fn, every_s: float):
    while True:
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("loop_error", loop=name)
        await asyncio.sleep(every_s)


async def main() -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level, json_logs=cfg.app_env == "prod")
    sm, sink = get_sessionmaker(), make_sink(cfg)

    async def beat():
        async with sm() as s:
            await queue.heartbeat(s, WORKER, {"sink": type(sink).__name__})
            await s.commit()

    log.info("worker_start", worker=WORKER, sink=type(sink).__name__)
    try:
        await _run(sm, sink, cfg, beat)
    finally:
        await get_engine().dispose()


async def _streams(sm) -> list[tuple[str, dict]]:
    """Stream routes (enabled or not): each idles until enabled and has watched accounts."""
    async with sm() as s:
        await ensure_bluesky(s)
        await s.commit()
        rows = (await s.execute(text("SELECT name, config FROM source_registry WHERE connector_type = 'stream'"))).all()
    return [(r.name, r.config) for r in rows]


async def _run(sm, sink, cfg, beat):
    await asyncio.gather(
        _loop("scheduler", lambda: poll_due(sm, cfg.sec_contact_email, imap_cfg(cfg), bot_cfg(cfg)), 30),
        _loop("jobs", lambda: process_jobs(sm), 2),
        _loop("alerts", lambda: deliver_pending(sm, sink, cfg.display_timezone), 5),
        _loop("heartbeat", beat, 30),
        _loop("discovery", lambda: discover_pending(sm), 600),
        *(stream_forever(sm, n, c) for n, c in await _streams(sm)),
    )


if __name__ == "__main__":
    asyncio.run(main())
