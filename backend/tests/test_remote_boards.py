"""India / remote jobs: the geo rule, the Himalayas and Remotive adapters, and the job-alert gate."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.factory import build_adapter
from app.connectors.remote_boards import HimalayasAdapter, RemotiveAdapter
from app.connectors.runner import run_once
from app.services import registry
from app.services.geo import classify_geo, wanted
from app.worker import process_jobs


@pytest.mark.parametrize("loc,title,india,remote", [
    ("Bengaluru, India", "", True, False),
    ("Kolkata", "", True, False),
    ("Indiana", "", False, False),  # a word boundary, not a substring
    ("Remote", "", False, True),
    ("Remote (worldwide)", "", False, True),
    ("Remote, APAC", "", False, True),
    ("Remote - India", "", True, True),
    ("Remote, US or India", "", True, True),
    ("Remote (United States)", "", False, False),
    ("Remote, US", "", False, False),
    ("Europe (remote)", "", False, False),
    ("Remote (Worldwide except Canada)", "", False, False),
    ("Not remote - London", "", False, False),
    # real Databricks / Cloudflare locations seen on 2026-11-18: a named state or city limits the job, "Distributed; Hybrid" is not remote
    ("Los Angeles, California; Remote - California; San Francisco, California", "", False, False),
    ("Remote - California; Remote - New York", "", False, False),
    ("Distributed; Hybrid", "", False, False),
    ("Remote (Ukraine)", "", False, False),
    ("Remote, Anywhere", "", False, True),
    ("Fully remote - worldwide, full-time", "", False, True),
    ("San Francisco", "Engineer", False, False),
    ("", "Backend Engineer (Remote)", False, True),  # no location: the title counts
    ("", "Non-remote Engineer", False, False),
    ("Berlin", "Engineer (Remote)", False, False),  # a location was given: the title does not rescue it
    (None, None, False, False),
])
def test_classify_geo(loc, title, india, remote):
    assert classify_geo(loc, title) == (india, remote)
    assert wanted(loc, title) == (india or remote)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def hj(i, pub, restrictions=()):
    return {"title": f"Engineer {i}", "companyName": "Acme", "guid": f"https://himalayas.app/jobs/{i}", "applicationLink": f"https://himalayas.app/jobs/{i}",
            "locationRestrictions": list(restrictions), "pubDate": pub}


async def test_himalayas_keeps_only_jobs_open_to_india_and_dates_them_in_utc():
    a = HimalayasAdapter("Himalayas")
    worldwide = await a.normalize(hj(1, 1794995552))
    us = await a.normalize(hj(2, 1794995552, ["United States"]))
    india = await a.normalize(hj(3, 1794995552, ["India", "United States"]))
    assert worldwide.location == "Remote (worldwide)" and worldwide.event_type == "job" and worldwide.raw_metadata["remote"]
    assert worldwide.published_at == datetime.fromtimestamp(1794995552, UTC) and us is None
    assert india.location == "Remote (India, United States)"
    # live run 2026-11-18: a denylist kept "Remote (Ukraine)"; a country list must name India to count
    assert await a.normalize(hj(4, 1794995552, ["Ukraine"])) is None and await a.normalize(hj(5, 1794995552, ["Aruba", "Jordan"])) is None
    apac = await a.normalize(hj(6, 1794995552, ["Japan", "India", "Nepal", "Fiji"]))
    assert apac.location == "Remote (India and 3 other countries)"


async def test_himalayas_pages_until_it_reaches_jobs_it_has_seen():
    pages = {None: {"jobs": [hj(i, 1000 - i) for i in range(1, 4)], "nextCursor": "p2"},
             "p2": {"jobs": [hj(4, 990), hj(5, 50), hj(6, 40)], "nextCursor": "p3"},  # 50 and 40 are older than the cursor
             "p3": {"jobs": [hj(7, 30)], "nextCursor": None}}
    seen = []

    def handler(r):
        seen.append(r.url.params.get("cursor"))
        return httpx.Response(200, json=pages[r.url.params.get("cursor")])

    res = await HimalayasAdapter("H", client(handler)).fetch({"since": 100})
    assert [j["guid"][-1] for j in res.items] == ["1", "2", "3", "4"] and res.cursor == {"since": 999} and seen == [None, "p2"]
    first = await HimalayasAdapter("H", client(handler)).fetch(None)  # no cursor yet: walks every page
    assert len(first.items) == 7 and seen[2:] == [None, "p2", "p3"]


async def test_remotive_normalize_and_filter():
    a = RemotiveAdapter("Remotive")
    base = {"id": 7, "url": "https://remotive.com/remote-jobs/x", "title": "SRE", "company_name": "Acme", "publication_date": "2026-11-14T20:33:53"}
    ok = await a.normalize({**base, "candidate_required_location": "Worldwide"})
    blank = await a.normalize(base)
    us = await a.normalize({**base, "candidate_required_location": "USA, Canada, USA timezones"})
    assert ok.location == "Remote (Worldwide)" and ok.published_at == datetime(2026, 11, 14, 20, 33, 53, tzinfo=UTC)
    assert ok.canonical_url == "https://remotive.com/remote-jobs/x" and blank.location == "Remote (worldwide)" and us is None
    kept = {w: await a.normalize({**base, "candidate_required_location": w}) is not None
            for w in ("India", "APAC", "Asia", "Ukraine", "Europe", "Worldwide except Canada", "USA", "Americas, EMEA")}
    assert kept == {"India": True, "APAC": True, "Asia": True, "Ukraine": False, "Europe": False, "Worldwide except Canada": False, "USA": False,
                    "Americas, EMEA": False}  # allowlist: only India, blank or worldwide-style text counts
    got = await RemotiveAdapter("R", client(lambda r: httpx.Response(200, json={"jobs": [base]}))).fetch()
    assert got.items == [base]


def test_factory_builds_the_remote_boards():
    assert isinstance(build_adapter("Himalayas", {"kind": "himalayas"}), HimalayasAdapter)
    assert isinstance(build_adapter("Remotive", {"kind": "remotive"}), RemotiveAdapter)


class Fake(SourceAdapter):
    connector_type = "feed"

    def __init__(self, name, events):
        self.name, self.events = name, events

    async def fetch(self, cursor=None):
        return FetchResult(items=self.events, cursor={"n": len(self.events)})

    async def normalize(self, item):
        return item

    async def health_check(self):
        return True

    async def get_capabilities(self):
        return Capabilities()


async def test_job_alerts_only_for_india_or_remote_open_to_india(sm):
    async with sm() as s:
        await registry.register_source(s, "A", "feed")
        await registry.set_enabled(s, "A", "feed", True)
        await s.commit()
    now = datetime.now(UTC)

    def job(i, loc, **kw):
        return NormalizedEvent(source_name="A", source_event_id=i, canonical_url=f"https://e.com/{i}", event_type="job", title=f"Engineer {i}",
                               location=loc, published_at=now - timedelta(hours=1), **kw)

    await run_once(sm, Fake("A", [job("a", "Pune"), job("b", "Remote (worldwide)"), job("c", "Remote, US"), job("d", "London"),
                                  job("e", None, raw_metadata={"region": "india"})]))
    await process_jobs(sm)
    async with sm() as s:
        got = sorted(r.payload["title"] for r in (await s.execute(text("select payload from alert_deliveries"))).all())
        stored = (await s.execute(text("select count(*) from source_events where event_type = 'job'"))).scalar_one()
    assert got == ["Engineer a", "Engineer b", "Engineer e"] and stored == 5  # London and Remote, US are stored but never alerted
