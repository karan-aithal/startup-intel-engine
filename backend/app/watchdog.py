"""Failure alerting from outside the worker, so a dead worker still gets reported.

Run by cron (scripts/install_cron.sh) or by hand: cd backend && uv run python -m app.watchdog
Queues an ops alert for each new problem, delivers the outbox once, prints every current problem, and exits 1 if there are any.
"""
import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine, get_sessionmaker
from app.services import ops
from app.services.notify import deliver_pending, make_sink


async def main() -> int:
    cfg = get_settings()
    setup_logging(cfg.log_level)
    sm = get_sessionmaker()
    try:
        async with sm() as s:
            found = await ops.problems(s, llm_enabled=cfg.llm_enabled, backup_dir=Path(cfg.backup_dir).expanduser())
        await ops.watch(sm, cfg)
        await deliver_pending(sm, make_sink(cfg), cfg.display_timezone)  # the worker may be the thing that is down
        for _, msg in found:
            print(msg)
        return 1 if found else 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
