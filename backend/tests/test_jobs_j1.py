"""Jobs track J1: the write guard and the job record (job_postings): upsert at ingest, closing, retention, backfill."""
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from app.api import watchlist
from app.connectors.api import AtsAdapter
from app.connectors.base import NormalizedEvent
from app.connectors.runner import run_once
from app.core.config import get_settings
from app.main import app
from app.services import jobs, registry
from app.services.ingest import ingest_batch
from app.services.retention import purge

OK = "http://127.0.0.1:3010"  # in CORS_ORIGINS


# ---------- the write guard ----------

@pytest.fixture
def api(sm, monkeypatch):
    monkeypatch.setattr(watchlist, "get_sessionmaker", lambda: sm)
    assert OK in get_settings().cors_list
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")


async def test_guard_rejects_cross_origin_and_non_json_and_accepts_same_origin(api):
    body = {"value": "Some_Channel"}
    hdr = {"Origin": OK, "Content-Type": "application/json"}
    r = await api.post("/api/watchlist/telegram", json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "Origin" in r.json()["detail"]
    assert (await api.post("/api/watchlist/telegram", json=body)).status_code == 403  # no Origin at all
    assert (await api.post("/api/watchlist/telegram", content='{"value": "Some_Channel"}', headers={"Origin": OK, "Content-Type": "text/plain"})).status_code == 415
    assert (await api.post("/api/watchlist/telegram", content="value=Some_Channel", headers={"Origin": OK, "Content-Type": "application/x-www-form-urlencoded"})).status_code == 415
    assert (await api.delete("/api/watchlist/telegram?value=Some_Channel", headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await api.get("/api/watchlist")).json() == {"telegram": [], "bluesky": []}  # nothing got through, and reads need no Origin
    r = await api.post("/api/watchlist/telegram", json=body, headers=hdr)
    assert r.status_code == 200 and r.json() == {"added": "Some_Channel"}
    assert (await api.delete("/api/watchlist/telegram?value=Some_Channel", headers={"Origin": OK})).status_code == 200  # DELETE has no body, so no content type
    assert (await api.get("/api/watchlist")).json()["telegram"] == []


async def test_guard_rejects_a_host_that_is_not_this_machine(api):  # DNS rebinding: the browser's Origin is the attacker's page, but the Host may be rebound
    r = await api.post("/api/watchlist/telegram", json={"value": "Some_Channel"}, headers={"Origin": OK, "Host": "evil.example:8000"})
    assert r.status_code == 403 and "machine" in r.json()["detail"]


# ---------- the job record ----------

def ev(source, eid, title="Backend Engineer", location="Bengaluru, India", company="acme", **kw):
    return NormalizedEvent(source_name=source, source_event_id=eid, source_url=f"https://x/{eid}", canonical_url=f"https://x/{source}/{eid}", event_type="job",
                           title=title, location=location, company_name=company, published_at=datetime.now(UTC), **kw)


async def route(s, name):
    return await registry.register_source(s, name, "api", retention_days=30)


async def records(s, where="true"):
    return (await s.execute(text(f"SELECT external_job_id, status, title, company_name, is_remote FROM job_postings WHERE {where} ORDER BY external_job_id"))).all()


async def test_every_job_event_gets_one_record_and_repeats_do_not_duplicate(sm):
    async with sm() as s:
        gh, hi = await route(s, "greenhouse:acme"), await route(s, "Himalayas")  # an ATS board and a remote board (no ats_boards row, so a NULL board)
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1"), ev("greenhouse:acme", "2")])
        await ingest_batch(s, hi, [ev("Himalayas", "1", location="Remote (worldwide)", company="Globex", raw_metadata={"remote": True})])
        await ingest_batch(s, hi, [ev("Himalayas", "1", location="Remote (worldwide)", company="Globex", raw_metadata={"remote": True})])  # same job, polled again
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1"), ev("greenhouse:acme", "2")])
        await s.commit()
    async with sm() as s:
        assert sorted(await records(s)) == [("1", "open", "Backend Engineer", "Globex", True), ("1", "open", "Backend Engineer", "acme", None),
                                            ("2", "open", "Backend Engineer", "acme", None)]
        a = await jobs.audit(s)
        assert a["job_events"] == 3 and a["records"] == 3 and a["events_without_record"] == 0 and a["expected_records"] == 3  # same id on two routes = two jobs


async def test_a_job_that_leaves_an_ats_board_is_closed_and_reopens_if_it_returns(sm):
    async with sm() as s:
        gh = await route(s, "greenhouse:acme")
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1"), ev("greenhouse:acme", "2")], full_listing=True)
        await s.commit()
    async with sm() as s:
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1")], full_listing=True)  # 2 is gone from the board
        await s.commit()
    async with sm() as s:
        assert [(r[0], r[1]) for r in await records(s)] == [("1", "open"), ("2", "closed")]
        assert (await s.execute(text("SELECT closed_at IS NOT NULL FROM job_postings WHERE external_job_id = '2'"))).scalar_one()
        await ingest_batch(s, gh, [], full_listing=True)  # an empty listing is an outage, not a board that closed everything
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1")])  # not a full listing: closes nothing
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1"), ev("greenhouse:acme", "2")], full_listing=True)
        await s.commit()
    async with sm() as s:
        assert [(r[0], r[1]) for r in await records(s)] == [("1", "open"), ("2", "open")]
        assert (await s.execute(text("SELECT closed_at FROM job_postings WHERE external_job_id = '2'"))).scalar_one() is None


async def test_ats_poll_through_run_once_closes_the_job_the_board_dropped(sm):
    listing = [{"id": 1, "title": "SRE", "absolute_url": "https://g/1", "location": {"name": "Pune"}}, {"id": 2, "title": "PM", "absolute_url": "https://g/2", "location": {"name": "Pune"}}]

    def handler(r):
        return httpx.Response(200, json={"jobs": listing})

    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with sm() as s:
        await route(s, "greenhouse:acme")
        await registry.set_enabled(s, "greenhouse:acme", "api", True)
        await s.commit()
    assert (await run_once(sm, AtsAdapter("greenhouse", "acme", c)))["new"] == 2
    del listing[1]
    await run_once(sm, AtsAdapter("greenhouse", "acme", c))
    async with sm() as s:
        assert [(r[0], r[1]) for r in await records(s)] == [("1", "open"), ("2", "closed")]


async def test_stale_jobs_close_by_age_and_the_record_outlives_the_event_text(sm):
    async with sm() as s:
        hi = await route(s, "Himalayas")
        await ingest_batch(s, hi, [ev("Himalayas", "old"), ev("Himalayas", "new")])
        await s.execute(text("UPDATE job_postings SET last_seen_at = now() - interval '31 days' WHERE external_job_id = 'old'"))
        assert (await jobs.maintain(s))["closed_stale"] == 1
        # retention scrubs the event (content, author, most raw_metadata; decision 75); the record is its own copy and keeps title, company and link
        await s.execute(text("UPDATE source_events SET retention_expires_at = now() - interval '1 day'"))
        await purge(s)
        await s.commit()
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM source_events WHERE raw_metadata ? 'scrubbed'"))).scalar_one() == 2
        rows = (await s.execute(text("SELECT external_job_id, status, title, company_name, url FROM job_postings ORDER BY 1"))).all()
        assert rows == [("new", "open", "Backend Engineer", "acme", "https://x/Himalayas/new"), ("old", "closed", "Backend Engineer", "acme", "https://x/Himalayas/old")]


async def test_company_is_taken_from_the_board_and_linked_later_for_remote_boards(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme Inc','acme inc') RETURNING id"))).scalar_one()
        await s.execute(text("INSERT INTO ats_boards (company_id, provider, board_token, status) VALUES (:c, 'greenhouse', 'acme', 'active')"), {"c": cid})
        gh, hi = await route(s, "greenhouse:acme"), await route(s, "Himalayas")
        await ingest_batch(s, gh, [ev("greenhouse:acme", "1")])
        await ingest_batch(s, hi, [ev("Himalayas", "9", company="Globex")])
        await s.commit()
    async with sm() as s:
        a, b = (await s.execute(text("SELECT company_id, company_name, ats_board_id IS NOT NULL FROM job_postings ORDER BY source_id = :h"), {"h": hi})).all()
        assert (a.company_id, a.company_name, a[2]) == (cid, "Acme Inc", True) and b.company_id is None and b.company_name == "Globex" and b[2] is False
        gid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Globex','globex') RETURNING id"))).scalar_one()
        await s.execute(text("UPDATE source_events SET company_id = :g WHERE source_event_id = '9'"), {"g": gid})
        assert (await jobs.maintain(s))["linked"] == 1
        assert (await s.execute(text("SELECT company_id FROM job_postings WHERE external_job_id = '9'"))).scalar_one() == gid


async def test_backfill_creates_a_record_per_event_and_matches_the_audit(sm):
    async with sm() as s:
        gh, hi = await route(s, "greenhouse:acme"), await route(s, "Himalayas")
        for sid, name, n in ((gh, "greenhouse:acme", 3), (hi, "Himalayas", 2)):  # events stored the old way: no records
            for i in range(n):
                await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, company_name, canonical_url, published_at, detected_at)
                    VALUES (:s, :i, 'job', 'Engineer', 'Pune', 'acme', :u, now(), now() - interval '2 days')"""), {"s": sid, "i": str(i), "u": f"https://x/{name}/{i}"})
        before = await jobs.audit(s)
        assert before["job_events"] == 5 and before["records"] == 0 and before["events_without_record"] == 5 and before["expected_records"] == 5
        assert await jobs.backfill(s) == 5
        after = await jobs.audit(s)
        assert after["records"] == after["expected_records"] == 5 and after["events_without_record"] == 0
        assert await jobs.backfill(s) == 0  # idempotent
        await s.commit()
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM job_postings WHERE source_event_id IS NOT NULL AND first_seen_at < now() - interval '1 day'"))).scalar_one() == 5
