"""DB tests run in a throwaway schema inside the real database; dev data is never touched."""
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base

URL = get_settings().database_url


@pytest.fixture(scope="session")
async def schema():
    name = f"test_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(URL)
    async with admin.begin() as c:
        await c.exec_driver_sql(f"CREATE SCHEMA {name}")
    eng = create_async_engine(URL, connect_args={"server_settings": {"search_path": f"{name},public"}})
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await eng.dispose()
    yield name
    async with admin.begin() as c:
        await c.exec_driver_sql(f"DROP SCHEMA {name} CASCADE")
    await admin.dispose()


def make_engine(schema: str):
    return create_async_engine(URL, connect_args={"server_settings": {"search_path": f"{schema},public"}})


@pytest.fixture
async def engine(schema):
    eng = make_engine(schema)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sm(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def clean(schema, request):
    """Empty all tables before each DB test."""
    if not {"sm", "engine", "schema"} & set(request.fixturenames):
        yield
        return
    eng = make_engine(schema)
    async with eng.begin() as c:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await c.exec_driver_sql(f"TRUNCATE {tables} CASCADE")
    await eng.dispose()
    yield
