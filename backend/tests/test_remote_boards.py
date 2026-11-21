"""India / remote jobs: the geo rule, the Himalayas and Remotive adapters, and the job-alert gate."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.api import intel
from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.factory import build_adapter
from app.connectors.remote_boards import (
    HimalayasAdapter,
    JobicyAdapter,
    RemotiveAdapter,
    WeWorkRemotelyAdapter,
    WorkingNomadsAdapter,
)
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


# ---- J3: Jobicy, We Work Remotely, Working Nomads (decision 106) ----

def jb(i, geo, ts, title="Engineer", company="Acme &amp; Co"):
    return {"id": str(i), "url": f"https://jobicy.com/jobs/{i}-engineer", "jobTitle": f"{title} {i}", "companyName": company, "jobGeo": geo, "pubDate": ts}


T = [f"2026-11-20T{h:02d}:00:00+00:00" for h in range(24)]  # T[h] = that hour on the 20th, later hour = newer


async def test_jobicy_filters_by_geo_follows_the_cursor_and_stops_at_seen_jobs(monkeypatch):
    monkeypatch.setattr("app.connectors.remote_boards.JOBICY_PAUSE", 0)
    pages = {("anywhere", None): {"jobs": [jb(1, "Anywhere", T[20]), jb(2, "USA", T[19])], "hasMore": True, "nextCursor": "c1"},
             ("anywhere", "c1"): {"jobs": [jb(3, "Anywhere", T[10]), jb(4, "Anywhere", T[9])], "hasMore": False, "nextCursor": None},
             ("apac", None): {"jobs": [jb(5, "APAC", T[18]), jb(6, "India,  USA", T[17])], "hasMore": False, "nextCursor": None}}
    seen = []

    def handler(r):
        q = r.url.params
        key = (q["geo"], q.get("cursor"))
        assert r.url.path == "/api/v2/remote-jobs" and q["count"] == "100"
        seen.append(key)
        return httpx.Response(200, json=pages[key])

    first = await JobicyAdapter("Jobicy", client(handler)).fetch(None)
    assert [j["id"] for j in first.items] == ["1", "2", "3", "4", "5", "6"] and seen == [("anywhere", None), ("anywhere", "c1"), ("apac", None)]
    assert first.cursor == {"since_anywhere": int(datetime.fromisoformat(T[20]).timestamp()), "since_apac": int(datetime.fromisoformat(T[18]).timestamp())}
    seen.clear()
    again = await JobicyAdapter("Jobicy", client(handler)).fetch(first.cursor)  # newest seen is 20:00; page 1 of each query ends below it (19:00, 17:00), so no second page
    assert seen == [("anywhere", None), ("apac", None)] and [j["id"] for j in again.items] == ["1", "5"]


async def test_jobicy_keeps_only_jobs_open_to_india_and_decodes_entities():
    a = JobicyAdapter("Jobicy")
    kept = {g: await a.normalize(jb(1, g, T[5])) is not None for g in ("Anywhere", "APAC", "India,  USA", "USA", "Canada,  USA", "Europe", "Germany", "Anywhere except USA")}
    assert kept == {"Anywhere": True, "APAC": True, "India,  USA": True, "USA": False, "Canada,  USA": False, "Europe": False, "Germany": False, "Anywhere except USA": False}
    e = await a.normalize(jb(7, "India,  USA", T[5]))
    assert e.company_name == "Acme & Co" and e.location == "Remote (India, USA)" and e.event_type == "job" and e.source_name == "Jobicy"
    assert e.canonical_url == "https://jobicy.com/jobs/7-engineer" and e.published_at == datetime(2026, 11, 20, 5, tzinfo=UTC)  # the link goes to jobicy.com


WWR = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>WWR</title>
{items}</channel></rss>"""


def wi(slug, title, region, country="", when="Thu, 19 Nov 2026 17:03:31 +0000"):
    return (f"<item><title>{title}</title><region>{region}</region><country>{country}</country><category>Programming</category><pubDate>{when}</pubDate>"
            f"<guid>https://weworkremotely.com/remote-jobs/{slug}</guid><link>https://weworkremotely.com/remote-jobs/{slug}</link></item>")


async def test_weworkremotely_reads_region_and_country_and_splits_the_company():
    feed = WWR.format(items="".join([
        wi("a", "Acme: Backend Engineer", "Anywhere in the World"),
        wi("b", "Beta: Designer", "USA Only", "\U0001F1FA\U0001F1F8 United States of America"),
        wi("c", "Gamma: SRE", "Asia Only"),
        wi("d", "Delta: Analyst", "Anywhere in the World", "\U0001F1E8\U0001F1F4 Colombia"),  # region says anywhere, the country list is stricter
        wi("e", "Eps: Data Eng", "Anywhere in the World", "\U0001F1EE\U0001F1F3 India, \U0001F1F3\U0001F1F1 Netherlands"),
        wi("f", "No company here", "Anywhere in the World")]))
    a = WeWorkRemotelyAdapter("We Work Remotely", client=client(lambda r: httpx.Response(200, content=feed.encode())))
    items = (await a.fetch()).items
    events = [e for e in [await a.normalize(i) for i in items] if e]
    assert [e.source_event_id.rsplit("/", 1)[1] for e in events] == ["a", "c", "e", "f"]  # USA only and Colombia-only are dropped
    by = {e.source_event_id.rsplit("/", 1)[1]: e for e in events}
    assert by["a"].title == "Backend Engineer" and by["a"].company_name == "Acme" and by["a"].location == "Remote (Anywhere in the World)"
    assert by["e"].location == "Remote (India, Netherlands)" and by["f"].title == "No company here" and by["f"].company_name is None
    assert by["a"].published_at == datetime(2026, 11, 19, 17, 3, 31, tzinfo=UTC) and by["a"].canonical_url.startswith("https://weworkremotely.com/")


async def test_workingnomads_keeps_only_jobs_open_to_india():
    a = WorkingNomadsAdapter("Working Nomads")

    def row(i, loc):
        return {"url": f"https://www.workingnomads.com/job/go/{i}", "title": f"Role {i}", "company_name": "TELUS", "location": loc, "pub_date": "2026-11-19T07:13:33-05:00"}

    kept = {loc: await a.normalize(row(1, loc)) is not None for loc in (
        "Global", "Anywhere in the world", "Europe, LATAM, APAC, the U.S., Canada", "", "USA only", "USA, Canada, Ireland and UK ONLY",
        "Time zone: CET (+/- 3 hours)", "Japan - Remote", "Worldwide except USA")}
    assert kept == {"Global": True, "Anywhere in the world": True, "Europe, LATAM, APAC, the U.S., Canada": True, "": True, "USA only": False,
                    "USA, Canada, Ireland and UK ONLY": False, "Time zone: CET (+/- 3 hours)": False, "Japan - Remote": False, "Worldwide except USA": False}
    e = await a.normalize(row(2, "Global"))
    assert e.published_at == datetime(2026, 11, 19, 12, 13, 33, tzinfo=UTC) and e.canonical_url.endswith("/job/go/2") and e.location == "Remote (Global)"
    with pytest.raises(ValueError):
        await WorkingNomadsAdapter("x", client(lambda r: httpx.Response(200, json={"jobs": []}))).fetch()


def test_factory_builds_the_j3_boards():
    assert isinstance(build_adapter("Jobicy", {"kind": "jobicy"}), JobicyAdapter)
    assert isinstance(build_adapter("Working Nomads", {"kind": "workingnomads"}), WorkingNomadsAdapter)
    assert isinstance(build_adapter("We Work Remotely", {"kind": "weworkremotely"}), WeWorkRemotelyAdapter)


async def test_each_j3_board_stores_only_eligible_jobs_and_shows_in_source_health(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr("app.connectors.remote_boards.JOBICY_PAUSE", 0)
    now = datetime.now(UTC)
    jobicy = lambda r: httpx.Response(200, json={"jobs": [jb(1, "Anywhere", now.isoformat()), jb(2, "USA", now.isoformat())] if r.url.params["geo"] == "anywhere" else [],
                                                  "hasMore": False})
    nomads = lambda r: httpx.Response(200, json=[{"url": "https://www.workingnomads.com/job/go/1", "title": "Open", "company_name": "A", "location": "Global",
                                                   "pub_date": now.isoformat()},
                                                  {"url": "https://www.workingnomads.com/job/go/2", "title": "Closed", "company_name": "B", "location": "USA only",
                                                   "pub_date": now.isoformat()}])
    wwr = lambda r: httpx.Response(200, content=WWR.format(items=wi("a", "Acme: Open", "Anywhere in the World") + wi("b", "Beta: Closed", "USA Only")).encode())
    boards = [("Jobicy", "api", JobicyAdapter("Jobicy", client(jobicy))), ("Working Nomads", "api", WorkingNomadsAdapter("Working Nomads", client(nomads))),
              ("We Work Remotely", "feed", WeWorkRemotelyAdapter("We Work Remotely", client=client(wwr)))]
    async with sm() as s:
        for name, ctype, _ in boards:
            await registry.register_source(s, name, ctype)
            await registry.set_enabled(s, name, ctype, True)
        await s.commit()
    for _, _, adapter in boards:
        out = await run_once(sm, adapter)
        assert out["status"] == "ok" and out["new"] == 1  # the restricted-country job is dropped before storage
    async with sm() as s:
        titles = sorted(r[0] for r in (await s.execute(text("select title from source_events where event_type = 'job'"))).all())
    assert titles == ["Engineer 1", "Open", "Open"]
    health = {r["name"]: r for r in await intel.source_health()}
    assert {"Jobicy", "Working Nomads", "We Work Remotely"} <= set(health) and all(health[n]["enabled"] and health[n]["events_total"] == 1 for n in ("Jobicy", "Working Nomads", "We Work Remotely"))
