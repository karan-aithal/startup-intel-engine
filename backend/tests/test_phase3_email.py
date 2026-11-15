"""Phase 3: IMAP connector plumbing, parser registry, fallback, multi-event emails, dedup against other sources."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.connectors import email_parsers
from app.connectors.base import NormalizedEvent
from app.connectors.imap import ImapAdapter
from app.connectors.runner import run_once
from app.services import registry
from app.worker import process_jobs


def msg(uid=1, frm="alerts@jobs.example.com", subject="3 new jobs", mid="m1"):
    return {"uid": uid, "from_email": frm, "subject": subject, "date": datetime.now(UTC) - timedelta(minutes=3),
            "html": "<p>x</p>", "text": "x", "message_id": mid}


def adapter(msgs, seen=None):
    async def fetcher(cursor):
        if seen is not None:
            seen.append(cursor)
        new = [m for m in msgs if m["uid"] > (cursor or {}).get("last_uid", 0)]
        return new, {"uidvalidity": "1", "last_uid": max((m["uid"] for m in new), default=(cursor or {}).get("last_uid", 0))}
    return ImapAdapter("Alerts Inbox", fetcher=fetcher)


@pytest.fixture
def clean_registry():
    saved = list(email_parsers._PARSERS)
    email_parsers._PARSERS.clear()
    yield
    email_parsers._PARSERS[:] = saved


async def reg(sm):
    async with sm() as s:
        await registry.register_source(s, "Alerts Inbox", "email_alert_parser")
        await registry.set_enabled(s, "Alerts Inbox", "email_alert_parser", True)
        await s.commit()


def test_requires_credentials():
    with pytest.raises(ValueError):
        ImapAdapter("x")


async def test_unknown_sender_uses_fallback(clean_registry):
    evs = await adapter([]).normalize(msg(frm="x@unknown.io"))
    assert len(evs) == 1 and evs[0].event_type == "email" and evs[0].raw_metadata["parser"] == "fallback"
    assert evs[0].title == "3 new jobs" and evs[0].content is None  # body is never stored by the fallback


async def test_registered_parser_matches_domain_and_subdomain(clean_registry):
    @email_parsers.register("jobs.example.com")
    def p(m, source):
        return [NormalizedEvent(source_name=source, source_event_id=f"{m['uid']}-{i}", event_type="job", title=f"Job {i}")
                for i in range(3)]

    a = adapter([])
    assert len(await a.normalize(msg(frm="a@jobs.example.com"))) == 3
    assert len(await a.normalize(msg(frm="a@mail.jobs.example.com"))) == 3
    assert (await a.normalize(msg(frm="a@notjobs.example.com")))[0].raw_metadata["parser"] == "fallback"


async def test_broken_parser_falls_back_instead_of_losing_the_email(clean_registry):
    @email_parsers.register("jobs.example.com")
    def boom(m, source):
        raise RuntimeError("template changed")

    evs = await adapter([]).normalize(msg())
    assert evs[0].raw_metadata["parser"] == "fallback"


async def test_cursor_advances_and_second_poll_is_empty(sm, clean_registry):
    await reg(sm)
    seen = []
    a = adapter([msg(1, mid="a"), msg(2, mid="b", subject="Acme raises $5 million in Seed round led by X")], seen)
    r1 = await run_once(sm, a)
    r2 = await run_once(sm, a)
    assert r1["new"] == 2 and r2["fetched"] == 0 and seen[1]["last_uid"] == 2


async def test_one_email_many_events_and_cross_source_dedup(sm, clean_registry):
    await reg(sm)

    @email_parsers.register("jobs.example.com")
    def p(m, source):
        return [NormalizedEvent(source_name=source, source_event_id=f"{m['uid']}-{i}", event_type="job",
                                title=f"Job {i}", canonical_url=f"https://careers.example.com/{i}", published_at=m["date"])
                for i in range(3)]

    async with sm() as s:  # same job URL already collected from another source
        await registry.register_source(s, "greenhouse:x", "api")
        sid = (await s.execute(text("select id from source_registry where name='greenhouse:x'"))).scalar_one()
        await s.execute(text("insert into source_events (source_id, source_event_id, canonical_url, event_type, title) "
                             "values (:i, 'g1', 'https://careers.example.com/1', 'job', 'Job 1')"), {"i": sid})
        await s.commit()
    r = await run_once(sm, adapter([msg()]))
    assert r["new"] == 2 and r["duplicates"] == 1  # URL 1 deduplicated against the other source
    await process_jobs(sm)
    async with sm() as s:
        n = (await s.execute(text("select count(*) from alert_deliveries"))).scalar_one()
    assert n == 2  # both new jobs are recent -> alerts; the duplicate produced none


async def test_latency_uses_published_and_detected(sm, clean_registry):
    await reg(sm)
    await run_once(sm, adapter([msg()]))
    async with sm() as s:
        sec = (await s.execute(text("select extract(epoch from detected_at - published_at) from source_events"))).scalar_one()
    assert 170 < sec < 240  # email Date was 3 minutes before detection
