"""Verify Alembic migrations in an isolated scratch schema. The real `public` schema is never written to.

Usage: cd backend && env -u PYTHONPATH uv run python ../scripts/migration_check.py empty|copy|live|cleanup
  empty    scratch schema, migrate 0001 -> head; drift + constraint/index names vs the ORM; downgrade/upgrade round trip
  copy     scratch schema at 0002 loaded with a copy of the real rows, migrate to head; row counts + checksums must not change
  live     read-only: drift + names of the real database against the ORM
  cleanup  drop the scratch schemas
Exits non-zero on any failure. Needs no CREATEDB: scratch schemas live in the same database (search_path scratch,public).

Why the scratch schema gets its own alembic_version: Alembic's has_table() sees public.alembic_version through the search_path,
skips every migration already recorded there and runs ALTERs against the real tables. Each run also fingerprints `public`
(columns, indexes, constraints, version) before and after and fails if it changed. pytest does NOT cover migrations: the suite
builds its schema from the ORM, so only this script catches migration/model mismatches."""
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import sqlalchemy.ext.asyncio as sa_async
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import app.db.models  # noqa: F401  (register tables)
from app.core.config import get_settings
from app.db.base import Base

URL = get_settings().database_url
SCHEMA = {"empty": "mig_empty", "copy": "mig_copy"}
FAILS: list[str] = []
FP = ("select md5(coalesce(string_agg(x, '|' order by x), '')) from ("
      "select table_name||'.'||column_name||':'||data_type||coalesce(column_default,'') x from information_schema.columns where table_schema='public' "
      "union all select indexdef from pg_indexes where schemaname='public' "
      "union all select conrelid::regclass||pg_get_constraintdef(oid) from pg_constraint where connamespace='public'::regnamespace "
      "union all select 'v='||version_num from public.alembic_version) t")
COLS = ("select column_name from information_schema.columns where table_schema=:s and table_name=:t "
        "and is_generated='NEVER' order by ordinal_position")


def check(ok: bool, msg: str) -> None:
    print(("ok    " if ok else "FAIL  ") + msg)
    if not ok:
        FAILS.append(msg)


def engine(schema: str):
    return create_async_engine(URL, connect_args={"server_settings": {"search_path": f"{schema},public"}})


async def sql(schema, q, **p):
    eng = engine(schema)
    async with eng.begin() as c:
        r = await c.execute(text(q), p)
        rows = r.fetchall() if r.returns_rows else []
    await eng.dispose()
    return rows


async def drift(schema):
    eng = engine(schema)
    async with eng.connect() as c:
        diffs = await c.run_sync(lambda sc: compare_metadata(
            MigrationContext.configure(sc, opts={"compare_type": True, "compare_server_default": False}), Base.metadata))
    await eng.dispose()
    return diffs


def orm_names() -> set[str]:
    """Constraint and index names the ORM would create (naming convention applied)."""
    d, names = postgresql.dialect(), set()
    for t in Base.metadata.sorted_tables:
        names |= set(re.findall(r"CONSTRAINT (\w+)", str(CreateTable(t).compile(dialect=d))))
        names |= {re.search(r"INDEX (\w+)", str(CreateIndex(i).compile(dialect=d))).group(1) for i in t.indexes}
    return names


async def check_schema(schema: str, label: str) -> None:
    d = await drift(schema)
    check(not d, f"{label}: no drift against ORM metadata" + ("" if not d else f" ({len(d)}: {d[:3]})"))
    db = {r[0] for r in await sql(schema, f"select conname from pg_constraint where connamespace = '{schema}'::regnamespace and contype in ('p','f','u','c')")}
    db |= {r[0] for r in await sql(schema, f"select indexname from pg_indexes where schemaname = '{schema}'")}
    db.discard("alembic_version_pkc")
    want = orm_names()
    check(db == want, f"{label}: constraint/index names match ORM" + ("" if db == want else f" (only in db: {sorted(db - want)}; only in ORM: {sorted(want - db)})"))


def alembic(schema: str, action: str, rev: str) -> None:
    orig = sa_async.async_engine_from_config  # env.py builds its own engine; pin it to the scratch schema

    def pinned(cfg, prefix="sqlalchemy.", **kw):
        kw["connect_args"] = {"server_settings": {"search_path": f"{schema},public"}}
        return orig(cfg, prefix=prefix, **kw)
    sa_async.async_engine_from_config = pinned
    try:
        c = Config("alembic.ini")
        c.set_main_option("script_location", "alembic")
        getattr(command, action)(c, rev)
    finally:
        sa_async.async_engine_from_config = orig


async def version(schema):
    return (await sql(schema, "select version_num from alembic_version"))[0][0]


async def run_empty(schema):
    await asyncio.to_thread(alembic, schema, "upgrade", "head")
    head = await version(schema)
    print("head revision:", head)
    await check_schema(schema, "fresh upgrade")
    await asyncio.to_thread(alembic, schema, "downgrade", "0002")
    check(await version(schema) == "0002", "downgrade to 0002")
    await asyncio.to_thread(alembic, schema, "upgrade", "head")
    check(await version(schema) == head, "re-upgrade to head")
    await check_schema(schema, "after round trip")


async def run_copy(schema):
    await asyncio.to_thread(alembic, schema, "upgrade", "0002")
    tabs = [r[0] for r in await sql("public", "select relname from pg_stat_user_tables where schemaname='public' and relname<>'alembic_version' and n_live_tup>0")]
    pending, last = list(tabs), ""
    for _ in range(6):  # FK order: retry until every table is in
        for t in list(pending):
            lst = ", ".join(f'"{c[0]}"' for c in await sql(schema, COLS, s=schema, t=t))
            try:
                await sql(schema, f'insert into {schema}."{t}" ({lst}) select {lst} from public."{t}"')
                pending.remove(t)
            except Exception as e:  # noqa: BLE001
                last = str(e)[:160]
    check(not pending, f"copied {len(tabs)} populated tables" + (f" (stuck: {pending}: {last})" if pending else ""))
    await asyncio.to_thread(alembic, schema, "upgrade", "head")
    await check_schema(schema, "migrated copy")
    for t in tabs:  # compare only the columns that existed before the migration
        old = [r[0] for r in await sql("public", COLS, s="public", t=t)]
        lst = ", ".join(f'x."{c}"' for c in old)

        def q(s):
            return f"select count(*), md5(string_agg(row({lst})::text, ',' order by row({lst})::text)) from {s}.\"{t}\" x"
        a, b = (await sql("public", q("public")))[0], (await sql(schema, q(schema)))[0]
        check(a == b, f"{t}: {a[0]} rows, data unchanged")


async def main(mode):
    if mode == "cleanup":
        for s in SCHEMA.values():
            await sql("public", f"drop schema if exists {s} cascade")
        print("dropped", *SCHEMA.values())
        return
    before = (await sql("public", FP))[0][0]
    try:
        if mode == "live":
            await check_schema("public", "real database")
        else:
            schema = SCHEMA[mode]
            await sql("public", f"drop schema if exists {schema} cascade")
            await sql("public", f"create schema {schema}")
            await sql("public", f"create table {schema}.alembic_version (version_num varchar(32) not null, "
                                "constraint alembic_version_pkc primary key (version_num))")
            await (run_empty if mode == "empty" else run_copy)(schema)
    finally:
        after = (await sql("public", FP))[0][0]
        check(before == after, "public schema untouched by this run")
    print("\n" + ("FAILED: " + "; ".join(FAILS) if FAILS else "all checks passed"))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("empty", "copy", "live", "cleanup"):
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
    sys.exit(1 if FAILS else 0)
