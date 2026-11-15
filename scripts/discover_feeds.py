"""Run feed auto-discovery now. Usage: cd backend && uv run python ../scripts/discover_feeds.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import get_engine, get_sessionmaker
from app.services.discovery import discover_pending


async def main():
    print(f"stored {await discover_pending(get_sessionmaker(), limit=1000)} new company sources")
    await get_engine().dispose()


asyncio.run(main())
