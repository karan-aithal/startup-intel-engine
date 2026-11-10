import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_live(client):
    r = await client.get("/health/live")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_ready_reports_degraded_when_db_down(client, monkeypatch):
    async def boom():
        raise ConnectionRefusedError

    monkeypatch.setattr("app.api.health.check_db", boom)
    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["database"]["ok"] is False


def test_bad_database_url_rejected():
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(database_url="mysql://x")
