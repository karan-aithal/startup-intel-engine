"""Jobs track J6: saved searches (scheduled scans): schedule maths, watermark, one digest per run, pause, crash safety, the write guard, the migration."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.api import scans as scans_api
from app.core.config import get_settings
from app.db.base import Base
from app.main import app
from app.services import explore, registry, scans
from app.services.pipeline import format_alert

OK = "http://127.0.0.1:3010"
HDR = {"Origin": OK, "Content-Type": "application/json"}
NOW = datetime.now(UTC)
H = timedelta(hours=1)


# ---------- pure ----------

def test_next_after_skips_missed_slots_and_waits_for_a_future_start():
    start, iv = datetime(2026, 1, 1, tzinfo=UTC), 6 * 3600
    assert scans.next_after(start, iv, start - H) == start
    assert scans.next_after(start, iv, start) == start + timedelta(hours=6)
    assert scans.next_after(start, iv, start + timedelta(hours=13)) == start + timedelta(hours=18)  # a long outage is not replayed


def test_window_widens_with_the_gap_and_stops_at_30():
    assert scans.window_days(7, NOW - H, NOW) == 7
    assert scans.window_days(1, NOW - timedelta(days=3, hours=1), NOW) == 5
    assert scans.window_days(7, NOW - timedelta(days=90), NOW) == 30


def test_interval_units():
    assert [scans.interval_s(2, u) for u in ("minutes", "hours", "days")] == [120, 7200, 172800]


def test_digest_text_lists_new_matches_and_escapes_html():
    items = [{"detected_at": NOW, "title": "A <b>", "company": "Acme", "location": "Pune", "source_url": "https://x/1", "fit": "strong"}]
    payload, n = scans.digest("Backend", {**scans.DEFAULT_FILTERS, "q": "backend"}, items, NOW - H, NOW)
    msg = format_alert("Job Scans", payload)
    assert n == 1 and "SCHEDULED SCAN" in msg and "A &lt;b&gt;" in msg and "Acme" in msg and "q=backend" in payload["url"] and "mode=all" in payload["url"]
    assert scans.digest("Backend", scans.DEFAULT_FILTERS, items, NOW + H, NOW) is None  # nothing detected after the watermark: no digest


# ---------- database ----------

async def job(s, eid, title, location="Bengaluru, India", detected=NOW - H, source="greenhouse:acme"):
    sid = await registry.register_source(s, source, "api")
    await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, company_name, raw_metadata, published_at, detected_at)
        VALUES (:s, :i, 'job', :t, :l, 'Acme', '{}'::jsonb, :d, :d)"""), {"s": sid, "i": eid, "t": title, "l": location, "d": detected})


async def make(sm, name="Backend", filters=None, **kw) -> str:
    async with sm() as s:
        sid = await scans.create(s, name, {**scans.DEFAULT_FILTERS, "q": "engineer", **(filters or {})}, kw.get("v", 6), kw.get("u", "hours"), None, "Asia/Kolkata")
        await s.commit()
    return sid


async def force(sm, sid, **cols):  # test setup: put the search at a chosen moment
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    async with sm() as s:
        await s.execute(text(f"UPDATE saved_searches SET {sets} WHERE id = CAST(:i AS uuid)"), {"i": sid, **cols})
        await s.commit()


async def alerts(sm):
    async with sm() as s:
        return [dict(r) for r in (await s.execute(text("SELECT dedup_key, topic, payload FROM alert_deliveries ORDER BY created_at"))).mappings()]


async def runs(sm):
    async with sm() as s:
        return [dict(r) for r in (await s.execute(text("SELECT run_no, state, total_matches, new_matches, error FROM saved_search_runs ORDER BY run_no"))).mappings()]


async def test_a_search_with_a_past_watermark_sends_one_digest_for_new_matches_only(sm):
    async with sm() as s:
        await job(s, "1", "Old Engineer", detected=NOW - 3 * H)
        await job(s, "2", "New Engineer", detected=NOW - H)
        await job(s, "3", "New Data Engineer", "Remote (worldwide)", detected=NOW - H)
        await job(s, "4", "Designer", detected=NOW - H)  # not a match
        await s.commit()
    sid = await make(sm)
    await force(sm, sid, watermark=NOW - 2 * H, next_run_at=NOW - H)
    assert await scans.run_due(sm) == 1
    a = await alerts(sm)
    assert len(a) == 1 and a[0]["topic"] == "Job Scans" and a[0]["dedup_key"] == f"scan:{sid}:1"
    assert sorted(j["title"] for j in a[0]["payload"]["jobs"]) == ["New Data Engineer", "New Engineer"]
    assert await runs(sm) == [{"run_no": 1, "state": "ok", "total_matches": 3, "new_matches": 2, "error": None}]  # total counts the old match too
    # a second run with no new jobs sends nothing
    await force(sm, sid, next_run_at=NOW - H)
    assert await scans.run_due(sm) == 1
    assert len(await alerts(sm)) == 1
    assert (await runs(sm))[1]["new_matches"] == 0


async def test_creation_watermark_means_no_flood_of_existing_jobs(sm):
    async with sm() as s:
        await job(s, "1", "Old Engineer", detected=NOW - H)
        await s.commit()
    sid = await make(sm)  # watermark = now
    await force(sm, sid, next_run_at=NOW - H)
    await scans.run_due(sm)
    assert await alerts(sm) == [] and (await runs(sm))[0]["total_matches"] == 1


async def test_the_scan_applies_the_explore_rule_so_a_restricted_remote_job_is_never_reported(sm):
    async with sm() as s:
        await job(s, "1", "Engineer", "Remote, US", detected=NOW - H)  # remote but restricted
        await job(s, "2", "Engineer", "London", detected=NOW - H)
        await s.commit()
    sid = await make(sm)
    await force(sm, sid, watermark=NOW - 2 * H, next_run_at=NOW - H)
    await scans.run_due(sm)
    assert await alerts(sm) == []


async def test_a_paused_search_does_not_run_and_resume_runs_it_with_its_old_watermark(sm):
    async with sm() as s:
        await job(s, "1", "Engineer", detected=NOW - H)
        await s.commit()
    sid = await make(sm)
    await force(sm, sid, watermark=NOW - 2 * H, next_run_at=NOW - H)
    async with sm() as s:
        await scans.set_status(s, sid, "paused")
        await s.commit()
    assert await scans.run_due(sm) == 0 and await alerts(sm) == []
    async with sm() as s:
        await scans.set_status(s, sid, "active")
        await s.commit()
    assert await scans.run_due(sm) == 1 and len(await alerts(sm)) == 1


async def test_a_worker_killed_mid_run_neither_loses_nor_duplicates_the_digest(sm):
    async with sm() as s:
        await job(s, "1", "Engineer", detected=NOW - H)
        await s.commit()
    sid = await make(sm)
    await force(sm, sid, watermark=NOW - 2 * H, next_run_at=NOW - H)
    async with sm() as s:
        rows = await scans.claim(s)
        await s.commit()
    assert len(rows) == 1
    async with sm() as s:  # the worker dies after doing the work but before the commit
        await scans.run_one(s, rows[0])
        await s.rollback()
    assert await alerts(sm) == [] and await runs(sm) == []  # nothing half-written
    assert await scans.run_due(sm) == 0  # the lease still holds the search
    await force(sm, sid, lease_expires_at=NOW - H)  # the lease expires
    assert await scans.run_due(sm) == 1
    assert [a["dedup_key"] for a in await alerts(sm)] == [f"scan:{sid}:1"]  # exactly one digest
    async with sm() as s:  # even a second finisher of the same run cannot queue it twice
        assert await scans.add_alert(s, f"scan:{sid}:1", {"label": "scan"}) is None


async def test_two_workers_do_not_claim_one_search(sm):
    sid = await make(sm)
    await force(sm, sid, next_run_at=NOW - H)
    async with sm() as a, sm() as b:
        first = await scans.claim(a)
        second = await scans.claim(b)
        await a.commit()
        await b.commit()
    assert len(first) == 1 and second == []


async def test_a_failing_run_is_recorded_keeps_the_watermark_and_is_retried_next_slot(sm, monkeypatch):
    sid = await make(sm)
    mark = NOW - 2 * H
    await force(sm, sid, watermark=mark, next_run_at=NOW - H)

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(explore, "search", boom)
    assert await scans.run_due(sm) == 1
    r = await runs(sm)
    assert r[0]["state"] == "error" and "db hiccup" in r[0]["error"]
    async with sm() as s:
        row = (await s.execute(text("SELECT watermark, next_run_at, lease_expires_at FROM saved_searches"))).one()
    assert abs((row.watermark - mark).total_seconds()) < 1 and row.next_run_at > datetime.now(UTC) and row.lease_expires_at is None


async def test_cap_minimum_interval_and_run_now_cooldown(sm):
    async with sm() as s:
        with pytest.raises(scans.ScanError, match="15 minutes"):
            await scans.create(s, "x", scans.DEFAULT_FILTERS, 10, "minutes", None, "Asia/Kolkata")
        with pytest.raises(scans.ScanError, match="timezone"):
            await scans.create(s, "x", scans.DEFAULT_FILTERS, 1, "hours", None, "Mars/Base")
    sid = await make(sm)
    async with sm() as s:
        await scans.run_now(s, sid)
        await s.commit()
    assert await scans.run_due(sm) == 1
    async with sm() as s:
        with pytest.raises(scans.ScanError) as e:
            await scans.run_now(s, sid)  # ran a moment ago
        assert e.value.status == 429
    for i in range(scans.MAX_SEARCHES - 1):
        await make(sm, name=f"s{i}")
    async with sm() as s:
        with pytest.raises(scans.ScanError) as e:
            await scans.create(s, "one too many", scans.DEFAULT_FILTERS, 1, "hours", None, "Asia/Kolkata")
        assert e.value.status == 409


async def test_overview_has_history_cards_and_the_collection_schedule(sm):
    async with sm() as s:
        await registry.register_source(s, "greenhouse:acme", "api")
        await s.execute(text("UPDATE source_registry SET enabled = true"))
        await s.commit()
    sid = await make(sm)
    await force(sm, sid, next_run_at=NOW - H)
    await scans.run_due(sm)
    async with sm() as s:
        d = await scans.overview(s)
    assert d["cards"]["active"] == 1 and d["cards"]["runs_24h"] == 1 and d["cards"]["ok_7d"] == 1
    assert d["searches"][0]["runs"] == 1 and d["searches"][0]["runs_ok"] == 1 and d["runs"][0]["state"] == "ok" and d["runs"][0]["seconds"] is not None
    assert [x["name"] for x in d["schedule"]] == ["greenhouse:acme"]


async def test_retention_prunes_old_runs(sm):
    from app.services.retention import PRUNE
    sid = await make(sm)
    async with sm() as s:
        await s.execute(text("INSERT INTO saved_search_runs (search_id, run_no, started_at, state) VALUES (CAST(:i AS uuid), 1, now() - interval '91 days', 'ok'), (CAST(:i AS uuid), 2, now(), 'ok')"), {"i": sid})
        await s.execute(text(PRUNE["saved_search_runs"]))
        await s.commit()
    assert [r["run_no"] for r in await runs(sm)] == [2]


# ---------- HTTP ----------

@pytest.fixture
def api(sm, monkeypatch):
    monkeypatch.setattr(scans_api, "get_sessionmaker", lambda: sm)
    assert OK in get_settings().cors_list
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")


async def test_every_write_route_has_the_guard_and_a_cross_origin_write_is_rejected(api, sm):
    from app.api.guard import local_write
    for x in scans_api.router.routes:
        if "GET" not in x.methods:
            assert local_write in [d.call for d in x.dependant.dependencies], x.path
    body = {"name": "Backend", "filters": {"q": "engineer"}}
    assert (await api.post("/api/scans", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await api.post("/api/scans", json=body)).status_code == 403
    assert (await api.post("/api/scans", content="{}", headers={"Origin": OK, "Content-Type": "text/plain"})).status_code == 415
    assert (await api.get("/api/scans")).json()["searches"] == []  # nothing got through


async def test_create_pause_resume_run_delete_over_http(api, sm):
    r = await api.post("/api/scans", json={"name": "Backend", "filters": {"q": "engineer", "days": 3}, "interval_value": 1, "interval_unit": "days"}, headers=HDR)
    assert r.status_code == 200
    sid = r.json()["id"]
    got = (await api.get("/api/scans")).json()["searches"][0]
    assert got["filters"]["days"] == 3 and got["filters"]["mode"] == "all" and got["timezone"] == "Asia/Kolkata" and got["status"] == "active"
    assert (await api.post(f"/api/scans/{sid}/pause", json={}, headers=HDR)).json() == {"status": "paused"}
    assert (await api.post(f"/api/scans/{sid}/run", json={}, headers=HDR)).status_code == 409  # paused
    assert (await api.post(f"/api/scans/{sid}/resume", json={}, headers=HDR)).json() == {"status": "active"}
    assert (await api.post(f"/api/scans/{sid}/run", json={}, headers=HDR)).status_code == 200
    assert (await api.post("/api/scans/not-a-uuid/pause", json={}, headers=HDR)).status_code == 404
    assert (await api.post("/api/scans", json={"name": "x", "filters": {"days": 99}}, headers=HDR)).status_code == 422
    assert (await api.delete(f"/api/scans/{sid}", headers={"Origin": OK})).status_code == 200
    assert (await api.get("/api/scans")).json()["searches"] == []


# ---------- migration ----------

async def test_migration_0014_matches_the_orm_models(engine):
    """Runs 0014's upgrade() in an empty scratch schema (the engine's search_path puts it first, and the two tables need nothing else) and diffs the
    result against the models: a column, constraint or index that drifts shows up as a difference."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    path = Path(__file__).parents[1] / "alembic/versions/0014_saved_searches.py"
    spec = spec_from_file_location("m0014", path)
    m = module_from_spec(spec)
    spec.loader.exec_module(m)
    names = {"saved_searches", "saved_search_runs"}
    async with engine.begin() as c:
        await c.execute(text("DROP TABLE IF EXISTS saved_search_runs, saved_searches CASCADE"))  # the ORM fixture created them; the migration must stand alone

        def go(sync):
            ctx = MigrationContext.configure(sync, opts={"include_object": lambda o, n, t, r, cn: (n in names if t == "table" else o.table.name in names)})
            with Operations.context(ctx):
                m.upgrade()
            return compare_metadata(ctx, Base.metadata)

        diff = await c.run_sync(go)
        await c.execute(text("DROP TABLE saved_search_runs, saved_searches CASCADE"))
    assert diff == []
