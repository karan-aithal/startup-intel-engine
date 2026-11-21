"""Worker process: scheduler (poll due sources) + job worker (classify events) + alert delivery.

Run: cd backend && uv run python -m app.worker
"""
import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import text

from app.api.watchlist import ensure_bluesky
from app.connectors.bluesky import stream_forever
from app.connectors.factory import build_adapter
from app.connectors.runner import run_once
from app.core.config import get_settings
from app.core.logging import env_file_warning, setup_logging
from app.db.session import get_engine, get_sessionmaker
from app.services import ats, companies, jobs, ops, queue, retention
from app.services.correlate import correlate_all
from app.services.discovery import discover_pending
from app.services.expansion import demote_sources, expand_sources, sync_github_orgs
from app.services.llm import enrich_event
from app.services.notify import deliver_pending, make_sink
from app.services.pipeline import process_event
from app.services.scheduler import DUE_SQL, update_activity
from app.services.verify import verify_all

log = structlog.get_logger()
WORKER = f"{socket.gethostname()}-{os.getpid()}"


async def poll_due(sm, sec_email: str, imap: dict | None = None, bot: dict | None = None, concurrency: int = 5, secrets: dict | None = None) -> list[dict]:
    """One scheduler pass. Each source runs isolated; run_once records its own failure."""
    async with sm() as s:
        due = (await s.execute(DUE_SQL)).all()
    sem = asyncio.Semaphore(concurrency)

    async def one(name, config):
        async with sem:
            try:
                return await run_once(sm, build_adapter(name, config, sec_email=sec_email, imap=imap, bot=bot, secrets=secrets))
            except Exception as exc:  # noqa: BLE001 - bad config for one source must not stop the rest
                log.warning("source_build_failed", source=name, error=str(exc))
                return {"status": "error", "error": str(exc)}

    return await asyncio.gather(*(one(n, c) for n, c in due))


def imap_cfg(cfg) -> dict:
    return {"host": cfg.imap_host, "user": cfg.imap_user, "password": cfg.imap_password, "folder": cfg.imap_folder}


def secrets_cfg(cfg) -> dict:
    return {"github_token": cfg.github_token, "jooble_api_key": cfg.jooble_api_key, "jooble_base_url": cfg.jooble_base_url}


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


async def portal_checks(sm, cfg) -> int:
    """The owner's "re-check now" (J4): poll the route at once, outside its schedule. run_once records success or failure the usual way, so a
    success closes an open circuit."""
    async with sm() as s:
        jobs = await queue.claim(s, WORKER, kinds=["portal_check"], limit=3, lease_s=300)
        await s.commit()
    for j in jobs:
        try:
            async with sm() as s:
                row = (await s.execute(text("SELECT name, config FROM source_registry WHERE id = CAST(:i AS uuid)"), {"i": j["payload"]["route_id"]})).first()
            if row:
                await run_once(sm, build_adapter(row.name, row.config, sec_email=cfg.sec_contact_email, imap=imap_cfg(cfg), bot=bot_cfg(cfg),
                                                 secrets=secrets_cfg(cfg)))
            async with sm() as s:
                await queue.complete(s, str(j["id"]), WORKER)
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            async with sm() as s:
                await queue.fail(s, str(j["id"]), WORKER, f"{type(exc).__name__}: {exc}")
                await s.commit()
    return len(jobs)


async def llm_jobs(sm, cfg) -> int:
    """One job at a time on its own loop, so model latency cannot delay process_jobs or alert delivery."""
    async with sm() as s:
        jobs = await queue.claim(s, WORKER, kinds=["llm_enrich"], limit=1, lease_s=900)
        await s.commit()
    async with httpx.AsyncClient() as c:
        for j in jobs:
            try:
                async with sm() as s:
                    out = await enrich_event(s, j["payload"]["event_id"], c, cfg.llm_base_url, cfg.llm_model, cfg.llm_embed_model,
                                             cfg.llm_embed_base_url)
                    if out == "retry":  # server problem: keep the attempt records, then re-queue with backoff
                        await s.commit()
                        raise ConnectionError("model server unavailable or timed out")
                    await queue.complete(s, str(j["id"]), WORKER)
                    await s.commit()
            except Exception as exc:  # noqa: BLE001 - model server down: back off and retry
                async with sm() as s:
                    await queue.fail(s, str(j["id"]), WORKER, f"{type(exc).__name__}: {exc}")
                    await s.commit()
    return len(jobs)


async def verification(sm) -> None:
    """Claims -> evidence -> funding rounds, then correlation signals and scores. Never touches the alert path."""
    log.info("verification", rounds=await verify_all(sm), companies_correlated=await correlate_all(sm))


async def maintenance(sm, cfg) -> None:
    """Hot-company polling rates, and the monthly Common Crawl seeding run (when that route is enabled)."""
    async with sm() as s:
        await update_activity(s)
        await demote_sources(s)
        await expand_sources(s, cfg.auto_source_cap)
        await sync_github_orgs(s)
        await ats.demote_stale(s)
        await ats.promote_candidates(s, cfg.auto_watch_cap, cfg.auto_board_cap)
        await ats.refresh_hot_polling(s)
        log.info("retention", **await retention.purge(s))
        log.info("job_records", **await jobs.maintain(s))
        row = (await s.execute(text("""SELECT r.id, r.enabled, (SELECT max(started_at) FROM source_runs WHERE source_id = r.id) AS last
            FROM source_registry r WHERE r.name = 'Common Crawl ATS' AND r.connector_type = 'discovery'"""))).first()
        await s.commit()
    if row and row.enabled and (row.last is None or datetime.now(UTC) - row.last > timedelta(days=30)):
        async with sm() as s:
            rid = (await s.execute(text("INSERT INTO source_runs (source_id) VALUES (:i) RETURNING id"), {"i": row.id})).scalar_one()
            await s.commit()
        try:
            async with sm() as s, httpx.AsyncClient(timeout=60) as c:
                new = await ats.seed_from_common_crawl(s, c)
                await s.execute(text("UPDATE source_runs SET status='ok', finished_at=now(), items_new=:n WHERE id=:i"), {"n": new, "i": rid})
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            async with sm() as s:
                await s.execute(text("UPDATE source_runs SET status='error', finished_at=now(), error=:e WHERE id=:i"), {"e": str(exc)[:500], "i": rid})
                await s.commit()


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
    if msg := env_file_warning():
        log.warning("env_permissions", detail=msg)
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
        _loop("scheduler", lambda: poll_due(sm, cfg.sec_contact_email, imap_cfg(cfg), bot_cfg(cfg), secrets=secrets_cfg(cfg)), 30),
        _loop("jobs", lambda: process_jobs(sm), 2),
        _loop("portal_checks", lambda: portal_checks(sm, cfg), 5),
        _loop("alerts", lambda: deliver_pending(sm, sink, cfg.display_timezone), 5),
        _loop("heartbeat", beat, 30),
        _loop("discovery", lambda: discover_pending(sm), 600),
        _loop("companies", lambda: companies.resolve_pending(sm), 60),
        _loop("ats", lambda: ats.detect_pending(sm, cfg.auto_watch_cap, cfg.auto_board_cap), 120),
        *([_loop("llm", lambda: llm_jobs(sm, cfg), 3)] if cfg.llm_enabled else []),
        _loop("maintenance", lambda: maintenance(sm, cfg), 3600),
        _loop("verification", lambda: verification(sm), 300),
        _loop("watch", lambda: ops.watch(sm, cfg), 300),
        *(stream_forever(sm, n, c) for n, c in await _streams(sm)),
    )


if __name__ == "__main__":
    asyncio.run(main())
