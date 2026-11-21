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
    # J2 (decision 105): real Paytm / CRED / CEAT locations that name a state or small town and never the word India
    ("Ludhiana, Punjab", "", True, False), ("Patna, Bihar", "", True, False), ("tamil nadu", "", True, False), ("Nasik Plant", "", True, False),
    ("Aligarh, Uttar Pradesh", "", True, False), ("Remote - Karnataka", "", True, True), ("Salem, Oregon", "", False, False), ("Punjab", "", False, False),
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


async def test_himalayas_searches_india_and_worldwide_and_pages_until_it_reaches_seen_jobs():
    pages = {("in", "1"): [hj(i, 1000 - i, ["India"]) for i in range(1, 21)],
             ("in", "2"): [hj(21, 970, ["India"]), hj(22, 50, ["India"])] + [hj(i, 40, ["India"]) for i in range(23, 41)],  # 50 and 40 are older than the cursor
             ("ww", "1"): [hj(100, 5)] + [hj(i, 990 - i) for i in range(101, 120)],  # a pinned old job tops the list, fresh ones follow on the same page
             ("ww", "2"): [hj(i, 5) for i in range(120, 140)], ("in", "3"): [], ("ww", "3"): []}
    seen = []

    def handler(r):
        q = r.url.params
        key = ("in" if q.get("country") == "IN" else "ww", q["page"])
        assert r.url.path == "/jobs/api/search" and q["sort"] == "recent"
        assert (q.get("exclude_worldwide") == "true") == (key[0] == "in") and (q.get("worldwide") == "true") == (key[0] == "ww")
        seen.append(key)
        return httpx.Response(200, json={"jobs": pages[key]})

    res = await HimalayasAdapter("H", client(handler)).fetch({"since_in": 100, "since_ww": 100})
    assert [int(j["guid"].rsplit("/", 1)[1]) for j in res.items] == list(range(1, 22)) + list(range(101, 120))  # 22 and the 40s are older than since_in
    assert res.cursor == {"since_in": 999, "since_ww": 889} and seen == [("in", "1"), ("in", "2"), ("ww", "1"), ("ww", "2")]
    seen.clear()
    first = await HimalayasAdapter("H", client(handler)).fetch(None)  # no cursor yet: every page up to the cap
    assert len(first.items) == 40 + 40 and seen == [(k, str(n)) for k in ("in", "ww") for n in (1, 2, 3)]  # an empty page ends each query
    seen.clear()
    again = await HimalayasAdapter("H", client(handler)).fetch(res.cursor)  # nothing newer: the first page of each query only
    assert [int(j["guid"].rsplit("/", 1)[1]) for j in again.items] == [1, 101] and seen == [("in", "1"), ("ww", "1")] and again.cursor == res.cursor


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
