"""Phase 10: metrics, failure alerting, retention, secret redaction, retry and restart recovery."""
import os
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.logging import redact
from app.services import ops, outbox, queue, registry, retention
from app.services.notify import deliver_pending

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    """Never touch the real deletion ledger."""
    f = tmp_path / "ledger" / "deletions.jsonl"
    monkeypatch.setattr(retention, "ledger_path", lambda: f)
    return f


async def mk_source(s, name, route="feed", **cols):
    sid = await registry.register_source(s, name, route)
    sets = ", ".join(f"{k} = :{k}" for k in {"enabled": 1, **cols})
    await s.execute(text(f"UPDATE source_registry SET {sets} WHERE id = :id"), {"enabled": True, **cols, "id": sid})
    return sid


async def mk_run(s, sid, hours_ago, status="ok", fetched=0, new=0, error=None):
    await s.execute(text("""INSERT INTO source_runs (source_id, started_at, finished_at, status, items_fetched, items_new, error)
        VALUES (:s, now() - make_interval(hours => :h), now(), :st, :f, :n, :e)"""), {"s": sid, "h": hours_ago, "st": status, "f": fetched, "n": new, "e": error})


async def mk_event(s, sid, key, *, etype="article", status="none", expires_days=90, **cols):
    cols = {"content": "body", "author_name": "Ann", "author_url": "https://bsky.app/profile/ann",
            "raw_metadata": '{"region": "india", "tags": ["x"], "ignored_reason": "r"}', **cols}
    await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content, author_name, author_url, raw_metadata,
        processing_status, published_at, detected_at, retention_expires_at)
        VALUES (:s, :k, :t, 'T', :content, :author_name, :author_url, CAST(:raw_metadata AS jsonb), :st, now() - interval '1 hour', now(),
                now() + make_interval(days => :d))"""), {"s": sid, "k": key, "t": etype, "st": status, "d": expires_days, **cols})


# ---- secret handling ----

def test_redact_secrets_and_credential_shapes(monkeypatch):
    from app.core import logging as lg

    class C:
        telegram_bot_token, imap_password, github_token, jooble_api_key = "123456:" + "A" * 30, "hunter2-long-pass", "", "short"
    monkeypatch.setattr("app.core.config.get_settings", lambda: C)
    url = f"https://api.telegram.org/bot{C.telegram_bot_token}/sendMessage"
    out = redact(f"ConnectError for url '{url}' password hunter2-long-pass Authorization: Bearer abc.def postgresql://user:pw@host/db short")
    assert C.telegram_bot_token not in out and "hunter2" not in out and "abc.def" not in out and "user:pw@" not in out
    assert "short" in out  # under 8 characters: not treated as a secret value
    assert lg._redact_event(None, "x", {"error": f"bot{'1' * 8}:{'B' * 25}", "n": 3}) == {"error": "[redacted]", "n": 3}


async def test_error_text_is_redacted_before_storage(sm, monkeypatch):
    class C:
        telegram_bot_token, imap_password, github_token, jooble_api_key = "999:" + "Z" * 30, "", "", ""
    monkeypatch.setattr("app.core.config.get_settings", lambda: C)
    async with sm() as s:
        a = await outbox.add_alert(s, "k", {"x": 1})
        await outbox.claim_pending(s)
        await outbox.mark_failed(s, a, f"boom bot{C.telegram_bot_token}/sendMessage")
        await s.commit()
        assert C.telegram_bot_token not in (await s.execute(text("SELECT last_error FROM alert_deliveries"))).scalar_one()


def test_env_file_warning(tmp_path, monkeypatch):
    from app.core.logging import env_file_warning

    monkeypatch.chdir(tmp_path)
    assert env_file_warning() is None
    f = tmp_path / ".env"
    f.write_text("X=1")
    os.chmod(f, 0o644)
    assert "chmod 600" in env_file_warning()
    os.chmod(f, 0o600)
    assert env_file_warning() is None


# ---- metrics ----

async def test_metrics_text_is_valid_and_counts(sm):
    async with sm() as s:
        a = await mk_source(s, 'Fe"ed', last_success_at=NOW)
        await mk_run(s, a, 3, fetched=10, new=4)
        await mk_run(s, a, 2, status="error", error="Client error '403 Forbidden' for url")
        await mk_run(s, a, 1, status="skipped")
        await mk_event(s, a, "e1", status="funding")
        await mk_event(s, a, "e2", etype="job")
        await queue.enqueue(s, "process_event", {}, "j1")
        await queue.enqueue(s, "llm_enrich", {}, "j2")
        await s.execute(text("UPDATE job_queue SET status = 'dead' WHERE kind = 'llm_enrich'"))
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('A', 'a') RETURNING id"))).scalar_one()
        for st in ("VERIFIED", "VERIFIED", "PARTIALLY_VERIFIED"):  # statuses are stored upper-case
            await s.execute(text("INSERT INTO funding_rounds (company_id, verification_status) VALUES (:c, :s)"), {"c": cid, "s": st})
        await s.commit()
        t = await ops.metrics_text(s)
    lines = [ln for ln in t.splitlines() if ln and not ln.startswith("#")]
    pattern = r'[a-z_]+(\{[^{}]*\})? -?[0-9.e+]+'
    assert all(re.fullmatch(pattern, ln) for ln in lines), [ln for ln in lines if not re.fullmatch(pattern, ln)]
    names = {m for m, *_ in ops.METRICS}
    for required in ("source_last_success_timestamp", "source_detection_latency_seconds", "source_http_errors_total", "source_rate_limit_total",
                     "source_access_denied_total", "source_events_received_total", "source_events_relevant_total", "source_events_deduplicated_total",
                     "company_discovered_total", "ats_boards_discovered_total", "llm_extraction_failures_total", "job_queue_pending", "job_queue_failed",
                     "telegram_delivery_failures_total", "funding_verified_total", "jobs_discovered_total"):
        assert required in names and f"# TYPE {required} " in t
    lab = 'source="Fe\\"ed",route="feed"'  # the quote in the source name is escaped
    assert f"source_http_errors_total{{{lab}}} 1.0" in t and f"source_access_denied_total{{{lab}}} 1.0" in t
    assert f"source_rate_limit_total{{{lab}}} 1.0" in t and f"source_events_deduplicated_total{{{lab}}} 6.0" in t
    assert f"source_events_received_total{{{lab}}} 2.0" in t and f"source_events_relevant_total{{{lab}}} 1.0" in t
    assert 'job_queue_pending{kind="process_event"} 1.0' in t and 'job_queue_failed{kind="llm_enrich"} 1.0' in t
    assert "jobs_discovered_total 1.0" in t and "funding_verified_total 2.0" in t
    assert re.search(r'source_detection_latency_seconds\{' + re.escape(lab) + r'\} 3[0-9]{3}\.', t)  # published 1 h before detection


# ---- failure alerting ----

async def test_failed_source_detected_once_per_episode(sm):
    async with sm() as s:
        sid = await mk_source(s, "Down", last_success_at=NOW - timedelta(days=1), consecutive_failures=3, last_error="HTTPStatusError: 403")
        await mk_source(s, "Blip", consecutive_failures=2)  # below the repeat threshold: no alert
        found = await ops.problems(s)
        assert [m for _, m in found if "Down" in m] and not [m for _, m in found if "Blip" in m]
        assert await ops.raise_alerts(s, found) == 1
        assert await ops.raise_alerts(s, await ops.problems(s)) == 0  # same episode: not queued again
        await s.execute(text("UPDATE source_registry SET last_success_at = now(), consecutive_failures = 0 WHERE id = :i"), {"i": sid})
        assert await ops.problems(s) == []  # recovered
        await s.execute(text("UPDATE source_registry SET consecutive_failures = 3 WHERE id = :i"), {"i": sid})
        assert await ops.raise_alerts(s, await ops.problems(s)) == 1  # failed again after recovering: a new episode
        assert (await s.execute(text("SELECT topic FROM alert_deliveries"))).scalars().all() == ["Ops", "Ops"]


async def test_silent_source_detected(sm):
    async with sm() as s:
        quiet = await mk_source(s, "Quiet")
        busy = await mk_source(s, "Busy")
        young = await mk_source(s, "Young")
        for h in (60, 50, 40):  # earlier runs returned items
            await mk_run(s, quiet, h, fetched=10)
            await mk_run(s, busy, h, fetched=10)
        for h in (20, 10, 2):  # the last 24 h: every run "ok"
            await mk_run(s, quiet, h, fetched=0)
            await mk_run(s, busy, h, fetched=10)
            await mk_run(s, young, h, fetched=0)  # never returned anything: not a change, not flagged
        found = [m for _, m in await ops.problems(s)]
        assert len(found) == 1 and "Quiet" in found[0] and "Silent" in found[0]


async def test_queue_and_notification_problems(sm, tmp_path):
    async with sm() as s:
        for i, kind in enumerate(("process_event", "llm_enrich")):
            await queue.enqueue(s, kind, {}, f"d{i}")
            await queue.enqueue(s, kind, {}, f"p{i}")
        await s.execute(text("UPDATE job_queue SET status = 'dead', finished_at = now() WHERE idempotency_key LIKE 'd%'"))
        await s.execute(text("UPDATE job_queue SET run_at = now() - interval '1 hour' WHERE idempotency_key LIKE 'p%'"))
        msgs = " ".join(m for _, m in await ops.problems(s, backup_dir=tmp_path))
        assert "'process_event' job(s) exhausted" in msgs and "'process_event' job(s) pending" in msgs
        assert "llm_enrich" not in msgs  # model off: its jobs are expected to wait
        assert "llm_enrich" in " ".join(m for _, m in await ops.problems(s, llm_enabled=True, backup_dir=tmp_path))
        assert "Backup missing" in msgs

        await s.execute(text("TRUNCATE job_queue"))
        a = await outbox.add_alert(s, "x", {"y": 1})
        await s.execute(text("UPDATE alert_deliveries SET next_attempt_at = now() - interval '1 hour' WHERE id = :i"), {"i": a})
        assert "Alert backlog" in " ".join(m for _, m in await ops.problems(s))
        await s.execute(text("UPDATE alert_deliveries SET status = 'dead', next_attempt_at = now()"))
        assert "undeliverable" in " ".join(m for _, m in await ops.problems(s))


async def test_worker_and_backup_staleness(sm, tmp_path):
    async with sm() as s:
        await queue.heartbeat(s, "w1")
        assert "Worker silent" not in " ".join(m for _, m in await ops.problems(s))
        await s.execute(text("UPDATE worker_heartbeats SET last_seen_at = now() - interval '10 minutes'"))
        assert "Worker silent" in " ".join(m for _, m in await ops.problems(s))
        d = tmp_path / "daily"
        d.mkdir()
        f = d / "db_1.dump"
        f.write_text("x")
        assert not [m for _, m in await ops.problems(s, backup_dir=tmp_path) if "Backup" in m]  # fresh
        old = time.time() - 40 * 3600
        os.utime(f, (old, old))
        assert "Backup stale" in " ".join(m for _, m in await ops.problems(s, backup_dir=tmp_path))


# ---- failed notifications retry, restarts lose nothing ----

class Flaky:
    def __init__(self, fail):
        self.fail, self.sent = fail, []

    async def send(self, topic, message):
        if self.fail:
            raise RuntimeError("telegram 502")
        self.sent.append((topic, message))
        return "42"


async def test_failed_notification_is_retried_until_delivered(sm):
    async with sm() as s:
        await ops.raise_alerts(s, [("k1", "Source down: X")])
        await s.commit()
    assert await deliver_pending(sm, Flaky(fail=True), pause_s=0) == 0
    async with sm() as s:
        st = (await s.execute(text("SELECT status, attempts, last_error, next_attempt_at > now() AS later FROM alert_deliveries"))).one()
        assert (st.status, st.attempts, st.later) == ("failed", 1, True) and "502" in st.last_error
        await s.execute(text("UPDATE alert_deliveries SET next_attempt_at = now()"))  # the backoff elapses
        await s.commit()
    ok = Flaky(fail=False)
    assert await deliver_pending(sm, ok, pause_s=0) == 1 and "OPS" in ok.sent[0][1] and "Source down: X" in ok.sent[0][1]
    async with sm() as s:
        assert (await s.execute(text("SELECT status FROM alert_deliveries"))).scalar_one() == "sent"


async def test_worker_restart_loses_no_job_and_no_alert(sm):
    async with sm() as s:
        await queue.enqueue(s, "process_event", {"event_id": "e"}, "job")
        await outbox.add_alert(s, "alert", {"label": "ops", "title": "t", "source": "s", "detected_at": NOW.isoformat()}, topic="Ops")
        await s.commit()
        assert len(await queue.claim(s, "old-worker")) == 1  # claimed, then the worker is killed before finishing
        assert len(await outbox.claim_pending(s)) == 1  # alert leased, killed before mark_sent
        await s.commit()
        assert await queue.claim(s, "new-worker") == []  # leases still held
        await s.execute(text("UPDATE job_queue SET lease_expires_at = now() - interval '1 s'"))
        await s.execute(text("UPDATE alert_deliveries SET lease_expires_at = now() - interval '1 s'"))
        await s.commit()
        again = await queue.claim(s, "new-worker")  # the restarted worker picks both up
        assert len(again) == 1 and again[0]["attempts"] == 2
        redo = await outbox.claim_pending(s)
        assert len(redo) == 1 and redo[0]["possible_duplicate"]  # flagged "(resent)"


# ---- retention and deletion ----

async def test_retention_scrubs_expired_events_and_prunes(sm):
    async with sm() as s:
        sid = await mk_source(s, "Src")
        await mk_event(s, sid, "old", expires_days=-1)
        await mk_event(s, sid, "fresh", expires_days=30)
        await queue.enqueue(s, "process_event", {}, "d-old")
        await queue.enqueue(s, "process_event", {}, "d-new")
        await queue.enqueue(s, "process_event", {}, "dead-new")
        await s.execute(text("UPDATE job_queue SET status = 'done', finished_at = now() - interval '31 days' WHERE idempotency_key = 'd-old'"))
        await s.execute(text("UPDATE job_queue SET status = 'done', finished_at = now() WHERE idempotency_key = 'd-new'"))
        await s.execute(text("UPDATE job_queue SET status = 'dead', finished_at = now() - interval '31 days' WHERE idempotency_key = 'dead-new'"))
        await queue.heartbeat(s, "gone")
        await s.execute(text("UPDATE worker_heartbeats SET last_seen_at = now() - interval '8 days'"))
        await mk_run(s, sid, 24 * 91)
        await mk_run(s, sid, 1)
        out = await retention.purge(s)
        assert out["events_scrubbed"] == 1 and out["job_queue_done"] == 1 and out["worker_heartbeats"] == 1 and out["source_runs"] == 1
        assert out["job_queue_dead"] == 0  # dead jobs are kept 90 days for diagnosis
        old = (await s.execute(text("SELECT content, author_name, author_url, title, raw_metadata FROM source_events WHERE source_event_id = 'old'"))).one()
        assert old.content is None and old.author_name is None and old.author_url is None and old.title == "T"
        assert old.raw_metadata == {"scrubbed": True, "region": "india", "ignored_reason": "r"}  # keys later code reads survive; tags do not
        fresh = (await s.execute(text("SELECT content, author_name FROM source_events WHERE source_event_id = 'fresh'"))).one()
        assert (fresh.content, fresh.author_name) == ("body", "Ann")
        assert (await retention.purge(s))["events_scrubbed"] == 0  # idempotent


async def test_forget_clears_a_person(sm):
    async with sm() as s:
        sid = await mk_source(s, "Src")
        await mk_event(s, sid, "a")
        await mk_event(s, sid, "b", author_name="Bob", author_url="https://bsky.app/profile/bob")
        assert await retention.forget(s, "ANN") == 1 and await retention.forget(s, "https://bsky.app/profile/bob") == 1
        assert await retention.forget(s, "nobody") == 0
        assert (await s.execute(text("SELECT count(*) FROM source_events WHERE author_name IS NOT NULL OR author_url IS NOT NULL"))).scalar_one() == 0


async def test_deletion_ledger_survives_a_restore(sm, ledger):
    async with sm() as s:
        sid = await mk_source(s, "Src")
        await mk_event(s, sid, "a")
        await mk_event(s, sid, "b", author_name="Bob", author_url="https://bsky.app/profile/bob")
        assert await retention.forget(s, "  Ann ") == 1 and await retention.forget(s, "Ann") == 0  # repeated request: one ledger line
        await s.commit()
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1 and "ann" not in ledger.read_text().lower().replace("2026", "")  # hash only, never the name
    assert oct(ledger.stat().st_mode & 0o777) == "0o600"
    # "restore": a backup taken before the request brings the person back
    async with sm() as s:
        await s.execute(text("UPDATE source_events SET author_name = 'ANN', author_url = 'https://bsky.app/profile/ann' WHERE source_event_id = 'a'"))
        await s.execute(text("UPDATE source_events SET author_name = 'Ann' WHERE source_event_id = 'b'"))  # same person under another event
        await s.commit()
    async with sm() as s:
        out = await retention.purge(s)  # restore.sh and the hourly worker both run this
        assert out["ledger_cleared"] == 2
        rows = (await s.execute(text("SELECT source_event_id, author_name, author_url FROM source_events ORDER BY 1"))).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [("a", None, None), ("b", None, None)]
