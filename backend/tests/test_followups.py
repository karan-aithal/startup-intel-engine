"""Jobs track J8: follow-ups. The pure verdict (cadence table, pins, stop, per-status counts), the reminder window, one reminder per due episode
(restart-safe), logging and pinning move the date, the off switch, the write guard and the migration."""
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.api import followups as api_mod
from app.core.config import get_settings
from app.db.base import Base
from app.main import app
from app.services import followups as fu
from app.services import pipeline, registry

OK = "http://127.0.0.1:3010"
HDR = {"Origin": OK, "Content-Type": "application/json"}
NOW = datetime(2026, 11, 22, 5, 30, tzinfo=UTC)  # 11:00 on 22 Nov in Asia/Kolkata: inside the reminder window
TODAY = date(2026, 11, 22)
C = fu.load_cfg()


def d(n: int) -> date:
    return TODAY - timedelta(days=n)


def v(status, since_ago, logs_ago=(), pin=None, stopped=False):
    return fu.verdict(status, d(since_ago), [d(n) for n in logs_ago], pin, stopped, TODAY, C)


# ---------- the pure verdict ----------

def test_applied_cadence():
    assert v("applied", 6)["urgency"] == "waiting" and v("applied", 6)["next_on"] == d(-1)
    r = v("applied", 8)
    assert (r["urgency"], r["next_on"], r["count"]) == ("overdue", d(1), 0)
    assert v("applied", 7)["urgency"] == "overdue"  # due on the day
    assert v("applied", 20, [3])["urgency"] == "waiting" and v("applied", 20, [3])["next_on"] == d(-4)  # 7 days after the last one
    assert v("applied", 20, [7])["urgency"] == "overdue"
    r = v("applied", 30, [20, 10])  # two follow-ups made: cold, nothing more to do
    assert (r["urgency"], r["next_on"]) == ("cold", None)


def test_responded_and_assessment_count_from_the_status_start():
    assert v("responded", 0)["urgency"] == "waiting"  # replied today, first follow-up is due tomorrow
    assert v("responded", 1)["urgency"] == "urgent" and v("responded", 2)["urgency"] == "urgent"
    assert v("responded", 3)["urgency"] == "overdue"
    assert v("assessment", 5, [1])["urgency"] == "waiting" and v("assessment", 5, [3])["urgency"] == "overdue"  # then every 3 days, no limit
    assert v("responded", 9, [8, 5, 2, 1])["urgency"] == "waiting"  # many follow-ups never go cold outside applied


def test_interview_thank_you_then_every_three_days():
    assert v("interview", 0)["urgency"] == "waiting" and v("interview", 1)["urgency"] == "overdue"
    assert v("interview", 4, [1])["urgency"] == "waiting" and v("interview", 6, [3])["urgency"] == "overdue"


def test_pin_overrides_the_cadence_and_revives_a_cold_job():
    assert v("applied", 8, pin=d(-3))["urgency"] == "waiting"  # overdue by the cadence, deferred by the pin
    r = v("applied", 30, [20, 10], pin=d(1))  # cold, pinned to yesterday: due again
    assert (r["urgency"], r["next_on"]) == ("overdue", d(1))
    assert v("responded", 1, pin=d(0))["urgency"] == "overdue"  # a pin is never "urgent"


def test_stopped_outranks_everything_and_other_statuses_have_no_verdict():
    r = v("applied", 30, stopped=True, pin=d(1))
    assert (r["urgency"], r["next_on"]) == ("stopped", None)
    for st in ("saved", "offer", "hired", "rejected", "withdrawn", "skipped"):
        assert v(st, 10) is None


def test_reminder_window_and_quiet_hours():
    def at(h):
        return fu.window_open(datetime(2026, 11, 22, h, 0), C)
    assert [h for h in range(24) if at(h)] == list(range(10, 21))  # 10:00 up to 20:59; 21:00 to 09:59 waits
    plain = {**C, "reminders": {**C["reminders"], "hour": 6, "quiet_from": 1, "quiet_until": 5}}  # quiet hours that do not cross midnight
    assert [h for h in range(24) if fu.window_open(datetime(2026, 11, 22, h), plain)] == list(range(6, 24))


def test_config_file_is_valid_and_bad_values_are_named(tmp_path):
    assert C["enabled"] and C["cadence"]["applied_first_days"] == 7 and C["channels"] == ["Email", "LinkedIn", "Phone", "Other"]
    f = tmp_path / "f.yaml"
    f.write_text(fu.CONFIG.read_text().replace("applied_first_days: 7", "applied_first_days: soon"))
    with pytest.raises(ValueError, match="applied_first_days"):
        fu.load_cfg(f)
    f.write_text(fu.CONFIG.read_text().replace("hour: 10 ", "hour: 25 "))
    with pytest.raises(ValueError, match="reminders.hour"):
        fu.load_cfg(f)


# ---------- reminders: one per due episode ----------

async def job(sm, title="Backend Engineer", status="applied", applied_ago=8, changed_ago=None, source="greenhouse:acme"):
    async with sm() as s:
        sid = await registry.register_source(s, source, "api")
        await s.execute(text("""INSERT INTO tracked_jobs (source_id, external_job_id, title, company_name, url, status, applied_at, status_changed_at)
            VALUES (:s, :e, :t, 'Acme', 'https://jobs.example/1', :st, :ap, :ch)"""),
                        {"s": sid, "e": title, "t": title, "st": status, "ap": NOW - timedelta(days=applied_ago) if applied_ago is not None else None,
                         "ch": NOW - timedelta(days=applied_ago if changed_ago is None else changed_ago)})
        await s.commit()
        return str((await s.execute(text("SELECT id FROM tracked_jobs WHERE title = :t"), {"t": title})).scalar_one())


async def alerts(sm):
    async with sm() as s:
        return [tuple(r) for r in await s.execute(text("SELECT dedup_key, topic, payload->>'label' FROM alert_deliveries ORDER BY dedup_key"))]


async def entry(sm, tid, cfg=C, now=NOW):
    async with sm() as s:
        return next(e for e in await fu.entries(s, cfg, now) if e["id"] == tid)


async def test_applied_8_days_ago_is_overdue_and_gets_exactly_one_reminder_even_after_restarts(sm):
    tid = await job(sm)
    e = await entry(sm, tid)
    assert e["urgency"] == "overdue" and e["next_on"] == d(1)
    assert await fu.remind_due(sm, C, NOW) == 1
    assert await alerts(sm) == [(f"followup:{tid}:{d(1)}", "Follow-ups", "followup")]
    # a worker restart is a fresh call; later passes the same day and the next day add nothing for the same due date
    assert await fu.remind_due(sm, C, NOW) == 0
    assert await fu.remind_due(sm, C, NOW + timedelta(hours=3)) == 0
    assert await fu.remind_due(sm, C, NOW + timedelta(days=1)) == 0
    assert len(await alerts(sm)) == 1


async def test_a_job_that_is_not_due_and_a_saved_job_get_none(sm):
    await job(sm, "Fresh", applied_ago=2)
    await job(sm, "Saved", status="saved", applied_ago=None, changed_ago=30)
    assert await fu.remind_due(sm, C, NOW) == 0 and await alerts(sm) == []


async def test_quiet_hours_hold_the_reminder_until_the_window_opens(sm):
    await job(sm)
    assert await fu.remind_due(sm, C, datetime(2026, 11, 22, 17, 0, tzinfo=UTC)) == 0  # 22:30 IST
    assert await fu.remind_due(sm, C, datetime(2026, 11, 22, 3, 0, tzinfo=UTC)) == 0  # 08:30 IST
    assert await alerts(sm) == []
    assert await fu.remind_due(sm, C, datetime(2026, 11, 22, 4, 30, tzinfo=UTC)) == 1  # 10:00 IST: not lost, queued now


async def test_logging_a_follow_up_moves_the_next_date_and_starts_a_new_episode(sm):
    tid = await job(sm)
    assert await fu.remind_due(sm, C, NOW) == 1
    async with sm() as s:
        await fu.log(s, tid, "Email", "Priya (recruiter)", "asked for status", None, None, C, NOW)
        await s.commit()
    e = await entry(sm, tid)
    assert (e["urgency"], e["next_on"], e["count"], e["last_on"]) == ("waiting", TODAY + timedelta(days=7), 1, TODAY)
    assert await fu.remind_due(sm, C, NOW) == 0
    later = NOW + timedelta(days=7)  # the next due date: a new episode, a new reminder
    assert await fu.remind_due(sm, C, later) == 1
    assert [k for k, *_ in await alerts(sm)] == sorted([f"followup:{tid}:{d(1)}", f"followup:{tid}:{TODAY + timedelta(days=7)}"])
    async with sm() as s:  # a second follow-up makes it cold: no further reminder, ever
        await fu.log(s, tid, "Phone", None, None, TODAY + timedelta(days=7), None, C, later)
        await s.commit()
    assert (await entry(sm, tid, now=later))["urgency"] == "cold"
    assert await fu.remind_due(sm, C, later + timedelta(days=30)) == 0


async def test_pinning_overrides_the_cadence_and_a_log_afterwards_drops_the_pin(sm):
    tid = await job(sm)
    async with sm() as s:
        await fu.pin(s, tid, TODAY + timedelta(days=3))
        await s.commit()
    e = await entry(sm, tid)
    assert (e["urgency"], e["next_on"], e["pinned"]) == ("waiting", TODAY + timedelta(days=3), True)
    assert await fu.remind_due(sm, C, NOW) == 0  # deferred
    assert await fu.remind_due(sm, C, NOW + timedelta(days=3)) == 1
    async with sm() as s:  # log, then pin in the same step: the pin stands; a plain log afterwards clears it
        await fu.log(s, tid, "Email", None, None, None, TODAY + timedelta(days=2), C, NOW)
        await s.commit()
    assert (await entry(sm, tid))["next_on"] == TODAY + timedelta(days=2)
    async with sm() as s:
        await fu.log(s, tid, "Email", None, None, None, None, C, NOW)
        await s.commit()
    e = await entry(sm, tid)
    assert (e["pinned"], e["next_on"]) == (False, None)  # two applied follow-ups: cold again


async def test_stop_silences_a_job_and_a_logged_follow_up_lifts_it(sm):
    tid = await job(sm)
    async with sm() as s:
        await fu.stop(s, tid, True)
        await s.commit()
    assert (await entry(sm, tid))["urgency"] == "stopped" and await fu.remind_due(sm, C, NOW) == 0
    async with sm() as s:
        await fu.log(s, tid, "Other", None, None, None, None, C, NOW)
        await s.commit()
    assert (await entry(sm, tid))["urgency"] == "waiting"


async def test_responded_clock_starts_when_the_status_began_not_at_the_application(sm):
    tid = await job(sm, "Replied", status="responded", applied_ago=10, changed_ago=0)
    e = await entry(sm, tid)
    assert (e["urgency"], e["days_since"]) == ("waiting", 0)  # career-ops would say overdue here
    assert await fu.remind_due(sm, C, NOW) == 0
    assert await fu.remind_due(sm, C, NOW + timedelta(days=1)) == 1


async def test_follow_ups_are_counted_per_status(sm):
    tid = await job(sm, "Moved", applied_ago=20)
    async with sm() as s:
        await fu.log(s, tid, "Email", None, None, d(15), None, C, NOW)
        await fu.log(s, tid, "Email", None, None, d(9), None, C, NOW)
        await s.execute(text("UPDATE tracked_jobs SET status = 'responded', status_changed_at = :n WHERE id = CAST(:i AS uuid)"), {"n": NOW - timedelta(days=2), "i": tid})
        await s.commit()
    e = await entry(sm, tid)
    assert e["count"] == 0 and e["urgency"] == "urgent" and len(e["log"]) == 2  # the two applied-phase logs do not count for the responded phase


async def test_turning_the_feature_off_hides_the_page_data_and_stops_reminders(sm):
    await job(sm)
    off = {**C, "enabled": False}
    assert await fu.remind_due(sm, off, NOW) == 0 and await alerts(sm) == []
    async with sm() as s:
        assert await fu.overview(s, off, NOW) == {"enabled": False}
    assert await fu.remind_due(sm, C, NOW) == 1  # on again: the reminder that was due is queued


async def test_overview_tiles_equal_rows(sm):
    await job(sm, "A", applied_ago=8)
    await job(sm, "B", applied_ago=2)
    await job(sm, "C", status="responded", applied_ago=5, changed_ago=2)
    async with sm() as s:
        o = await fu.overview(s, C, NOW)
    assert o["tiles"] == {"urgent": 1, "overdue": 1, "waiting": 1, "cold": 0, "stopped": 0} and len(o["items"]) == 3
    assert [e["title"] for e in o["items"]] == ["C", "A", "B"]  # most urgent first


def test_reminder_text_formats():
    p = fu.reminder({"title": "SRE", "company_name": "Acme", "status": "applied", "urgency": "overdue", "next_on": d(1), "days_since": 8, "count": 0,
                     "contact": "Priya <p@x.example>", "url": "https://jobs.example/1"}, NOW)
    out = pipeline.format_alert("Follow-ups", p)
    assert "FOLLOW-UP DUE" in out and "SRE" in out and "Acme" in out and "8 days in applied" in out and "&lt;p@x.example&gt;" in out


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
            assert local_write in [dep.call for dep in x.dependant.dependencies], x.path
    tid = await job(sm)
    body = {"channel": "Email"}
    assert (await api.post(f"/api/followups/{tid}/log", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await api.post(f"/api/followups/{tid}/log", json=body)).status_code == 403
    assert (await api.put(f"/api/followups/{tid}/stop", json={"stopped": True})).status_code == 403
    assert (await api.post(f"/api/followups/{tid}/log", content="{}", headers={"Origin": OK, "Content-Type": "text/plain"})).status_code == 415
    assert (await api.get("/api/followups")).json()["items"][0]["count"] == 0  # nothing got through


async def test_log_pin_stop_and_delete_over_http(api, sm):
    tid = await job(sm)
    o = (await api.get("/api/followups")).json()
    assert o["enabled"] and o["settings"]["file"] == "config/followups.yaml"
    r = await api.post(f"/api/followups/{tid}/log", json={"channel": "LinkedIn", "contact": "Sam", "notes": "pinged"}, headers=HDR)
    assert r.status_code == 200
    lid = r.json()["id"]
    item = (await api.get("/api/followups")).json()["items"][0]
    assert item["count"] == 1 and item["log"][0]["channel"] == "LinkedIn" and item["urgency"] == "waiting"
    assert (await api.put(f"/api/followups/{tid}/pin", json={"on": "2030-01-01"}, headers=HDR)).status_code == 200
    assert (await api.get("/api/followups")).json()["items"][0]["next_on"] == "2030-01-01"
    assert (await api.put(f"/api/followups/{tid}/pin", json={"on": None}, headers=HDR)).status_code == 200
    assert (await api.put(f"/api/followups/{tid}/stop", json={"stopped": True}, headers=HDR)).status_code == 200
    assert (await api.get("/api/followups")).json()["tiles"]["stopped"] == 1
    assert (await api.delete(f"/api/followups/log/{lid}", headers={"Origin": OK})).status_code == 200
    assert (await api.get("/api/followups")).json()["items"][0]["count"] == 0
    assert (await api.delete(f"/api/followups/log/{lid}", headers={"Origin": OK})).status_code == 404


async def test_bad_input_is_refused(api, sm):
    tid = await job(sm)
    saved = await job(sm, "Saved", status="saved", applied_ago=None, changed_ago=1)

    def post(t, **b):
        return api.post(f"/api/followups/{t}/log", json=b, headers=HDR)

    assert (await post(tid, channel="Carrier pigeon")).status_code == 422
    assert (await post(tid, channel="Email", on="2999-01-01")).status_code == 422  # in the future
    assert (await post(tid, channel="Email", on="2026-01-02", next_on="2026-01-01")).status_code == 422
    assert (await post(saved, channel="Email")).status_code == 422  # a saved job has no follow-ups
    assert (await post("not-a-uuid", channel="Email")).status_code == 422
    assert (await post("00000000-0000-0000-0000-000000000000", channel="Email")).status_code == 404
    assert (await api.get("/api/followups")).json()["items"][0]["count"] == 0


async def test_off_switch_over_http(api, sm, monkeypatch, tmp_path):
    tid = await job(sm)
    f = tmp_path / "followups.yaml"
    f.write_text(fu.CONFIG.read_text().replace("enabled: true", "enabled: false"))
    monkeypatch.setattr(fu, "CONFIG", f)
    assert (await api.get("/api/followups/enabled")).json() == {"enabled": False}
    assert (await api.get("/api/followups")).json() == {"enabled": False}
    assert (await api.post(f"/api/followups/{tid}/log", json={"channel": "Email"}, headers=HDR)).status_code == 409


# ---------- migration ----------

async def test_migration_0016_matches_the_orm_models(engine):
    """Runs 0016's upgrade() in the scratch schema after removing what it adds, then diffs the result against the models."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    spec = spec_from_file_location("m0016", Path(__file__).parents[1] / "alembic/versions/0016_followups.py")
    m = module_from_spec(spec)
    spec.loader.exec_module(m)
    names = {"followup_log", "tracked_jobs"}
    async with engine.begin() as c:
        await c.execute(text("DROP TABLE IF EXISTS followup_log CASCADE"))
        await c.execute(text("ALTER TABLE tracked_jobs DROP COLUMN next_pin_on, DROP COLUMN reminders_stopped_at"))

        def go(sync):
            ctx = MigrationContext.configure(sync, opts={"include_object": lambda o, n, t, r, cn: (n in names if t == "table" else o.table.name in names)})
            with Operations.context(ctx):
                m.upgrade()
            return compare_metadata(ctx, Base.metadata)

        assert await c.run_sync(go) == []
