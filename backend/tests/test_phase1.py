import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.runner import run_once
from app.services import outbox, queue, registry
from app.services.ingest import ingest_batch, load_cursor
from tests.conftest import make_engine


def ev(eid="1", url=None, src="demo", title="Acme raises $5M seed"):
    return NormalizedEvent(source_name=src, source_event_id=eid, canonical_url=url,
                           event_type="funding_news", title=title, content="body")


async def count(s, table):
    return (await s.execute(text(f"select count(*) from {table}"))).scalar_one()


# ---------- registry ----------
async def test_register_toggle_and_independent_routes(sm):
    async with sm() as s:
        a = await registry.register_source(s, "Entrackr", "feed", category="funding")
        await registry.register_source(s, "Entrackr", "email_alert_parser")
        await s.commit()
        assert not await registry.is_enabled(s, "Entrackr", "feed")  # new routes start disabled
        await registry.set_enabled(s, "Entrackr", "feed", True)
        await s.commit()
        assert await registry.is_enabled(s, "Entrackr", "feed")
        assert not await registry.is_enabled(s, "Entrackr", "email_alert_parser")  # other route unaffected
        # re-registering updates fields but keeps enabled
        assert await registry.register_source(s, "Entrackr", "feed", priority=10) == a
        await s.commit()
        assert await registry.is_enabled(s, "Entrackr", "feed")


async def test_zero_cost_guard(sm):
    async with sm() as s:
        # A per-read-billed source cannot be registered through the normal API...
        with pytest.raises(IntegrityError):
            await registry.register_source(s, "PaidApi", "api", cost_per_read=0.01, cost_class="paid")
        await s.rollback()
        # ...only the owner can approve it explicitly.
        await s.execute(text("""insert into source_registry (name, connector_type, cost_per_read, paid_approved)
                                values ('PaidApi','api',0.01,true)"""))
        await s.commit()


# ---------- ingest / duplicates ----------
async def test_duplicate_source_events_rejected(sm):
    async with sm() as s:
        sid = await registry.register_source(s, "demo", "feed")
        sid2 = await registry.register_source(s, "other", "feed")
        n, d = await ingest_batch(s, sid, [ev("1"), ev("1"), ev("2")])
        assert (n, d) == (2, 1)
        # same canonical URL from a different source is also a duplicate
        n, d = await ingest_batch(s, sid, [ev("3", url="https://x.com/a")])
        n2, d2 = await ingest_batch(s, sid2, [ev("9", url="https://x.com/a")])
        assert (n, n2, d2) == (1, 0, 1)
        # events without ids/urls are allowed to coexist
        n, _ = await ingest_batch(s, sid, [ev(None), ev(None)])
        assert n == 2
        await s.commit()
        assert await count(s, "source_events") == 5
        assert await count(s, "job_queue") == 5  # one process_event job per stored event


async def test_dates_kept_separate_and_retention_set(sm):
    async with sm() as s:
        sid = await registry.register_source(s, "demo", "feed", retention_days=90)
        e = ev("1")
        e.published_at = e.detected_at.replace(year=2020)
        await ingest_batch(s, sid, [e])
        r = (await s.execute(text(
            "select published_at, detected_at, retention_expires_at - detected_at as r from source_events"))).one()
        assert r.published_at != r.detected_at and r.r.days == 90


# ---------- runner: toggle, isolation, health ----------
class Fake(SourceAdapter):
    connector_type = "feed"

    def __init__(self, name, items=(), fail=False):
        self.name, self.items, self.fail = name, list(items), fail

    async def fetch(self, cursor=None):
        if self.fail:
            raise RuntimeError("boom")
        return FetchResult(items=self.items, cursor={"n": len(self.items)})

    async def normalize(self, item):
        return ev(item, src=self.name)

    async def health_check(self):
        return True

    async def get_capabilities(self):
        return Capabilities(public_feed=True)


async def test_runner_disabled_failing_and_healthy_sources_are_independent(sm):
    async with sm() as s:
        for n in ("good", "bad", "off"):
            await registry.register_source(s, n, "feed")
        await registry.set_enabled(s, "good", "feed", True)
        await registry.set_enabled(s, "bad", "feed", True)
        await s.commit()
    assert (await run_once(sm, Fake("off", ["1"])))["reason"] == "disabled"
    for _ in range(3):
        assert (await run_once(sm, Fake("bad", fail=True)))["status"] == "error"  # never raises
    assert (await run_once(sm, Fake("good", ["1", "2"])))["new"] == 2
    second = await run_once(sm, Fake("good", ["1", "2"]))
    assert second["new"] == 0 and second["duplicates"] == 2  # same items twice -> stored once
    async with sm() as s:
        h = dict((await s.execute(text("select name, health_status from source_registry"))).all())
        assert h == {"good": "HEALTHY", "bad": "UNAVAILABLE", "off": "DISABLED"}
        sid = (await s.execute(text("select id from source_registry where name='good'"))).scalar_one()
        assert await load_cursor(s, str(sid)) == {"n": 2}
        assert await count(s, "source_events") == 2


# ---------- queue ----------
async def test_enqueue_is_idempotent(sm):
    async with sm() as s:
        assert await queue.enqueue(s, "k", {"a": 1}, "same")
        assert await queue.enqueue(s, "k", {"a": 1}, "same") is None
        assert await queue.enqueue(s, "other", {}, "same")
        await s.commit()


async def test_concurrent_workers_never_share_a_job(sm):
    async with sm() as s:
        for i in range(2):
            await queue.enqueue(s, "k", {}, str(i))
        await s.commit()
    async with sm() as a, sm() as b:
        ja = await queue.claim(a, "w1")
        jb = await queue.claim(b, "w2")  # a's claim is still uncommitted -> SKIP LOCKED
        assert len(ja) == len(jb) == 1 and ja[0]["id"] != jb[0]["id"]
        await a.commit()
        await b.commit()


async def test_retry_backoff_then_dead_letter(sm):
    async with sm() as s:
        await queue.enqueue(s, "k", {}, "x", max_attempts=2)
        await s.commit()
        j = (await queue.claim(s, "w"))[0]
        assert await queue.fail(s, j["id"], "w", "err1") == "pending"
        await s.commit()
        assert await queue.claim(s, "w") == []  # backoff: not due yet
        await s.execute(text("update job_queue set run_at = now()"))
        j = (await queue.claim(s, "w"))[0]
        assert j["attempts"] == 2
        assert await queue.fail(s, j["id"], "w", "err2") == "dead"
        await s.commit()
        assert await queue.counts(s) == {"dead": 1}


async def test_jobs_survive_restart_and_abandoned_leases_recover(schema):
    e1 = make_engine(schema)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    async with async_sessionmaker(e1)() as s:
        await queue.enqueue(s, "k", {"v": 1}, "a")
        await queue.enqueue(s, "k", {"v": 2}, "b")
        await s.commit()
        job = (await queue.claim(s, "crashing-worker"))[0]  # worker takes a job...
        await s.commit()
    await e1.dispose()  # ...and the whole process dies
    e2 = make_engine(schema)  # restart
    async with async_sessionmaker(e2)() as s:
        got = await queue.claim(s, "w2", lease_s=60)  # the second job survived the restart
        assert [g["payload"] for g in got] == [{"v": 2}]
        await s.execute(text("update job_queue set lease_expires_at = now() - interval '1 second' where id = :i"),
                        {"i": job["id"]})
        await s.commit()
        assert await queue.recover_abandoned(s) == 1  # lost lease is reclaimed, nothing lost
        await s.commit()
        assert (await queue.counts(s)).get("pending") == 1
        assert await count(s, "job_queue") == 2
    await e2.dispose()


async def test_stale_worker_cannot_complete_reclaimed_job(sm):
    async with sm() as s:
        await queue.enqueue(s, "k", {}, "a")
        j = (await queue.claim(s, "w1"))[0]
        await s.execute(text("update job_queue set locked_by='w2'"))
        assert not await queue.complete(s, j["id"], "w1")
        await s.commit()


# ---------- outbox ----------
async def test_outbox_dedup_retry_and_crash_recovery(sm):
    async with sm() as s:
        assert await outbox.add_alert(s, "evt-1", {"text": "hi"}, topic="India Funding")
        assert await outbox.add_alert(s, "evt-1", {"text": "hi"}) is None  # same alert twice -> one
        await s.commit()
        d = (await outbox.claim_pending(s))[0]
        assert await outbox.mark_failed(s, d["id"], "telegram 500") == "failed"
        await s.commit()
        assert await outbox.claim_pending(s) == []  # backing off
        await s.execute(text("update alert_deliveries set next_attempt_at = now()"))
        d = (await outbox.claim_pending(s))[0]
        assert d["attempts"] == 2
        await s.commit()
        # sender crashes while 'sending': lease expiry makes it claimable again
        await s.execute(text("update alert_deliveries set lease_expires_at = now() - interval '1 second'"))
        d = (await outbox.claim_pending(s))[0]
        await outbox.mark_sent(s, d["id"], "tg-77")
        await s.commit()
        assert (await s.execute(text("select status from alert_deliveries"))).scalar_one() == "sent"


# ---------- other constraints ----------
async def test_unique_constraints(sm):
    async with sm() as s:
        c = (await s.execute(text("insert into companies (name, canonical_name) values ('A','a') returning id"))).scalar_one()
        await s.execute(text("insert into company_domains (company_id, domain) values (:c, 'a.com')"), {"c": c})
        await s.commit()
        with pytest.raises(IntegrityError):
            await s.execute(text("insert into company_domains (company_id, domain) values (:c, 'a.com')"), {"c": c})
        await s.rollback()
        b = (await s.execute(text("insert into ats_boards (provider, board_token) values ('greenhouse','acme') returning id"))).scalar_one()
        ins = "insert into job_postings (ats_board_id, external_job_id, title) values (:b, '77', 'Eng')"
        await s.execute(text(ins), {"b": b})
        with pytest.raises(IntegrityError):
            await s.execute(text(ins), {"b": b})
        await s.rollback()


async def test_funding_identity_key_unique_only_when_set(sm):
    async with sm() as s:
        c = (await s.execute(text("insert into companies (name, canonical_name) values ('A','a') returning id"))).scalar_one()
        for _ in range(2):  # unset identity: several allowed
            await s.execute(text("insert into funding_rounds (company_id) values (:c)"), {"c": c})
        await s.execute(text("insert into funding_rounds (company_id, identity_key) values (:c,'a|seed|2026-11')"), {"c": c})
        with pytest.raises(IntegrityError):
            await s.execute(text("insert into funding_rounds (company_id, identity_key) values (:c,'a|seed|2026-11')"), {"c": c})
        await s.rollback()
