"""J4 Portals: board states, /api/portals, enable/disable under the caps, re-check through the queue, add-from-URL, and the write guard on every write route."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app import worker
from app.api import portals as portals_api
from app.api.guard import local_write
from app.connectors.api import AtsAdapter
from app.core.config import get_settings
from app.main import app
from app.services import ats, portals, registry
from app.services.portals import PortalError, board_state
from app.services.scheduler import DUE_SQL

NOW = datetime.now(UTC)
OK = "http://127.0.0.1:3010"


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- the state rule (owner's thresholds, 2026-11-19) ----

@pytest.mark.parametrize("kw,state", [
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": NOW, "jobs": 12}, "live"),
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": NOW, "jobs": 0}, "empty"),
    ({"enabled": True, "status": "active", "failures": 3, "circuit_open_until": None, "last_success": NOW, "jobs": 12}, "broken"),  # 3 failures in a row
    ({"enabled": True, "status": "active", "failures": 2, "circuit_open_until": None, "last_success": NOW, "jobs": 12}, "live"),  # 2 is not enough
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": NOW + timedelta(hours=1), "last_success": NOW, "jobs": 12}, "broken"),
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": NOW - timedelta(hours=1), "last_success": NOW, "jobs": 12}, "live"),  # circuit closed again
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": NOW - timedelta(days=4), "jobs": 12}, "broken"),  # stale: > 3 days
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": NOW - timedelta(days=2), "jobs": 12}, "live"),
    ({"enabled": True, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": None, "jobs": None}, "pending"),  # never polled successfully
    ({"enabled": False, "status": "active", "failures": 0, "circuit_open_until": None, "last_success": NOW, "jobs": 12}, "disabled"),
    ({"enabled": True, "status": "paused", "failures": 0, "circuit_open_until": None, "last_success": NOW, "jobs": 12}, "disabled"),
    ({"enabled": False, "status": "candidate", "failures": 0, "circuit_open_until": None, "last_success": None, "jobs": 5}, "candidate"),
    ({"enabled": True, "status": None, "failures": 0, "circuit_open_until": None, "last_success": NOW, "jobs": 0}, "empty"),  # a seeded board has no ats_boards row
])
def test_board_state(kw, state):
    assert board_state(**kw) == state


# ---- seeded data: a live board, an empty board, broken boards, a seeded board, a watched company with no board ----

async def company(s, name, watched=True):
    return (await s.execute(text("INSERT INTO companies (name, canonical_name, watched) VALUES (:n, :c, :w) RETURNING id"),
                            {"n": name, "c": name.lower(), "w": watched})).scalar_one()


async def board(s, cid, token, *, provider="greenhouse", status="active", count=10, enabled=True, failures=0, success=NOW, circuit=None, small=None,
                jobs=(), verification="verified"):
    name = f"{provider}:{token}"
    rid = await registry.register_source(s, name, "api", config={"kind": "ats", "provider": provider, "token": token}, poll_interval_seconds=1800)
    if enabled:
        await registry.set_enabled(s, name, "api", True)
    await s.execute(text("UPDATE source_registry SET consecutive_failures = :f, last_success_at = :ls, circuit_open_until = :c WHERE id = CAST(:i AS uuid)"),
                    {"f": failures, "ls": success, "c": circuit, "i": rid})
    if cid is not None:
        await s.execute(text("""INSERT INTO ats_boards (company_id, provider, board_token, board_url, status, last_job_count, last_success_at, verification_status,
            evidence) VALUES (:c, :p, :t, :u, :st, :n, :ls, :v, CAST(:e AS jsonb))"""),
            {"c": cid, "p": provider, "t": token, "u": f"https://boards.greenhouse.io/{token}", "st": status, "n": count, "ls": success, "v": verification,
             "e": '{"small_board": true}' if small else "{}"})
    for i, (title, loc) in enumerate(jobs):
        await s.execute(text("INSERT INTO job_postings (source_id, external_job_id, title, location, status) VALUES (CAST(:s AS uuid), :e, :t, :l, 'open')"),
                        {"s": rid, "e": f"{token}-{i}", "t": title, "l": loc})
    return rid


@pytest.fixture
async def seeded(sm):
    async with sm() as s:
        a, b, c, d, e = [await company(s, n) for n in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")]
        await company(s, "Zeta")  # watched, no board at all
        await company(s, "Eta", watched=False)  # not watched: not listed under "no ATS"
        ids = {
            "live": await board(s, a, "alpha", small=False, jobs=[("Backend Engineer", "Bengaluru, India"), ("Designer", "London"), ("SRE", "Remote")]),
            "small": await board(s, a, "alpha-small", count=3, small=True),
            "empty": await board(s, b, "beta", count=0),
            "failing": await board(s, c, "gamma", failures=3),
            "circuit": await board(s, d, "delta", circuit=NOW + timedelta(hours=2)),
            "stale": await board(s, e, "epsilon", success=NOW - timedelta(days=5)),
            "seeded": await board(s, None, "stripe", jobs=[("Engineer", "Pune"), ("Engineer", "Pune"), ("Analyst", "New York")]),
            "paused": await board(s, a, "alpha-paused", status="paused", enabled=False),
        }
        for kind, name, on in (("himalayas", "Himalayas", True), ("jooble", "Jooble India", False)):
            ids[kind] = await registry.register_source(s, name, "api", config={"kind": kind}, poll_interval_seconds=21600)
            if on:
                await registry.set_enabled(s, name, "api", True)
                await s.execute(text("UPDATE source_registry SET last_success_at = now() WHERE id = CAST(:i AS uuid)"), {"i": ids[kind]})
        await s.execute(text("INSERT INTO job_postings (source_id, external_job_id, title, location, status) VALUES (CAST(:s AS uuid), 'h1', 'Eng', 'Remote (worldwide)', 'open')"),
                        {"s": ids["himalayas"]})
        await s.commit()
    return ids


async def test_every_state_is_shown_correctly(sm, seeded):
    async with sm() as s:
        d = await portals.portals(s, 200, 300)
    by = {b["name"]: b for b in d["company_boards"]}
    assert {n: b["state"] for n, b in by.items()} == {
        "greenhouse:alpha": "live", "greenhouse:alpha-small": "live", "greenhouse:beta": "empty", "greenhouse:gamma": "broken", "greenhouse:delta": "broken",
        "greenhouse:epsilon": "broken", "greenhouse:stripe": "live", "greenhouse:alpha-paused": "disabled"}
    assert d["company_boards"][0]["state"] == "broken"  # broken boards sort first
    assert {n for n, b in by.items() if b["small_board"]} == {"greenhouse:alpha-small", "greenhouse:stripe"}  # 5 jobs or fewer; a badge, not a state
    assert d["no_ats"] == [{"company_id": d["no_ats"][0]["company_id"], "company": "Zeta", "website_url": None, "careers_url": None}]  # a watched company with no board
    assert d["states"] == {"live": 4, "empty": 1, "broken": 3, "disabled": 2, "no_ats": 1}  # includes the two job boards: Himalayas live, Jooble disabled


async def test_counts_come_from_the_job_record_and_the_geo_rule(sm, seeded):
    async with sm() as s:
        d = await portals.portals(s, 200, 300)
    by = {b["name"]: b for b in d["company_boards"]}
    a = by["greenhouse:alpha"]
    assert (a["open"], a["india"], a["remote"]) == (3, 1, 1)  # Bengaluru is India, plain "Remote" is remote-eligible, London is neither
    st = by["greenhouse:stripe"]
    assert st["kind"] == "seeded" and st["company"] is None and (st["open"], st["india"]) == (3, 2) and st["discovered_via"] == "seed list"
    assert st["board_url"] == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    jb = {b["name"]: b for b in d["job_boards"]}
    assert jb["Himalayas"]["state"] == "live" and jb["Himalayas"]["remote"] == 1 and jb["Jooble India"]["state"] == "disabled"
    assert d["caps"] == {"companies": {"used": 6, "cap": 200}, "boards": {"used": 6, "cap": 300}}


# ---- the HTTP surface ----

@pytest.fixture
def api(sm, monkeypatch):
    monkeypatch.setattr(portals_api, "get_sessionmaker", lambda: sm)
    assert OK in get_settings().cors_list
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")


async def test_get_is_read_only_and_every_write_route_has_the_guard(api, seeded):
    r = await api.get("/api/portals")
    assert r.status_code == 200 and set(r.json()) == {"states", "company_boards", "job_boards", "no_ats", "caps"}
    routes = portals_api.router.routes  # FastAPI wraps included routers, so app.routes does not list these; the GET above proves the router is mounted
    assert sorted((x.path, tuple(sorted(x.methods - {"HEAD"}))) for x in routes) == [
        ("/api/portals", ("GET",)), ("/api/portals", ("POST",)), ("/api/portals/{route_id}/enabled", ("POST",)), ("/api/portals/{route_id}/recheck", ("POST",))]
    for x in routes:
        if "GET" in x.methods:
            assert x.methods == {"GET"}  # the read endpoint takes nothing else
        else:
            assert local_write in [d.call for d in x.dependant.dependencies], x.path  # every write goes through the J1 guard
    rid = seeded["live"]
    bad = {"Origin": "https://evil.example"}
    assert (await api.post("/api/portals", json={"company_id": str(rid), "url": "https://acme.example/careers"}, headers=bad)).status_code == 403
    assert (await api.post(f"/api/portals/{rid}/enabled", json={"enabled": False})).status_code == 403  # no Origin
    assert (await api.post(f"/api/portals/{rid}/recheck", content="{}", headers={"Origin": OK, "Content-Type": "text/plain"})).status_code == 415
    assert (await api.get("/api/portals")).json()["states"]["live"] == 4  # nothing changed


async def test_write_endpoints_work_same_origin_and_report_errors(api, seeded):
    hdr = {"Origin": OK, "Content-Type": "application/json"}
    r = await api.post(f"/api/portals/{seeded['live']}/enabled", json={"enabled": False}, headers=hdr)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await api.post("/api/portals/not-a-uuid/enabled", json={"enabled": True}, headers=hdr)).status_code == 404
    assert (await api.post(f"/api/portals/{seeded['jooble']}/enabled", json={"enabled": True}, headers=hdr)).status_code == 409  # Jooble stays gated
    assert (await api.post(f"/api/portals/{seeded['live']}/recheck", json={}, headers=hdr)).status_code == 409  # disabled now
    assert (await api.post(f"/api/portals/{seeded['failing']}/recheck", json={}, headers=hdr)).json()["queued"] is True
    assert (await api.post("/api/portals", json={"company_id": "00000000-0000-0000-0000-000000000000", "url": "https://acme.example/careers"}, headers=hdr)).status_code == 404


# ---- disable and enable ----

async def due(sm):
    async with sm() as s:
        return {r.name for r in (await s.execute(DUE_SQL)).all()}


async def test_disabling_a_board_stops_its_polling_and_frees_its_slot(sm, seeded):
    assert "greenhouse:alpha" in await due(sm)
    async with sm() as s:
        out = await portals.set_route_enabled(s, str(seeded["live"]), False, 200, 300)
        await s.commit()
        assert out == {"route_id": str(seeded["live"]), "name": "greenhouse:alpha", "enabled": False}
    assert "greenhouse:alpha" not in await due(sm)
    async with sm() as s:
        st = (await s.execute(text("SELECT status, promotion_reason FROM ats_boards WHERE board_token = 'alpha'"))).one()
        watched = (await s.execute(text("SELECT watched FROM companies WHERE name = 'Alpha'"))).scalar_one()
        used = await ats.active_boards(s)
    assert tuple(st) == ("paused", "paused by owner") and used == 5
    assert watched is True  # Alpha still has another active board (alpha-small), so it stays watched
    async with sm() as s:
        await portals.set_route_enabled(s, str(seeded["small"]), False, 200, 300)
        await s.commit()
        assert (await s.execute(text("SELECT watched FROM companies WHERE name = 'Alpha'"))).scalar_one() is False  # its last active board: the company slot is freed


async def test_enabling_respects_both_caps_and_a_seeded_board_just_toggles(sm, seeded):
    async with sm() as s:
        with pytest.raises(PortalError) as e:
            await portals.set_route_enabled(s, str(seeded["paused"]), True, 200, 6)  # 6 boards are active and the cap is 6
        assert e.value.status == 409 and "cap" in e.value.detail
        out = await portals.set_route_enabled(s, str(seeded["paused"]), True, 200, 7)
        assert out["enabled"] is True
        await s.commit()
        assert (await s.execute(text("SELECT status FROM ats_boards WHERE board_token = 'alpha-paused'"))).scalar_one() == "active"
        await portals.set_route_enabled(s, str(seeded["seeded"]), False, 200, 7)  # no ats_boards row: only the route changes
        await s.commit()
        assert (await s.execute(text("SELECT enabled FROM source_registry WHERE name = 'greenhouse:stripe'"))).scalar_one() is False
        with pytest.raises(PortalError) as e:
            await portals.set_route_enabled(s, "00000000-0000-0000-0000-000000000000", True, 200, 7)
        assert e.value.status == 404


# ---- add a portal from a careers URL ----

CAREERS = '<html><a href="https://boards.greenhouse.io/zypto">Open roles</a></html>'
BOARD = {"jobs": [{"id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/zypto/jobs/1", "location": {"name": "Bengaluru"},
                   "first_published": "2026-11-14T00:00:00Z"}]}


def web(r):
    if r.url.host == "acme.example":
        return httpx.Response(200, text=CAREERS)
    if r.url.host == "boards-api.greenhouse.io" and r.url.path == "/v1/boards/zypto/jobs":
        return httpx.Response(200, json=BOARD)
    return httpx.Response(404)


async def test_adding_a_careers_url_registers_a_board_under_the_caps(sm):
    async with sm() as s:
        cid = str(await company(s, "Zypto", watched=False))
        await s.commit()
    async with sm() as s:  # no room under the board cap: stored as a candidate and not polled
        out = await portals.add_portal(s, cid, "https://acme.example/careers", 200, 0, client(web))
        await s.commit()
    assert out == [{"provider": "greenhouse", "token": "zypto", "result": "candidate"}]
    async with sm() as s:
        b = (await s.execute(text("SELECT status, discovered_via, evidence->>'url' AS url, last_job_count FROM ats_boards WHERE board_token = 'zypto'"))).one()
        enabled = (await s.execute(text("SELECT enabled FROM source_registry WHERE name = 'greenhouse:zypto'"))).scalar_one()
    assert tuple(b) == ("candidate", "added in Portals", "https://acme.example/careers", 1) and enabled is False
    async with sm() as s:  # the same URL again with room: promoted
        out = await portals.add_portal(s, cid, "https://acme.example/careers", 200, 300, client(web))
        await s.commit()
    assert out[0]["result"] == "registered"
    async with sm() as s:
        assert (await s.execute(text("SELECT status FROM ats_boards WHERE board_token = 'zypto'"))).scalar_one() == "active"


async def test_a_board_url_pasted_directly_needs_no_page_fetch(sm):
    async with sm() as s:
        cid = str(await company(s, "Zypto", watched=False))
        out = await portals.add_portal(s, cid, "https://boards.greenhouse.io/zypto", 200, 300, client(lambda r: web(r) if r.url.host != "boards.greenhouse.io" else (_ for _ in ()).throw(AssertionError("page fetched"))))
        await s.commit()
    assert out[0]["result"] == "registered"


async def test_add_portal_refuses_what_it_cannot_use(sm):
    async with sm() as s:
        cid = str(await company(s, "Zypto", watched=False))
        for url, status in (("ftp://acme.example/x", 422), ("https://www.zippia.com/jobs", 422), ("https://nothing.example/careers", 422)):
            with pytest.raises(PortalError) as e:
                await portals.add_portal(s, cid, url, 200, 300, client(lambda r: httpx.Response(200, text="<html>no board here</html>")))
            assert e.value.status == status, url
        with pytest.raises(PortalError) as e:
            await portals.add_portal(s, "00000000-0000-0000-0000-000000000000", "https://acme.example/careers", 200, 300, client(web))
        assert e.value.status == 404
        assert (await s.execute(text("SELECT count(*) FROM ats_boards"))).scalar_one() == 0


async def test_a_dead_board_is_reported_invalid_not_registered(sm):
    async with sm() as s:
        cid = str(await company(s, "Zypto", watched=False))
        out = await portals.add_portal(s, cid, "https://boards.greenhouse.io/ghost", 200, 300, client(lambda r: httpx.Response(404)))
        await s.commit()
    assert out == [{"provider": "greenhouse", "token": "ghost", "result": "invalid"}]
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM ats_boards"))).scalar_one() == 0


# ---- re-check now ----

async def test_recheck_is_queued_once_and_the_worker_polls_the_route(sm, seeded, monkeypatch):
    async with sm() as s:
        first = await portals.request_recheck(s, str(seeded["failing"]))
        again = await portals.request_recheck(s, str(seeded["failing"]))
        await s.commit()
        with pytest.raises(PortalError) as e:
            await portals.request_recheck(s, str(seeded["paused"]))  # a disabled route cannot be re-checked
        assert e.value.status == 409
    assert first["queued"] is True and again["queued"] is False  # one per route per 5 minutes

    def listing(r):
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "SRE", "absolute_url": "https://g/1", "location": {"name": "Pune"}}]})

    monkeypatch.setattr(worker, "build_adapter", lambda name, config, **kw: AtsAdapter(config["provider"], config["token"], client(listing)))
    assert await worker.portal_checks(sm, get_settings()) == 1
    async with sm() as s:
        job = (await s.execute(text("SELECT status FROM job_queue WHERE kind = 'portal_check'"))).scalar_one()
        route = (await s.execute(text("SELECT consecutive_failures, health_status FROM source_registry WHERE name = 'greenhouse:gamma'"))).one()
        runs = (await s.execute(text("SELECT status, items_fetched FROM source_runs"))).all()
    assert job == "done" and tuple(route) == (0, "HEALTHY") and [tuple(r) for r in runs] == [("ok", 1)]  # the 3 failures are cleared by one good poll


async def test_portal_counts_use_the_stored_workplace_like_explore(sm, seeded):
    async with sm() as s:
        for ext, loc, wp in (("w1", "Remote", "hybrid"), ("w2", None, "remote")):
            await s.execute(text("INSERT INTO job_postings (source_id, external_job_id, title, location, workplace, status) VALUES (CAST(:s AS uuid), :e, 'Eng', :l, :w, 'open')"),
                            {"s": seeded["live"], "e": ext, "l": loc, "w": wp})
        await s.commit()
        a = {b["name"]: b for b in (await portals.portals(s, 200, 300))["company_boards"]}["greenhouse:alpha"]
    assert (a["open"], a["remote"]) == (5, 2)  # hybrid "Remote" is not remote; a remote job with no location is; the 3 original rows give 1


async def test_discovery_lists_seeded_boards_too(sm, seeded, monkeypatch):
    from app.api import intel
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    d = await intel.discovery()
    names = {(b["provider"], b["board_token"]) for b in d["boards"]}
    assert ("greenhouse", "stripe") in names and ("greenhouse", "alpha") in names
    assert sum(x["n"] for x in d["board_status"]) == len(d["boards"])


async def test_add_portal_creates_a_new_company_or_reuses_an_exact_match(sm):
    async with sm() as s:
        out = await portals.add_portal(s, None, "https://acme.example/careers", 200, 300, client(web), company_name="Zypto Labs")
        await s.commit()
    assert out[0]["result"] == "registered"
    async with sm() as s:
        row = (await s.execute(text("SELECT c.name, c.watched FROM ats_boards b JOIN companies c ON c.id = b.company_id WHERE b.board_token = 'zypto'"))).one()
        assert tuple(row) == ("Zypto Labs", True)
        n = (await s.execute(text("SELECT count(*) FROM companies"))).scalar_one()
        await portals.add_portal(s, None, "https://acme.example/careers", 200, 300, client(web), company_name="zypto labs")  # same company again
        assert (await s.execute(text("SELECT count(*) FROM companies"))).scalar_one() == n
        with pytest.raises(PortalError) as e:
            await portals.add_portal(s, None, "https://acme.example/careers", 200, 300, client(web))
        assert e.value.status == 422


async def test_a_new_company_is_not_kept_when_no_board_lists_jobs(sm, monkeypatch):
    async def dead(*a, **k):
        return "invalid"
    monkeypatch.setattr(portals, "register_board", dead)
    async with sm() as s:
        with pytest.raises(PortalError):
            await portals.add_portal(s, None, "https://acme.example/careers", 200, 300, client(web), company_name="Ghostco")
        await s.rollback()  # what the API does on PortalError
        assert (await s.execute(text("SELECT count(*) FROM companies WHERE name = 'Ghostco'"))).scalar_one() == 0
