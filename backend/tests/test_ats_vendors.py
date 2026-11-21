"""J2: Keka, Darwinbox, TurboHire, Zoho Recruit and SmartRecruiters boards, workplace type, the small-board flag, the aggregator list and the fetch guard.
Fixtures are synthetic and shaped like each vendor's response; nothing here calls a live host."""
import asyncio
import html
import json
import socket
from datetime import UTC, datetime, timedelta

import httpcore
import httpx
import pytest
from sqlalchemy import text

from app.api import intel
from app.connectors.api import AtsAdapter, workplace
from app.connectors.ats_vendors import (
    VENDORS,
    BoardError,
    Darwinbox,
    Keka,
    SmartRecruiters,
    TurboHire,
    ZohoRecruit,
)
from app.connectors.runner import run_once
from app.services import ats, discovery, registry
from app.services.aggregators import is_aggregator
from app.services.geo import classify_geo
from app.services.safefetch import PinnedBackend, UnsafeUrl, check_url, guarded_client

NOW = datetime.now(UTC) - timedelta(hours=1)
ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
UUID = "11111111-2222-3333-4444-555555555555"
ORG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- one fake vendor world: every board lists an India job and a London job ----

def world(request: httpx.Request) -> httpx.Response:
    h, p = request.url.host, request.url.path
    if h == "acme.keka.com" and p == "/careers":
        return httpx.Response(200, text=f'<script src="/ats/documents/{UUID}/careerportal/x1.html"></script>')
    if h == "acme.keka.com" and p == f"/careers/api/embedjobs/default/active/{UUID}":
        return httpx.Response(200, json=[
            {"id": 1, "title": "Backend Engineer", "jobLocations": [{"city": "Bengaluru", "countryName": "India"}], "publishedOn": ISO},
            {"id": 2, "title": "Designer", "jobLocations": [{"city": "London", "countryName": "United Kingdom"}], "publishedOn": ISO}])
    if h == "acme.darwinbox.in" and p == "/ms/candidateapi/job/alljobs":
        assert request.method == "POST" and json.loads(request.content)["companyId"] == "AB12cd"
        return httpx.Response(200, json={"status": "success", "job_counts": 2, "data": [
            {"id": "J1", "title": "Backend Engineer", "tool_tip_locations": ["Pune, Maharashtra, India"], "posted_on": int(NOW.timestamp())},
            {"id": "J2", "title": "Designer", "locations": "London, UK", "posted_on": int(NOW.timestamp())}]})
    if h == "thapi.azurewebsites.net" and p == "/api/token/noauth":
        assert request.headers["origin"] == "https://acme.turbohire.co" and request.headers["referer"] == "https://acme.turbohire.co/"
        return httpx.Response(200, json={"access_token": "tok"})
    if h == "thapi.azurewebsites.net" and p == "/api/publicorganizations":
        assert request.headers["authorization"] == "Bearer tok" and request.url.params["accountName"] == "acme"
        return httpx.Response(200, json={"OrgID": ORG})
    if h == "thapi.azurewebsites.net" and p == "/api/careerpagev2/filteredjobs":
        assert request.url.params["orgId"] == ORG and request.url.params["pageType"] == "0"
        return httpx.Response(200, json={"Result": [
            {"JobTitle": "Backend Engineer", "JobIdObfuscated": "ab%2Fcd", "Location": json.dumps([{"Address": "Hyderabad, India"}]), "PublishedDate": ISO},
            {"JobTitle": "Designer", "JobIdObfuscated": "ef12", "Location": json.dumps([{"Address": "London, UK"}]), "PublishedDate": ISO}]})
    if h == "acme.zohorecruit.in" and p == "/jobs/Careers":
        jobs = [{"id": 11, "Posting_Title": "Backend Engineer", "City": "Chennai", "Country": "India", "Date_Opened": ISO},
                {"id": 12, "Posting_Title": "Designer", "City": "London", "Country": "United Kingdom", "Date_Opened": ISO},
                {"id": 13, "Posting_Title": "Filled role", "City": "Pune", "Is_Locked": True}]
        return httpx.Response(200, text=f'<html><input type="hidden" id="jobs" value="{html.escape(json.dumps(jobs))}"></html>')
    if h == "api.smartrecruiters.com" and p == "/v1/companies/AcmeCo/postings":
        return httpx.Response(200, json={"totalFound": 2, "content": [
            {"id": "743", "name": "Backend Engineer", "location": {"city": "Mumbai", "country": "in"}, "releasedDate": ISO},
            {"id": "744", "name": "Designer", "location": {"fullLocation": "London, United Kingdom"}, "releasedDate": ISO}]})
    return httpx.Response(404)


CAREERS = {  # a careers page that links to each vendor's board
    "keka": ("acme", '<a href="https://acme.keka.com/careers">Jobs</a>'),
    "darwinbox": ("acme.darwinbox.in/AB12cd", '<a href="https://acme.darwinbox.in/ms/candidatev2/AB12cd/careers/allJobs">Jobs</a>'),
    "turbohire": ("acme", '<a href="https://acme.turbohire.co/">Jobs</a>'),
    "zoho_recruit": ("acme.zohorecruit.in", '<a href="https://acme.zohorecruit.in/jobs/Careers">Jobs</a>'),
    "smartrecruiters": ("AcmeCo", '<a href="https://careers.smartrecruiters.com/AcmeCo">Jobs</a>'),
}


@pytest.mark.parametrize("provider", list(CAREERS))
def test_board_is_detected_from_a_careers_page(provider):
    token, page = CAREERS[provider]
    assert (provider, token) in ats.find_boards(page)


@pytest.mark.parametrize("provider", list(CAREERS))
async def test_detected_registered_polled_and_listed_in_explore(sm, monkeypatch, provider):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    token, page = CAREERS[provider]

    def handler(r):
        return httpx.Response(200, text=f"<html>{page}</html>") if r.url.host == "acme.example" else world(r)

    c = client(handler)
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name, website_url, is_india_linked) VALUES ('Acme','acme','https://acme.example', true) "
                                    "RETURNING id"))).scalar_one()
        got = await ats.detect_for_company(s, str(cid), c, 200, 300)
        await s.commit()
    assert got == [f"{provider}:{token}=registered"]  # verified, then promoted under both caps
    async with sm() as s:
        row = (await s.execute(text("SELECT status, board_url, last_job_count, evidence FROM ats_boards"))).one()
        reg = (await s.execute(text("SELECT enabled, poll_interval_seconds FROM source_registry WHERE name = :n"), {"n": f"{provider}:{token}"})).one()
    assert row.status == "active" and row.last_job_count == 2  # Zoho: the filled third posting is skipped
    assert reg.enabled and reg.poll_interval_seconds == VENDORS[provider].poll
    assert row.evidence["small_board"] is True  # two jobs: a demo tenant or a regional board, flagged but still polled
    out = await run_once(sm, AtsAdapter(provider, token, c))
    assert out["status"] == "ok" and out["new"] == 2
    d = await intel.explore("all", "", "", "", "", 7, 50, 0)
    assert [j["title"] for j in d["items"]] == ["Backend Engineer"] and d["items"][0]["india"]  # the London job is stored, not listed


# ---- per-vendor parsing and failure modes ----

async def test_keka_unknown_tenant_and_changed_page_fail_loudly():
    def redirect(r):
        return httpx.Response(302, headers={"location": "/careers/Content/TenantNotFound.html"})

    with pytest.raises(BoardError, match="redirected"):
        await Keka().fetch(client(redirect), "ghost")
    with pytest.raises(BoardError, match="identifier"):
        await Keka().fetch(client(lambda r: httpx.Response(200, text="<html>no portal</html>")), "acme")
    with pytest.raises(BoardError, match="array"):
        Keka.parse({"jobs": []}, "https://acme.keka.com")
    assert Keka.parse([], "https://acme.keka.com") == []  # an empty board is not an error


def test_keka_parse_location_and_url():
    rows = Keka.parse([{"id": 5, "title": " Data  Engineer ", "jobLocations": [{"city": "Pune", "countryName": "India"}, {"name": "HQ - Tower 2"},
                                                                                {"city": "Pune", "countryName": "India"}], "publishedOn": "2026-11-10T08:00:00Z"},
                       {"id": None, "title": "no id"}, {"id": 6, "title": ""}], "https://acme.keka.com")
    assert rows == [{"id": "5", "title": "Data Engineer", "url": "https://acme.keka.com/careers/jobdetails/5", "loc": "Pune, India; HQ - Tower 2",
                     "pub": datetime(2026, 11, 10, 8, tzinfo=UTC), "team": None}]


async def test_darwinbox_pages_until_a_short_page_and_reports_a_partial_walk(monkeypatch):
    monkeypatch.setattr("app.connectors.ats_vendors.PAGE_PAUSE", 0)
    calls, fail_third = [], False

    def handler(r):
        page = json.loads(r.content)["page"]
        calls.append(page)
        if page == 3 and fail_third:
            return httpx.Response(500)
        n = 50 if page < 3 else 7
        return httpx.Response(200, json={"status": "success", "data": [{"id": f"J{page}-{i}", "title": "Role", "locations": "Pune"} for i in range(n)]})

    done = await Darwinbox().fetch(client(handler), "acme.darwinbox.in/AB12cd")
    assert len(done.rows) == 107 and done.complete and calls == [1, 2, 3]
    fail_third = True
    cut = await Darwinbox().fetch(client(handler), "acme.darwinbox.in/AB12cd")
    assert len(cut.rows) == 100 and not cut.complete  # kept what was read, but a missing job is not proven closed
    with pytest.raises(BoardError):
        await Darwinbox().fetch(client(lambda r: httpx.Response(200, json={"status": "error"})), "acme.darwinbox.in/AB12cd")


def test_turbohire_zoho_and_smartrecruiters_parse():
    th = TurboHire.parse({"Result": [{"JobTitle": "SRE", "JobIdObfuscated": "a%2Fb", "Location": "Remote, India", "PublishedDate": "2026-11-10T08:00:00"}]},
                         "https://acme.turbohire.co")
    assert th[0]["url"] == "https://acme.turbohire.co/job/publicjobs/a%2Fb" and th[0]["loc"] == "Remote, India" and th[0]["pub"].tzinfo
    with pytest.raises(BoardError):
        TurboHire.parse({"Items": []}, "https://acme.turbohire.co")
    value = html.escape(json.dumps([{"id": 1, "Posting_Title": "Dev", "City": "Pune", "Remote_Job": True}, {"id": 2, "Publish": False, "Posting_Title": "Gone"}]))
    z = ZohoRecruit.parse(f'<input id="jobs" value="{value}">', "https://acme.zohorecruit.com")
    assert [(r["title"], r["loc"]) for r in z] == [("Dev", "Pune, Remote")]
    assert ZohoRecruit.parse('<input id="jobs" value="[]">', "https://x.zohorecruit.com") == []
    with pytest.raises(BoardError, match="no id"):
        ZohoRecruit.parse("<html>Page does not exist</html>", "https://x.zohorecruit.com")  # an unknown tenant is not an empty board
    sr = SmartRecruiters.parse([{"id": "1", "name": "Staff Engineer (Remote)", "location": {"remote": True}, "department": {"label": "Platform"}}], "AcmeCo")
    assert sr[0]["url"] == "https://jobs.smartrecruiters.com/AcmeCo/1-staff-engineer-remote" and sr[0]["loc"] == "Remote" and sr[0]["team"] == "Platform"
    assert classify_geo(sr[0]["loc"], sr[0]["title"]) == (False, True)


async def test_smartrecruiters_pages_and_marks_a_page_cap_partial(monkeypatch):
    monkeypatch.setattr("app.connectors.ats_vendors.PAGE_PAUSE", 0)
    v = SmartRecruiters()
    v.MAX_PAGES = 2
    got = await v.fetch(client(lambda r: httpx.Response(200, json={"content": [{"id": f"{r.url.params['offset']}-{i}", "name": "Role"} for i in range(100)]})), "AcmeCo")
    assert len(got.rows) == 200 and not got.complete  # the cap ended the walk, not the board


@pytest.mark.parametrize("provider,token", [("keka", "evil.com/x"), ("keka", "www"), ("darwinbox", "acme.darwinbox.in"), ("darwinbox", "evil.com/AB"),
                                            ("turbohire", "a.b"), ("zoho_recruit", "acme.example.com"), ("smartrecruiters", "../x")])
def test_a_token_is_never_allowed_to_steer_the_host(provider, token):
    with pytest.raises(ValueError):
        AtsAdapter(provider, token)


# ---- a partial listing never closes jobs ----

async def test_partial_listing_does_not_close_jobs_but_a_complete_one_does(sm, monkeypatch):
    monkeypatch.setattr("app.connectors.ats_vendors.PAGE_PAUSE", 0)
    tok = "acme.darwinbox.in/AB12cd"
    two = [{"id": "J1", "title": "A", "locations": "Pune"}, {"id": "J2", "title": "B", "locations": "Pune"}]
    state = {"data": two, "fail": False}

    def handler(r):
        if state["fail"] and json.loads(r.content)["page"] == 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"status": "success", "data": state["data"]})

    async with sm() as s:
        await registry.register_source(s, f"darwinbox:{tok}", "api")
        await registry.set_enabled(s, f"darwinbox:{tok}", "api", True)
        await s.commit()
    assert (await run_once(sm, AtsAdapter("darwinbox", tok, client(handler))))["new"] == 2
    state.update(data=[{"id": f"P{i}", "title": f"P{i}", "locations": "Pune"} for i in range(50)], fail=True)  # page 1 full, page 2 fails: partial
    await run_once(sm, AtsAdapter("darwinbox", tok, client(handler)))
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM job_postings WHERE closed_at IS NOT NULL"))).scalar_one() == 0
    state.update(data=two[:1], fail=False)  # a complete listing without J2
    await run_once(sm, AtsAdapter("darwinbox", tok, client(handler)))
    async with sm() as s:
        closed = (await s.execute(text("SELECT external_job_id FROM job_postings WHERE closed_at IS NOT NULL"))).scalars().all()
    assert "J2" in closed and "J1" not in closed


# ---- small board ----

async def test_small_board_is_flagged_not_rejected_and_the_flag_follows_the_job_count(sm):
    def many(n):
        return lambda r: httpx.Response(200, json=[{"id": i, "title": f"R{i}", "jobLocations": []} for i in range(n)]) if "embedjobs" in r.url.path else \
            httpx.Response(200, text=f"<script>/ats/documents/{UUID}/careerportal/x.html</script>")

    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme','acme') RETURNING id"))).scalar_one()
        assert await ats.register_board(s, str(cid), "keka", "five", "t", 200, 300, client(many(5))) == "registered"
        assert await ats.register_board(s, str(cid), "keka", "six", "t", 200, 300, client(many(6))) == "registered"
        await s.commit()
    async with sm() as s:
        flags = dict((await s.execute(text("SELECT board_token, (evidence->>'small_board')::boolean FROM ats_boards"))).all())
        assert flags == {"five": True, "six": False}
        await s.execute(text("UPDATE ats_boards SET last_job_count = 9 WHERE board_token = 'five'"))
        await s.execute(text("UPDATE ats_boards SET last_job_count = 2 WHERE board_token = 'six'"))
        await ats.demote_stale(s)
        flags = dict((await s.execute(text("SELECT board_token, (evidence->>'small_board')::boolean FROM ats_boards"))).all())
    assert flags == {"five": False, "six": True}


# ---- workplace type ----

def test_workplace_field_and_its_effect_on_geo():
    assert [workplace(v) for v in ("remote", "Remote", "On-site", "OnSite", "Hybrid", "unspecified", None)] == ["remote", "remote", "onsite", "onsite", "hybrid", None, None]
    assert classify_geo("Remote", None, "hybrid") == (False, False)  # hybrid is office-anchored, not remote
    assert classify_geo("Remote", None, "onsite") == (False, False)
    assert classify_geo("", "Engineer", "remote") == (False, True)  # no location and no word in the title: the field says remote
    assert classify_geo("Bengaluru", None, "remote") == (True, True)
    assert classify_geo("Remote - California", None, "remote") == (False, False)  # the field never widens a named place
    assert classify_geo("London", None, "remote") == (False, False)
    assert classify_geo("Remote", None) == (False, True)  # no field: the old rule


async def test_lever_and_ashby_keep_the_workplace_type():
    lever = await AtsAdapter("lever", "x").normalize({"id": "1", "text": "SRE", "hostedUrl": "https://l/1", "categories": {"location": "Remote"}, "workplaceType": "hybrid"})
    ashby = await AtsAdapter("ashby", "x").normalize({"id": "2", "title": "SRE", "jobUrl": "https://a/2", "location": "Remote", "workplaceType": "OnSite"})
    plain = await AtsAdapter("ashby", "x").normalize({"id": "3", "title": "SRE", "jobUrl": "https://a/3", "location": "Remote"})
    assert lever.raw_metadata["workplace"] == "hybrid" and ashby.raw_metadata["workplace"] == "onsite" and "workplace" not in plain.raw_metadata


async def test_explore_hides_a_remote_labelled_hybrid_job(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    async with sm() as s:
        sid = await registry.register_source(s, "lever:x", "api")
        for i, wp in enumerate(("hybrid", "remote", None)):
            await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, raw_metadata, published_at, detected_at)
                VALUES (:s, :i, 'job', :t, 'Remote', CAST(:m AS jsonb), now(), now())"""),
                {"s": sid, "i": str(i), "t": f"Role {wp}", "m": json.dumps({"workplace": wp} if wp else {})})
        await s.commit()
    d = await intel.explore("all", "", "", "", "", 7, 50, 0)
    assert sorted(j["title"] for j in d["items"]) == ["Role None", "Role remote"]


# ---- aggregators ----

def test_aggregator_list():
    assert is_aggregator("https://www.zippia.com/jobs") and is_aggregator("https://jobs.zippia.com/x") and is_aggregator("jooble.org")
    assert not is_aggregator("https://notzippia.com") and not is_aggregator("https://indeed.com") and not is_aggregator("https://acme.com") and not is_aggregator(None)


async def test_an_aggregator_is_never_treated_as_a_company_site(sm):
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200, text='<a href="https://boards.greenhouse.io/zypto">x</a>')

    c = client(handler)
    assert await discovery.find_candidates("https://www.jooble.org/jobs-acme", c) == [] and seen == []
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name, website_url, careers_url) "
                                    "VALUES ('Acme','acme','https://www.adzuna.com/acme','https://jooble.org/x') RETURNING id"))).scalar_one()
        assert await ats.detect_for_company(s, str(cid), c, 200, 300) == [] and seen == []
    assert not any(d["kind"] == "careers" for d in discovery.classify_links("https://www.zippia.com", '<a href="/jobs">jobs</a>'))


# ---- the fetch guard ----

@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/api/x", "https://169.254.169.254/latest/meta-data", "http://localhost/x", "https://[::1]/x", "http://10.0.0.5/",
                                 "http://192.168.1.1/", "https://[::ffff:127.0.0.1]/", "ftp://example.com/x", "file:///etc/passwd", "https://user:pw@example.com/",
                                 "http://foo.internal/", "http://100.64.0.1/", "http://0.0.0.0/"])
async def test_the_guard_refuses_private_and_odd_urls(url):
    with pytest.raises(UnsafeUrl):
        await check_url(url)


async def test_the_guard_checks_every_redirect_hop_and_is_a_normal_http_error():
    hops = []

    def handler(r):
        hops.append(str(r.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}) if r.url.host == "93.184.216.34" else httpx.Response(200)

    c = guarded_client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(httpx.HTTPError):  # callers already catch httpx.HTTPError
        await c.get("https://93.184.216.34/")
    assert hops == ["https://93.184.216.34/"]  # the metadata address was never requested


async def test_the_guard_refuses_a_name_that_resolves_to_a_private_address(monkeypatch):
    async def fake(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake, raising=False)
    with pytest.raises(UnsafeUrl, match="non-public"):
        await check_url("https://careers.evil.example/")


async def test_explore_lists_a_job_that_names_a_state_but_not_india(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    async with sm() as s:
        sid = await registry.register_source(s, "lever:paytm", "api")
        for i, loc in enumerate(("Patna, Bihar", "Salem, Oregon")):
            await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, published_at, detected_at)
                VALUES (:s, :i, 'job', :t, :l, now(), now())"""), {"s": sid, "i": str(i), "t": f"Role {i}", "l": loc})
        await s.commit()
    d = await intel.explore("all", "", "", "", "", 7, 50, 0)
    assert [j["location"] for j in d["items"]] == ["Patna, Bihar"] and d["items"][0]["india"]


class FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.connected = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        return "stream"

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("not reached")

    async def sleep(self, seconds):
        return None


async def test_the_connection_is_pinned_to_the_address_that_was_checked(monkeypatch):
    """DNS rebinding: the name answers public to the first lookup and private to a second one. The connect must use the first, never look up again."""
    answers = iter(["93.184.216.34", "10.0.0.9"])  # one lookup per connect: the backend resolves once, checks, and dials that address

    async def fake(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake, raising=False)
    inner = FakeInner()
    backend = PinnedBackend(inner)
    assert await backend.connect_tcp("rebind.example", 443) == "stream"
    assert inner.connected == ["93.184.216.34"]  # dialled by address, not by name
    with pytest.raises(UnsafeUrl):  # the next lookup says private: refused before any dial
        await backend.connect_tcp("rebind.example", 443)
    assert inner.connected == ["93.184.216.34"]
    with pytest.raises(UnsafeUrl):
        await backend.connect_unix_socket("/var/run/docker.sock")


def test_the_guarded_client_pins_the_connection():
    c = guarded_client()
    assert isinstance(c._transport._pool._network_backend, PinnedBackend)  # httpx/httpcore internals: this fails loudly if an upgrade moves them
    mock = guarded_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert isinstance(mock._transport, httpx.MockTransport)
