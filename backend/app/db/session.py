from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def check_db() -> dict:
    """Return DB status, server version, installed extensions and migration revision."""
    async with get_engine().connect() as conn:
        version = (await conn.execute(text("show server_version"))).scalar_one()
        exts = (await conn.execute(text("select extname from pg_extension"))).scalars().all()
        try:
            rev = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        except Exception:  # noqa: BLE001 - table absent before first migration
            rev = None
    return {"server_version": version, "extensions": sorted(exts), "migration": rev}
