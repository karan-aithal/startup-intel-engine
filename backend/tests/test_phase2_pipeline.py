"""Phase 2 acceptance: dedup to one alert, both timestamps, source isolation, restart safety."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.connectors.api import AtsAdapter, HackerNewsAdapter, SecFormDAdapter
from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.runner import run_once
from app.services import registry
from app.services.notify import deliver_pending
from app.services.pipeline import format_alert, pick_topic
from app.worker import process_jobs


class Fake(SourceAdapter):
    connector_type = "feed"

    def __init__(self, name, events=None, boom=False):
        self.name, self.events, self.boom = name, events or [], boom

    async def fetch(self, cursor=None):
        if self.boom:
            raise RuntimeError("source down")
        return FetchResult(items=self.events, cursor={"n": len(self.events)})

    async def normalize(self, item):
        return item

    async def health_check(self):
        return True

    async def get_capabilities(self):
        return Capabilities()


def funding(eid="1", url="https://e.com/a"):
    return NormalizedEvent(source_name="A", source_event_id=eid, canonical_url=url, event_type="article",
                           title="Acme raises $5 million in Seed round led by X",
                           published_at=datetime.now(UTC) - timedelta(minutes=7),
                           raw_metadata={"region": "india"})


class Sink:
    def __init__(self):
        self.sent = []

    async def send(self, topic, message):
        self.sent.append((topic, message))


async def register(sm, *names):
    async with sm() as s:
        for n in names:
            await registry.register_source(s, n, "feed")
            await registry.set_enabled(s, n, "feed", True)
        await s.commit()


async def alerts(sm):
    async with sm() as s:
        return (await s.execute(text("select topic, payload, status from alert_deliveries"))).all()


async def test_same_item_twice_one_alert_with_both_timestamps(sm):
    await register(sm, "A")
    a = Fake("A", [funding()])
    for _ in range(2):  # collected twice
        await run_once(sm, a)
        await process_jobs(sm)
    rows = await alerts(sm)
    assert len(rows) == 1 and rows[0].topic == "India Funding"
    sink = Sink()
    assert await deliver_pending(sm, sink, pause_s=0) == 1
    msg = sink.sent[0][1]
    assert "Published:" in msg and "Detected:" in msg and "IST" in msg


async def test_second_source_same_url_still_one_alert(sm):
    await register(sm, "A", "B")
    await run_once(sm, Fake("A", [funding("1")]))
    await run_once(sm, Fake("B", [funding("zz")]))  # same canonical URL via another source
    await process_jobs(sm)
    assert len(await alerts(sm)) == 1


async def test_one_dead_source_does_not_stop_others(sm):
    await register(sm, "A", "B")
    bad = await run_once(sm, Fake("B", boom=True))
    good = await run_once(sm, Fake("A", [funding()]))
    assert bad["status"] == "error" and good["status"] == "ok" and good["new"] == 1
    async with sm() as s:
        st = dict((await s.execute(text("select name, health_status from source_registry"))).all())
    assert st["B"] == "DEGRADED" and st["A"] == "HEALTHY"


async def test_restart_loses_nothing(sm):
    await register(sm, "A")
    await run_once(sm, Fake("A", [funding()]))
    async with sm() as s:  # a worker claims the job then dies: lease expires, job still 'running'
        await s.execute(text("update job_queue set status='running', locked_by='dead', lease_expires_at=now()-interval '1 min'"))
        await s.commit()
    await process_jobs(sm)  # next worker recovers and processes it
    assert len(await alerts(sm)) == 1
    async with sm() as s:  # a sender dies mid-send: alert stuck in 'sending' is re-delivered
        await s.execute(text("update alert_deliveries set status='sending', lease_expires_at=now()-interval '1 min'"))
        await s.commit()
    sink = Sink()
    assert await deliver_pending(sm, sink, pause_s=0) == 1 and len(sink.sent) == 1


async def test_failed_delivery_retries_not_lost(sm):
    await register(sm, "A")
    await run_once(sm, Fake("A", [funding()]))
    await process_jobs(sm)

    class Bad:
        async def send(self, *_):
            raise RuntimeError("telegram 429")

    assert await deliver_pending(sm, Bad(), pause_s=0) == 0
    assert (await alerts(sm))[0].status == "failed"  # retried later by backoff, not dropped


async def test_rumour_goes_to_unverified_and_old_jobs_do_not_alert(sm):
    await register(sm, "A")
    rumour = NormalizedEvent(source_name="A", source_event_id="r", canonical_url="https://e.com/r",
                             event_type="article", title="Monzo in talks to raise up to $500M Series C")
    old_job = NormalizedEvent(source_name="A", source_event_id="j", canonical_url="https://e.com/j", event_type="job",
                              title="Engineer", published_at=datetime.now(UTC) - timedelta(days=30))
    await run_once(sm, Fake("A", [rumour, old_job]))
    await process_jobs(sm)
    rows = await alerts(sm)
    assert [r.topic for r in rows] == ["Unverified and Risk"]


def test_topic_and_format_escapes_html():
    assert pick_topic("hiring", False) == "Global Hiring" and pick_topic("funding", True) == "India Funding"
    m = format_alert("Global Funding", {"label": "funding", "title": "<b>x</b>", "source": "S", "detected_at": datetime.now(UTC)})
    assert "&lt;b&gt;x&lt;/b&gt;" in m and "Published: unknown" in m


# ---------- connectors ----------
def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_hn_cursor_and_normalize():
    hit = {"objectID": "9", "title": "Acme raises seed", "url": None, "created_at": "2026-11-12T10:00:00Z", "created_at_i": 1000}
    a = HackerNewsAdapter("HN", ["raises", "seed"], client(lambda r: httpx.Response(200, json={"hits": [hit]})))
    r = await a.fetch({"since": 5})
    assert len(r.items) == 1 and r.cursor == {"since": 1000}  # two queries, same hit, deduped
    ev = await a.normalize(r.items[0])
    assert ev.canonical_url == "https://news.ycombinator.com/item?id=9" and ev.published_at.year == 2026


async def test_ats_normalize_three_providers():
    gh = await AtsAdapter("greenhouse", "x").normalize({"id": 1, "title": "SRE", "absolute_url": "https://g/1",
        "location": {"name": "Pune"}, "first_published": "2026-11-12T10:00:00-04:00"})
    lv = await AtsAdapter("lever", "x").normalize({"id": "a", "text": "PM", "hostedUrl": "https://l/a",
        "createdAt": 1786469891368, "categories": {"location": "Remote", "team": "Product"}})
    ab = await AtsAdapter("ashby", "x").normalize({"id": "b", "title": "Dev", "jobUrl": "https://a/b",
        "publishedAt": "2026-11-01T00:00:00.000+00:00", "location": "Bengaluru", "department": "Eng"})
    hidden = await AtsAdapter("ashby", "x").normalize({"id": "c", "title": "t", "jobUrl": "u", "isListed": False})
    assert (gh.location, lv.raw_metadata["team"], ab.raw_metadata["team"]) == ("Pune", "Product", "Eng")
    assert gh.published_at and lv.published_at and ab.published_at and hidden is None
    with pytest.raises(ValueError):
        AtsAdapter("workday", "x")


async def test_ats_fetch_shapes():
    a = AtsAdapter("lever", "x", client(lambda r: httpx.Response(200, json=[{"id": "1"}])))
    b = AtsAdapter("ashby", "x", client(lambda r: httpx.Response(200, json={"jobs": [{"id": "1"}, {"id": "2"}]})))
    assert len((await a.fetch()).items) == 1 and len((await b.fetch()).items) == 2


async def test_sec_needs_email_and_skips_amendments():
    with pytest.raises(ValueError):
        SecFormDAdapter("SEC", "")
    a = SecFormDAdapter("SEC", "ops@example.com")
    assert "ops@example.com" in a.user_agent
    ok = await a.normalize({"title": "D - Acme Fund LP (0001234567) (Filer)", "link": "https://sec/1", "id": "u1"})
    assert ok.event_type == "form_d" and ok.company_name == "Acme Fund LP"
    assert await a.normalize({"title": "D/A - Acme (0001) (Filer)", "link": "https://sec/2"}) is None
