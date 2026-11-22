"""Jobs track J7: the Pipeline (tracked jobs): save and skip from Explore, the Inbox, status history, bulk moves, survival after retention, the import of
feedback marks (dry run equals the real run), tab counts equal rows, the write guard, the migration."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.api import tracked as api_mod
from app.core.config import get_settings
from app.db.base import Base
from app.main import app
from app.services import explore, registry, tracked

OK = "http://127.0.0.1:3010"
HDR = {"Origin": OK, "Content-Type": "application/json"}
NOW = datetime.now(UTC)
H = timedelta(hours=1)


async def job(s, eid, title="Backend Engineer", location="Bengaluru, India", source="greenhouse:acme", company="Acme"):
    sid = await registry.register_source(s, source, "api")
    await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, company_name, source_url, raw_metadata, published_at, detected_at)
        VALUES (:s, :i, 'job', :t, :l, :c, :u, '{}'::jsonb, :d, :d)"""), {"s": sid, "i": eid, "t": title, "l": location, "c": company, "u": f"https://jobs.example/{eid}", "d": NOW - H})


async def seed(sm, n=3) -> list[dict]:
    async with sm() as s:
        for i in range(1, n + 1):
            await job(s, str(i), f"Engineer {i}")
        await s.commit()
    async with sm() as s:
        return sorted((await explore.search(s))["items"], key=lambda j: j["title"])  # same timestamp: Explore order is arbitrary


async def tid_of(sm, title):
    async with sm() as s:
        return str((await s.execute(text("SELECT id FROM tracked_jobs WHERE title = :t"), {"t": title})).scalar_one())


async def do(sm, fn, *args):
    async with sm() as s:
        out = await fn(s, *args)
        await s.commit()
    return out


async def history(sm, tid):
    async with sm() as s:
        return [tuple(r) for r in (await s.execute(text("SELECT from_status, to_status, note FROM tracked_job_events WHERE tracked_job_id = CAST(:i AS uuid) ORDER BY at"), {"i": tid}))]


# ---------- Explore and the Inbox ----------

async def test_saving_and_skipping_from_explore_take_a_job_out_of_the_inbox(sm):
    items = await seed(sm)
    ids = [str(j["id"]) for j in items]
    assert all(j["tracked"] is None for j in items)
    assert await do(sm, tracked.track, ids[:1], "saved") == {"tracked": 1, "already": 0, "missing": 0}
    assert await do(sm, tracked.track, ids[1:2], "skipped") == {"tracked": 1, "already": 0, "missing": 0}
    async with sm() as s:
        shown = {j["title"]: j["tracked"] for j in (await explore.search(s))["items"]}
        inbox = await explore.search(s, hide_tracked=True)
        ov = await tracked.overview(s, "inbox")
    assert sorted(shown.values(), key=str) == [None, "saved", "skipped"]
    assert [j["title"] for j in inbox["items"]] == ["Engineer 3"] and inbox["total"] == 1 and inbox["counts"]["all"] == 1  # hidden before the counts, not after
    assert ov["counts"]["inbox"] == 1 and ov["counts"]["saved"] == 1 and ov["counts"]["skipped"] == 1 and ov["total"] == 1


async def test_tracking_twice_keeps_the_first_status_and_a_bad_request_changes_nothing(sm):
    items = await seed(sm, 1)
    eid = str(items[0]["id"])
    await do(sm, tracked.track, [eid], "saved")
    assert await do(sm, tracked.track, [eid], "skipped") == {"tracked": 0, "already": 1, "missing": 0}
    assert (await do(sm, tracked.track, ["00000000-0000-0000-0000-000000000000"], "saved"))["missing"] == 1
    for bad, status in ((["nope"], "saved"), ([], "saved"), ([eid], "applied")):
        with pytest.raises(tracked.TrackError):
            await do(sm, tracked.track, bad, status)
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM tracked_jobs"))).scalar_one() == 1


async def test_the_server_copies_title_company_and_link_from_the_event(sm):
    items = await seed(sm, 1)
    await do(sm, tracked.track, [str(items[0]["id"])], "saved")
    async with sm() as s:
        r = (await s.execute(text("SELECT title, company_name, url, location, status FROM tracked_jobs"))).one()
    assert tuple(r) == ("Engineer 1", "Acme", "https://jobs.example/1", "Bengaluru, India", "saved")


# ---------- states, history, bulk ----------

async def test_moving_a_job_records_history_and_applied_sets_its_date_once(sm):
    items = await seed(sm, 1)
    await do(sm, tracked.track, [str(items[0]["id"])], "saved")
    tid = await tid_of(sm, "Engineer 1")
    assert (await do(sm, tracked.set_status, [tid], "applied", "via the careers page"))["changed"] == 1
    async with sm() as s:
        first = (await s.execute(text("SELECT applied_at FROM tracked_jobs"))).scalar_one()
    assert first is not None
    await do(sm, tracked.set_status, [tid], "interview")
    await do(sm, tracked.set_status, [tid], "applied")  # back again: the date does not move
    async with sm() as s:
        assert (await s.execute(text("SELECT applied_at FROM tracked_jobs"))).scalar_one() == first
    assert await history(sm, tid) == [(None, "saved", None), ("saved", "applied", "via the careers page"), ("applied", "interview", None), ("interview", "applied", None)]
    assert (await do(sm, tracked.set_status, [tid], "applied"))["unchanged"] == 1  # no history row for a no-op
    assert len(await history(sm, tid)) == 4
    with pytest.raises(tracked.TrackError):
        await do(sm, tracked.set_status, [tid], "evaluated")


async def test_bulk_status_change_and_tab_counts_equal_rows(sm):
    items = await seed(sm, 4)
    await do(sm, tracked.track, [str(j["id"]) for j in items], "saved")
    async with sm() as s:
        ids = [str(r) for r in (await s.execute(text("SELECT id FROM tracked_jobs ORDER BY title"))).scalars()]
    assert await do(sm, tracked.set_status, ids[:3], "applied") == {"changed": 3, "unchanged": 0, "missing": 0}
    await do(sm, tracked.set_status, ids[:1], "offer")
    async with sm() as s:
        for tab in tracked.STATES:
            ov = await tracked.overview(s, tab)
            assert ov["counts"][tab] == ov["total"] == len(ov["items"]), tab  # the badge, the total and the rows agree
        ov = await tracked.overview(s, "saved")
        assert ov["counts"] == {"inbox": 0, "saved": 1, "applied": 2, "responded": 0, "assessment": 0, "interview": 0, "offer": 1, "hired": 0, "rejected": 0, "withdrawn": 0, "skipped": 0}
        funnel = {f["stage"]: f["reached"] for f in ov["funnel"]}
    assert funnel["saved"] == 4 and funnel["applied"] == 3 and funnel["offer"] == 1 and funnel["hired"] == 0


async def test_search_sort_and_paging_on_a_state_tab(sm):
    items = await seed(sm, 3)
    await do(sm, tracked.track, [str(j["id"]) for j in items], "saved")
    async with sm() as s:
        assert [j["title"] for j in (await tracked.overview(s, "saved", q="engineer 2"))["items"]] == ["Engineer 2"]
        assert (await tracked.overview(s, "saved", q="nothing"))["total"] == 0
        assert [j["title"] for j in (await tracked.overview(s, "saved", sort="title", limit=2))["items"]] == ["Engineer 1", "Engineer 2"]
        assert [j["title"] for j in (await tracked.overview(s, "saved", sort="title", limit=2, offset=2))["items"]] == ["Engineer 3"]
        assert (await tracked.overview(s, "saved", q="; DROP TABLE tracked_jobs; --"))["total"] == 0  # q is a bound parameter
        with pytest.raises(tracked.TrackError):
            await tracked.overview(s, "evaluated")


async def test_edit_fields_clear_one_and_restore_to_inbox(sm):
    items = await seed(sm, 1)
    await do(sm, tracked.track, [str(items[0]["id"])], "saved")
    tid = await tid_of(sm, "Engineer 1")
    await do(sm, tracked.edit, tid, {"notes": " call Priya ", "salary_text": "18-24 LPA"}, None, False)
    await do(sm, tracked.edit, tid, {"salary_text": ""}, None, False)
    async with sm() as s:
        d = await tracked.detail(s, tid)
    assert d["job"]["notes"] == "call Priya" and d["job"]["salary_text"] is None
    with pytest.raises(tracked.TrackError):
        await do(sm, tracked.edit, tid, {"notes": "x" * 5001}, None, False)
    with pytest.raises(tracked.TrackError):
        await do(sm, tracked.edit, tid, {"status": "hired"}, None, False)  # status moves go through set_status, which writes history
    await do(sm, tracked.restore, tid)
    async with sm() as s:
        assert (await explore.search(s, hide_tracked=True))["total"] == 1  # an Explore result again
    with pytest.raises(tracked.TrackError):
        await do(sm, tracked.restore, tid)


# ---------- survival and context ----------

async def test_a_tracked_job_still_reads_after_its_event_is_gone_and_warns_when_its_posting_closed(sm):
    items = await seed(sm, 1)
    async with sm() as s:
        sid, ext = (await s.execute(text("SELECT source_id, source_event_id FROM source_events"))).one()
        await s.execute(text("INSERT INTO job_postings (source_id, external_job_id, title, status) VALUES (:s, :e, 'Engineer 1', 'open')"), {"s": sid, "e": ext})
        await s.commit()
    await do(sm, tracked.track, [str(items[0]["id"])], "saved")
    tid = await tid_of(sm, "Engineer 1")
    async with sm() as s:
        assert (await tracked.detail(s, tid))["closed"] is False
        await s.execute(text("UPDATE job_postings SET closed_at = now(), status = 'closed'"))
        await s.execute(text("DELETE FROM source_events"))  # retention scrubbed it away entirely: the worst case
        await s.commit()
    async with sm() as s:
        d = await tracked.detail(s, tid)
        row = (await tracked.overview(s, "saved"))["items"][0]
    assert d["closed"] is True and d["job"]["title"] == "Engineer 1" and d["job"]["company_name"] == "Acme" and d["job"]["url"] == "https://jobs.example/1"
    assert row["title"] == "Engineer 1" and row["open"] is False


async def test_detail_carries_company_and_funding_context(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme', 'acme') RETURNING id"))).scalar_one()
        await job(s, "1")
        await s.execute(text("UPDATE source_events SET company_id = :c"), {"c": cid})
        await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, amount_usd, verification_status, announced_at) VALUES (:c, 'Series A', 5000000, 'VERIFIED', now())"""), {"c": cid})
        await s.commit()
    async with sm() as s:
        eid = str((await explore.search(s))["items"][0]["id"])
    await do(sm, tracked.track, [eid], "saved")
    async with sm() as s:
        d = await tracked.detail(s, await tid_of(sm, "Backend Engineer"))
    assert d["company"]["name"] == "Acme" and d["funding"][0]["stage"] == "Series A" and d["closed"] is None  # no J1 record: liveness unknown


# ---------- the import of feedback marks ----------

async def test_import_dry_run_count_equals_the_real_run_and_a_second_run_adds_nothing(sm):
    items = await seed(sm, 4)
    ev = [str(j["id"]) for j in items]
    async with sm() as s:
        await s.execute(text("INSERT INTO feedback (action, source_event_id, created_at) VALUES ('save', :e, now() - interval '3 days')"), {"e": ev[0]})
        await s.execute(text("INSERT INTO feedback (action, source_event_id, created_at) VALUES ('save', :e, now() - interval '3 days')"), {"e": ev[1]})
        await s.execute(text("INSERT INTO feedback (action, source_event_id, created_at) VALUES ('applied', :e, now() - interval '2 days')"), {"e": ev[1]})  # both marks: applied wins
        await s.execute(text("INSERT INTO feedback (action, source_event_id) VALUES ('applied', :e)"), {"e": ev[2]})
        await s.execute(text("INSERT INTO feedback (action) VALUES ('save')"))  # names nothing: reported, not guessed
        await s.execute(text("INSERT INTO feedback (action, source_event_id) VALUES ('useful', :e)"), {"e": ev[3]})  # not a pipeline mark
        await s.commit()
    async with sm() as s:
        dry = await tracked.import_marks(s)
        assert (await s.execute(text("SELECT count(*) FROM tracked_jobs"))).scalar_one() == 0  # a dry run writes nothing
    assert dry == {"marks": 5, "unresolved": 1, "jobs": 3, "already_tracked": 0, "to_import": 3, "saved": 1, "applied": 2, "dry_run": True}
    async with sm() as s:
        real = await tracked.import_marks(s, dry_run=False)
        await s.commit()
        n = (await s.execute(text("SELECT count(*) FROM tracked_jobs"))).scalar_one()
    assert real["imported"] == n == dry["to_import"]
    async with sm() as s:
        again = await tracked.import_marks(s, dry_run=False)
        await s.commit()
        by = dict((await s.execute(text("SELECT title, status FROM tracked_jobs"))).all())
        ap = (await s.execute(text("SELECT applied_at FROM tracked_jobs WHERE title = 'Engineer 2'"))).scalar_one()
    assert again["to_import"] == 0 and again["imported"] == 0 and again["already_tracked"] == 3
    assert by == {"Engineer 1": "saved", "Engineer 2": "applied", "Engineer 3": "applied"}
    assert NOW - ap > timedelta(days=1, hours=23)  # dated by the applied mark, not by the import


async def test_import_resolves_a_mark_that_names_only_the_job_record(sm):
    await seed(sm, 1)
    async with sm() as s:
        sid = (await s.execute(text("SELECT source_id FROM source_events"))).scalar_one()
        jp = (await s.execute(text("INSERT INTO job_postings (source_id, external_job_id, title, url, company_name) VALUES (:s, :e, 'Record Job', 'https://x/r', 'Beta') RETURNING id"), {"s": sid, "e": "rec-1"})).scalar_one()
        await s.execute(text("INSERT INTO feedback (action, job_posting_id) VALUES ('save', :j)"), {"j": jp})
        await s.commit()
    out = await do(sm, tracked.import_marks, False)
    assert out["imported"] == 1 and out["unresolved"] == 0
    async with sm() as s:
        assert tuple((await s.execute(text("SELECT title, company_name, url, status FROM tracked_jobs"))).one()) == ("Record Job", "Beta", "https://x/r", "saved")


# ---------- HTTP and the guard ----------

@pytest.fixture
def api(sm, monkeypatch):
    monkeypatch.setattr(api_mod, "get_sessionmaker", lambda: sm)
    assert OK in get_settings().cors_list
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")


async def test_every_write_route_has_the_guard_and_cross_origin_writes_are_rejected(api, sm):
    from app.api.guard import local_write
    for x in api_mod.router.routes:
        if "GET" not in x.methods:
            assert local_write in [d.call for d in x.dependant.dependencies], x.path
    items = await seed(sm, 1)
    body = {"event_ids": [str(items[0]["id"])], "status": "saved"}
    assert (await api.post("/api/pipeline/track", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await api.post("/api/pipeline/track", json=body)).status_code == 403
    assert (await api.post("/api/pipeline/track", content="{}", headers={"Origin": OK, "Content-Type": "text/plain"})).status_code == 415
    assert (await api.get("/api/pipeline")).json()["counts"]["saved"] == 0  # nothing got through


async def test_save_move_edit_and_restore_over_http(api, sm):
    items = await seed(sm, 2)
    r = await api.post("/api/pipeline/track", json={"event_ids": [str(j["id"]) for j in items], "status": "saved"}, headers=HDR)
    assert r.status_code == 200 and r.json()["tracked"] == 2
    page = (await api.get("/api/pipeline", params={"tab": "saved"})).json()
    assert page["total"] == 2 and page["counts"]["inbox"] == 0
    tid = page["items"][0]["id"]
    assert (await api.patch(f"/api/pipeline/{tid}", json={"status": "interview", "note": "phone screen", "next_step": "Thursday", "notes": "ask about team"}, headers=HDR)).status_code == 200
    d = (await api.get(f"/api/pipeline/{tid}")).json()
    assert d["job"]["status"] == "interview" and d["job"]["next_step"] == "Thursday" and d["history"][-1]["note"] == "phone screen"
    assert (await api.post("/api/pipeline/bulk", json={"ids": [page["items"][1]["id"]], "status": "rejected"}, headers=HDR)).json()["changed"] == 1
    assert (await api.post("/api/pipeline/bulk", json={"ids": [tid], "status": "evaluated"}, headers=HDR)).status_code == 422
    assert (await api.post("/api/pipeline/track", json={"event_ids": ["nope"], "status": "saved"}, headers=HDR)).status_code == 422
    assert (await api.get("/api/pipeline/not-a-uuid")).status_code == 422
    assert (await api.patch("/api/pipeline/00000000-0000-0000-0000-000000000000", json={"notes": "x"}, headers=HDR)).status_code == 404
    assert (await api.patch(f"/api/pipeline/{tid}", json={"status": "bogus"}, headers=HDR)).status_code == 422
    assert (await api.delete(f"/api/pipeline/{tid}", headers={"Origin": OK})).status_code == 200
    assert (await api.get("/api/pipeline", params={"tab": "inbox"})).json()["counts"]["inbox"] == 1


# ---------- migration ----------

async def test_migration_0015_matches_the_orm_models(engine):
    """Replays the real chain (0015, then 0016 which adds J8's columns and table) in the scratch schema and diffs the result against the models.
    The tables the migrations made are left in place, like the ORM ones, so later tests find a full schema."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    def load(name, file):
        spec = spec_from_file_location(name, Path(__file__).parents[1] / "alembic/versions" / file)
        m = module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    chain = [load("m0015", "0015_tracked_jobs.py"), load("m0016", "0016_followups.py")]
    names = {"tracked_jobs", "tracked_job_events", "followup_log"}
    async with engine.begin() as c:
        await c.execute(text("DROP TABLE IF EXISTS followup_log, tracked_job_events, tracked_jobs CASCADE"))

        def go(sync):
            ctx = MigrationContext.configure(sync, opts={"include_object": lambda o, n, t, r, cn: (n in names if t == "table" else o.table.name in names)})
            with Operations.context(ctx):
                for m in chain:
                    m.upgrade()
            return compare_metadata(ctx, Base.metadata)

        assert await c.run_sync(go) == []
