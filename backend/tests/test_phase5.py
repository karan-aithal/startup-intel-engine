"""Phase 5: unknown company -> website with evidence -> ATS board registered -> jobs collected."""
import httpx
from sqlalchemy import text

from app.connectors.api import AtsAdapter
from app.connectors.runner import run_once
from app.services import ats, companies
from app.services.discovery import discover_pending
from app.services.pipeline import process_event


def test_extractors():
    assert companies.extract_company("Exclusive: Zypto Labs raises $5M in seed round led by Peak XV") == "Zypto Labs"
    assert companies.extract_company("startup news today raises") is None
    t = "Zypto raises $5M led by Peak XV and Accel, with participation from Sequoia Surge, Elevation."
    assert companies.extract_investors(t) == [("Peak XV", "lead"), ("Accel", "lead"), ("Sequoia Surge", "participant"), ("Elevation", "participant")]
    assert companies.extract_founders("said Asha Rao, co-founder of Zypto") == ["Asha Rao"]
    assert companies.canonical("Zypto Labs Pvt. Ltd.") == "zypto"


def test_find_boards():
    html = '<a href="https://boards.greenhouse.io/zypto">x</a><a href="https://jobs.lever.co/other">y</a><a href="https://jobs.ashbyhq.com/embed">z</a>'
    assert ats.find_boards(html) == [("greenhouse", "zypto"), ("lever", "other")]


HOME = '<html><head><title>Zypto | Payments</title></head><body><a href="/careers">Careers</a></body></html>'
CAREERS = '<a href="https://boards.greenhouse.io/zypto">Open roles</a>'
JOBS = {"jobs": [{"id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/zypto/jobs/1",
                  "location": {"name": "Bengaluru"}, "first_published": "2026-11-14T00:00:00Z"}]}


def web():
    def h(req: httpx.Request):
        u = str(req.url).rstrip("/")
        if u == "https://zypto.com":
            return httpx.Response(200, text=HOME)
        if u == "https://zypto.com/careers":
            return httpx.Response(200, text=CAREERS)
        if u == "https://boards-api.greenhouse.io/v1/boards/zypto/jobs":
            return httpx.Response(200, json=JOBS)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(h), follow_redirects=True)


async def test_unknown_company_to_jobs(sm):
    async with sm() as s:
        await s.execute(text("SELECT 1"))
        from app.services import registry
        sid = await registry.register_source(s, "News", "feed")
        eid = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content)
            VALUES (:s, 'e1', 'article', 'Zypto raises $5M in seed round led by Peak XV', 'Co-founder Asha Rao said hiring starts.') RETURNING id"""),
            {"s": sid})).scalar_one()
        await s.commit()
    async with sm() as s:
        assert await process_event(s, str(eid)) == "funding"
        await s.commit()
    c = web()
    assert await companies.resolve_pending(sm, client=c) == 1
    async with sm() as s:
        co = (await s.execute(text("SELECT id, website_url, hot_until IS NOT NULL AS hot FROM companies"))).one()
        assert co.website_url == "https://zypto.com" and co.hot
        ev = (await s.execute(text("SELECT evidence_type, snippet FROM evidence WHERE evidence_type='website_identity'"))).one()
        assert "title match" in ev.snippet
        assert (await s.execute(text("SELECT count(*) FROM company_investors"))).scalar_one() == 1
        assert (await s.execute(text("SELECT name FROM founders"))).scalar_one() == "Asha Rao"
    await discover_pending(sm, client=c)  # finds /careers
    assert await ats.detect_pending(sm, cap=200, client=c) == 1
    async with sm() as s:
        b = (await s.execute(text("SELECT status FROM ats_boards WHERE board_token='zypto'"))).scalar_one()
        r = (await s.execute(text("SELECT enabled, poll_interval_seconds FROM source_registry WHERE name='greenhouse:zypto'"))).one()
        assert (b, r.enabled, r.poll_interval_seconds) == ("active", True, 600)  # hot company polled faster
        assert (await s.execute(text("SELECT watched FROM companies"))).scalar_one()
    out = await run_once(sm, AtsAdapter("greenhouse", "zypto", c))
    assert out["new"] == 1
    async with sm() as s:
        assert (await s.execute(text("SELECT title FROM source_events WHERE event_type='job'"))).scalar_one() == "Backend Engineer"


async def test_cap_stops_auto_watch(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Zypto','zypto') RETURNING id"))).scalar_one()
        await s.commit()
    async with sm() as s:
        assert await ats.register_board(s, str(cid), "greenhouse", "zypto", "test", cap=0, client=web()) == "candidate"
        await s.commit()
    async with sm() as s:
        assert not (await s.execute(text("SELECT enabled FROM source_registry WHERE name='greenhouse:zypto'"))).scalar_one()
        assert (await s.execute(text("SELECT status FROM ats_boards"))).scalar_one() == "candidate"


async def test_identity_resolution_fuzzy(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Zypto Technologies','zypto') RETURNING id"))).scalar_one()
        await s.commit()
        assert await companies.match_company(s, "ZYPTO Pvt Ltd") == (str(cid), 100)
        assert (await companies.match_company(s, "Zyptoo"))[0] is None  # short names: a typo is not enough to merge
        rid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme Robotic Systems','acme robotic systems') RETURNING id"))).scalar_one()
        assert (await companies.match_company(s, "Acme Robotics Systems"))[0] == str(rid)
        assert (await companies.match_company(s, "Totally Different"))[0] is None
