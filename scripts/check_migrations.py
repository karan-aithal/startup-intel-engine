"""Run every Alembic migration in a scratch schema and compare the result with the ORM models (compare_metadata, expect no diffs).
Usage: cd backend && uv run python ../scripts/check_migrations.py [--no-roundtrip]
Pytest builds its tables from the models and never runs migrations, so a migration that disagrees with the models is invisible to it.
The scratch schema gets its own alembic_version (otherwise Alembic sees public.alembic_version through the search_path and alters the REAL
tables, which happened once, see docs/decisions.md). public is fingerprinted before and after; any change fails the run. Take a backup first."""
import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import sqlalchemy.ext.asyncio as sa_async
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401
from alembic import command
from app.core.config import get_settings
from app.db.base import Base

SCHEMA = f"mig_{uuid.uuid4().hex[:8]}"
FINGERPRINT = """SELECT 'col', table_name, column_name, data_type, is_nullable, coalesce(column_default, '') FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL SELECT 'idx', tablename, indexname, indexdef, '', '' FROM pg_indexes WHERE schemaname = 'public'
UNION ALL SELECT 'con', conrelid::regclass::text, conname, pg_get_constraintdef(oid), '', '' FROM pg_constraint WHERE connamespace = 'public'::regnamespace
UNION ALL SELECT 'ver', version_num, '', '', '', '' FROM public.alembic_version ORDER BY 1, 2, 3"""


async def fingerprint(eng) -> str:
    async with eng.connect() as c:
        rows = (await c.exec_driver_sql(FINGERPRINT)).all()
    return hashlib.sha256(repr(sorted(map(tuple, rows))).encode()).hexdigest()


async def main(roundtrip: bool) -> int:
    url = get_settings().database_url
    admin = create_async_engine(url)
    before = await fingerprint(admin)
    real = sa_async.async_engine_from_config

    def scratch_engine(*a, **kw):  # env.py imports this name when it runs, so the migrations land in the scratch schema
        kw["connect_args"] = {"server_settings": {"search_path": f"{SCHEMA},public"}}
        return real(*a, **kw)

    async with admin.begin() as c:
        await c.exec_driver_sql(f"CREATE SCHEMA {SCHEMA}")
        await c.exec_driver_sql(f"CREATE TABLE {SCHEMA}.alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)")
    code = 0
    try:
        sa_async.async_engine_from_config = scratch_engine
        cfg = Config(str(ROOT / "backend/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "backend/alembic"))
        await asyncio.to_thread(command.upgrade, cfg, "head")
        if roundtrip:
            await asyncio.to_thread(command.downgrade, cfg, "-1")
            await asyncio.to_thread(command.upgrade, cfg, "head")
        eng = create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})  # scratch only: tables must not resolve to public
        async with eng.connect() as c:
            diffs = await c.run_sync(lambda sc: compare_metadata(MigrationContext.configure(sc), Base.metadata))
            head = (await c.exec_driver_sql("SELECT version_num FROM alembic_version")).scalar_one()
        await eng.dispose()
        print(f"scratch schema {SCHEMA} migrated to {head}{' (with a one-step downgrade and upgrade)' if roundtrip else ''}")
        print(f"compare_metadata diffs: {len(diffs)}")
        for d in diffs:
            print("  ", d)
        code = 1 if diffs else 0
    finally:
        sa_async.async_engine_from_config = real
        async with admin.begin() as c:
            await c.exec_driver_sql(f"DROP SCHEMA {SCHEMA} CASCADE")
    after = await fingerprint(admin)
    await admin.dispose()
    print("public schema unchanged" if before == after else "PUBLIC CHANGED: restore from the backup")
    return code or (0 if before == after else 2)


sys.exit(asyncio.run(main("--no-roundtrip" not in sys.argv)))
