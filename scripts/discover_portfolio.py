"""Seed company candidates from a VC/accelerator portfolio page. Usage: cd backend && uv run python ../scripts/discover_portfolio.py URL"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import get_engine, get_sessionmaker
from app.services.ats import discover_portfolio


async def main(url: str):
    async with get_sessionmaker()() as s, httpx.AsyncClient(timeout=20) as c:
        n = await discover_portfolio(s, url, c)
        await s.commit()
    print(f"{n} candidates added (resolved by the worker's companies loop)")
    await get_engine().dispose()


asyncio.run(main(sys.argv[1]))
