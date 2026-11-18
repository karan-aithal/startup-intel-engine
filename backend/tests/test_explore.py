"""/api/explore: India and remote-open-to-India jobs only, text filters, date window, source filter, paging."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.api import intel
from app.services import registry

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def use_test_db(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)


async def job(s, source, eid, title, location, days=1, meta="{}", company="Acme"):
    sid = await registry.register_source(s, source, "api")
    await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, company_name, raw_metadata, published_at, detected_at)
        VALUES (:s, :i, 'job', :t, :l, :c, CAST(:m AS jsonb), :p, :p)"""),
        {"s": sid, "i": eid, "t": title, "l": location, "c": company, "m": meta, "p": NOW - timedelta(days=days)})


@pytest.fixture
async def seeded(sm):
    async with sm() as s:
        await job(s, "greenhouse:acme", "1", "Backend Engineer", "Bengaluru, India")
        await job(s, "greenhouse:acme", "2", "Data Scientist", "Remote, US")  # remote but restricted: never listed
        await job(s, "greenhouse:acme", "3", "Designer", "London")  # neither
        await job(s, "Himalayas", "4", "Platform Engineer", "Remote (worldwide)", meta='{"remote": true}', company="Globex")
        await job(s, "Himalayas", "5", "Senior Backend Engineer", "Remote (India)", company="Globex")
        await job(s, "Jooble India", "6", "Sales Manager", None, meta='{"region": "india"}', company="Initech")  # India by source region alone
        await job(s, "lever:old", "7", "Old Role", "Pune", days=10)
        await job(s, "lever:old", "8", None, "Remote")  # no title: must not crash the text filters
        await s.commit()


def titles(d):
    return sorted(j["title"] or "" for j in d["items"])


async def test_only_india_and_remote_open_to_india_are_listed(sm, seeded):
    d = await intel.explore("all", "", "", "", "", 7, 50, 0)
    assert titles(d) == ["", "Backend Engineer", "Platform Engineer", "Sales Manager", "Senior Backend Engineer"]
    assert d["counts"] == {"all": 5, "india": 3, "remote": 3}  # "Remote (India)" is both; London and "Remote, US" are absent
    by = {j["title"]: j for j in d["items"]}
    assert by["Senior Backend Engineer"]["india"] and by["Senior Backend Engineer"]["remote"]
    assert by["Sales Manager"]["india"] and not by["Sales Manager"]["remote"]
    assert "region_india" not in by["Backend Engineer"]


async def test_mode_tabs_partition_the_list(sm, seeded):
    india = await intel.explore("india", "", "", "", "", 7, 50, 0)
    remote = await intel.explore("remote", "", "", "", "", 7, 50, 0)
    assert titles(india) == ["Backend Engineer", "Sales Manager", "Senior Backend Engineer"]
    assert titles(remote) == ["", "Platform Engineer", "Senior Backend Engineer"]
    assert india["counts"] == remote["counts"] == {"all": 5, "india": 3, "remote": 3}  # counts ignore the mode


async def test_text_filters_window_and_source(sm, seeded):
    assert titles(await intel.explore("all", "backend, data", "", "", "", 7, 50, 0)) == ["Backend Engineer", "Senior Backend Engineer"]
    assert titles(await intel.explore("all", "engineer", "senior", "", "", 7, 50, 0)) == ["Backend Engineer", "Platform Engineer"]
    assert titles(await intel.explore("all", "", "", "bengaluru", "", 7, 50, 0)) == ["Backend Engineer"]
    assert titles(await intel.explore("all", "", "", "worldwide, pune", "", 14, 50, 0)) == ["Old Role", "Platform Engineer"]  # 14 days reaches the Pune job
    him = await intel.explore("all", "", "", "", "Himalayas", 7, 50, 0)
    assert titles(him) == ["Platform Engineer", "Senior Backend Engineer"] and him["counts"]["all"] == 5
    assert {x["source"]: x["jobs"] for x in him["sources"]} == {"greenhouse:acme": 1, "Himalayas": 2, "Jooble India": 1, "lever:old": 1}


async def test_paging(sm, seeded):
    first = await intel.explore("all", "", "", "", "", 7, 2, 0)
    rest = await intel.explore("all", "", "", "", "", 7, 2, 2)
    assert first["total"] == rest["total"] == 5 and len(first["items"]) == 2 and len(rest["items"]) == 2
    assert not {j["id"] for j in first["items"]} & {j["id"] for j in rest["items"]}
    assert first["truncated"] is False
