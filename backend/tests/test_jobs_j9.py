"""Jobs track J9: Jobs-track metrics, the two new failure rules, and that retention and `forget` leave the owner's tracked rows alone."""
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.services import ops, registry, retention

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "ledger_path", lambda: tmp_path / "ledger" / "deletions.jsonl")


async def route(s, name, kind="ats", **cols):
    sid = await registry.register_source(s, name, "api", config={"kind": kind})
    sets = ", ".join(f"{k} = :{k}" for k in {"enabled": 1, **cols})
    await s.execute(text(f"UPDATE source_registry SET {sets} WHERE id = :id"), {"enabled": True, **cols, "id": sid})
    return sid


async def search(s, name="Backend", status="active"):
    return (await s.execute(text("""INSERT INTO saved_searches (name, filters, interval_value, interval_unit, status)
        VALUES (:n, '{}'::jsonb, 6, 'hours', :st) RETURNING id"""), {"n": name, "st": status})).scalar_one()


async def run(s, sid, no, state):
    await s.execute(text("INSERT INTO saved_search_runs (search_id, run_no, state, error) VALUES (:i, :n, :s, :e)"),
                    {"i": sid, "n": no, "s": state, "e": "TimeoutError: boom" if state == "error" else None})


async def tracked(s, sid, title="Backend Engineer", status="applied"):
    tid = (await s.execute(text("""INSERT INTO tracked_jobs (source_id, external_job_id, title, company_name, url, status, applied_at)
        VALUES (:s, :e, :t, 'Acme', 'https://jobs.example/1', :st, now()) RETURNING id"""), {"s": sid, "e": f"{title}-1", "t": title, "st": status})).scalar_one()
    await s.execute(text("INSERT INTO tracked_job_events (tracked_job_id, to_status) VALUES (:i, :st)"), {"i": tid, "st": status})
    await s.execute(text("INSERT INTO followup_log (tracked_job_id, logged_on, status, channel) VALUES (:i, current_date, :st, 'Email')"), {"i": tid, "st": status})
    return tid


# ---- metrics ----

async def test_jobs_metrics_are_valid_and_count(sm):
    async with sm() as s:
        gh = await route(s, "greenhouse:acme", last_success_at=NOW)
        sid = await search(s)
        await search(s, "Paused", "paused")
        await run(s, sid, 1, "ok")
        await run(s, sid, 2, "error")
        await s.execute(text("UPDATE saved_searches SET last_run_at = now() - interval '1 hour' WHERE id = :i"), {"i": sid})
        await tracked(s, gh, "A", "applied")
        await tracked(s, gh, "B", "saved")
        await tracked(s, gh, "C", "saved")
        await s.commit()
        t = await ops.metrics_text(s)
    pattern = r'[a-z_]+(\{[^{}]*\})? -?[0-9.e+]+'
    assert all(re.fullmatch(pattern, ln) for ln in t.splitlines() if ln and not ln.startswith("#"))
    assert 'saved_searches{status="active"} 1.0' in t and 'saved_searches{status="paused"} 1.0' in t
    assert 'saved_search_runs{state="ok"} 1.0' in t and 'saved_search_runs{state="error"} 1.0' in t
    assert re.search(r'saved_search_last_run_age_seconds\{search="Backend"\} 3[56][0-9]{2}\.', t)  # about an hour; the paused search has no series
    assert 'tracked_jobs{status="saved"} 2.0' in t and 'tracked_jobs{status="applied"} 1.0' in t
    assert 'boards_by_state{state="empty"} 1.0' in t or 'boards_by_state{state="pending"} 1.0' in t or 'boards_by_state{state="live"} 1.0' in t
    for name in ("jobs_open", "followups_by_urgency", "boards_by_state"):
        assert f"# TYPE {name} gauge" in t
    assert 'followups_by_urgency{urgency="urgent"}' in t


# ---- failure rules ----

async def test_failing_saved_search_alerts_once_per_episode(sm):
    async with sm() as s:
        sid = await search(s)
        await run(s, sid, 1, "ok")
        for n in (2, 3):
            await run(s, sid, n, "error")
        assert [k for k, _ in await ops.problems(s) if k.startswith("scan_failing")] == []  # two failures: a blip
        await run(s, sid, 4, "error")
        found = [(k, m) for k, m in await ops.problems(s) if k.startswith("scan_failing")]
        assert len(found) == 1 and "Backend" in found[0][1] and "TimeoutError" in found[0][1]
        assert await ops.raise_alerts(s, await ops.problems(s)) == 1
        assert await ops.raise_alerts(s, await ops.problems(s)) == 0  # the same episode is not queued again
        await run(s, sid, 5, "ok")
        assert [k for k, _ in await ops.problems(s) if k.startswith("scan_failing")] == []  # recovered
        for n in (6, 7, 8):
            await run(s, sid, n, "error")
        assert await ops.raise_alerts(s, await ops.problems(s)) == 1  # broke again: a new episode
        await s.execute(text("UPDATE saved_searches SET status = 'paused' WHERE id = :i"), {"i": sid})
        assert [k for k, _ in await ops.problems(s) if k.startswith("scan_failing")] == []  # a paused search is the owner's choice


async def test_board_broken_covers_what_source_down_does_not(sm):
    async with sm() as s:
        stale = await route(s, "greenhouse:stale", last_success_at=NOW - timedelta(days=4))
        await route(s, "greenhouse:fresh", last_success_at=NOW - timedelta(hours=2))
        await route(s, "greenhouse:down", last_success_at=NOW - timedelta(days=4), consecutive_failures=3)  # source_down owns this one
        await route(s, "Rss feed", kind="feed", last_success_at=NOW - timedelta(days=4))  # not a board
        circuit = await route(s, "himalayas", kind="himalayas", last_success_at=NOW, circuit_open_until=NOW + timedelta(hours=1))
        msgs = [m for k, m in await ops.problems(s) if k.startswith("board_broken")]
        assert len(msgs) == 2 and any("greenhouse:stale" in m and "no success since" in m for m in msgs) and any("himalayas" in m and "circuit" in m for m in msgs)
        assert not any("fresh" in m or "down" in m or "Rss" in m for m in msgs)
        assert next(m for k, m in await ops.problems(s) if k.startswith("source_down")).startswith("Source down: greenhouse:down")
        await s.execute(text("UPDATE source_registry SET last_success_at = now() WHERE id = :i"), {"i": stale})
        await s.execute(text("UPDATE source_registry SET circuit_open_until = NULL WHERE id = :i"), {"i": circuit})
        assert [k for k, _ in await ops.problems(s) if k.startswith("board_broken")] == []  # recovered


# ---- retention and forget leave tracked rows alone ----

async def test_purge_and_forget_keep_tracked_rows_and_their_history(sm):
    async with sm() as s:
        gh = await route(s, "greenhouse:acme")
        tid = await tracked(s, gh)
        # the job's event is past retention and gets scrubbed; the tracked row keeps its own copy
        await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content, author_name, raw_metadata, published_at, detected_at, retention_expires_at)
            VALUES (:s, 'e1', 'job', 'Backend Engineer', 'body', 'Ann', '{}'::jsonb, now(), now(), now() - interval '1 day')"""), {"s": gh})
        sid = await search(s)
        await run(s, sid, 1, "ok")
        out = await retention.purge(s)
        assert out["events_scrubbed"] == 1 and out["saved_search_runs"] == 0  # a fresh run is kept
        assert await retention.forget(s, "Ann") == 0  # already cleared; forget touches events only
        row = (await s.execute(text("SELECT title, company_name, url, status FROM tracked_jobs WHERE id = :i"), {"i": tid})).one()
        assert tuple(row) == ("Backend Engineer", "Acme", "https://jobs.example/1", "applied")
        for table in ("tracked_job_events", "followup_log"):
            assert (await s.execute(text(f"SELECT count(*) FROM {table} WHERE tracked_job_id = :i"), {"i": tid})).scalar_one() == 1
        # the 90-day prune is the only thing that removes run history
        await s.execute(text("UPDATE saved_search_runs SET started_at = now() - interval '91 days'"))
        assert (await retention.purge(s))["saved_search_runs"] == 1
        assert (await s.execute(text("SELECT count(*) FROM saved_searches"))).scalar_one() == 1
