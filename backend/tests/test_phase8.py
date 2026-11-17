"""Phase 8: quota manager, adaptive scheduling, circuit breaker, fallback, activity, expansion, GitHub and Jooble connectors."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml
from sqlalchemy import text

from app.connectors.base import FetchResult
from app.connectors.factory import build_adapter
from app.connectors.github import GithubAdapter
from app.connectors.jooble import JoobleAdapter
from app.connectors.runner import run_once
from app.services import expansion, quota
from app.services.correlate import correlate_company
from app.services.pipeline import process_event
from app.services.quota import Limiter, RateLimited
from app.services.registry import register_source, set_enabled
from app.services.scheduler import DUE_SQL, next_interval, update_activity
from tests.test_phase1 import Fake


def now():
    return datetime.now(UTC)


# ---------- quota manager ----------
@pytest.fixture
def tiny_provider():
    quota.cfg()["providers"]["t_prov"] = {"burst": 3, "per_second": 1.0, "cap": 5, "window_seconds": 60}
    yield "t_prov"
    del quota.cfg()["providers"]["t_prov"]


async def test_token_bucket_refills_and_window_cap_holds(sm, tiny_provider):
    lim, t0 = Limiter(sm, tiny_provider), now()
    for _ in range(3):
        await lim.take(now=t0)
    with pytest.raises(RateLimited) as e:
        await lim.take(now=t0)  # bucket empty
    assert e.value.why == "bucket empty" and 0.9 < e.value.wait_s <= 1.1
    await lim.take(now=t0 + timedelta(seconds=1))  # one token back after a second: 4 of the 5 allowed in this window
    await lim.take(now=t0 + timedelta(seconds=2))  # 5th
    with pytest.raises(RateLimited) as e:
        await lim.take(now=t0 + timedelta(seconds=10))  # the bucket has tokens, the window cap does not
    assert e.value.why == "window cap reached" and e.value.wait_s > 40
    await lim.take(now=t0 + timedelta(seconds=61))  # new window


async def test_quota_survives_a_restart_and_is_shared_between_routes(sm, tiny_provider):
    t0 = now()
    for _ in range(3):
        await Limiter(sm, tiny_provider).take(now=t0)  # three different Limiter objects = three routes / three process lifetimes
    with pytest.raises(RateLimited):
        await Limiter(sm, tiny_provider).take(now=t0)


async def test_provider_429_blocks_until_the_time_it_gave(sm, tiny_provider):
    lim = Limiter(sm, tiny_provider)
    await lim.block(120)
    with pytest.raises(RateLimited) as e:
        await lim.take()
    assert e.value.why == "provider asked us to wait" and 100 < e.value.wait_s <= 120
    await Limiter(sm, "not_configured").take()  # unlisted providers are not limited


# ---------- adaptive intervals ----------
def test_interval_follows_activity_with_clamps_and_hot_exemption():
    def n(*a, **k):
        return next_interval(*a, rnd=0.5, **k)  # rnd=0.5 -> zero jitter

    assert n(600, None) == 600 and n(600, 80) == 300 and n(600, 40) == 600 and n(600, 15) == 900 and n(600, 5) == 1200
    assert n(600, 0) == 2400  # idle: x4
    assert n(1800, 0) == 3600  # never pushed past the maximum
    assert n(7200, 0) == 7200  # a base above the maximum is left alone
    assert n(100, 80) == 60  # never below the minimum
    assert n(600, 0, hot=True) == 600  # hot companies never slow down
    assert n(600, 80, hot=True) == 300  # but they do speed up
    assert n(600, None, retry_after_s=5000) == 5000  # the provider's wait wins
    assert (next_interval(600, None, rnd=0.0), next_interval(600, None, rnd=1.0)) == (540, 660)


# ---------- scheduling, ordering, fallback ----------
async def route(s, name, enabled=True, **kw):
    cfg = kw.pop("config", {"kind": "feed", "url": "x"})
    await register_source(s, name, "feed", config=cfg, poll_interval_seconds=60, **kw)
    if enabled:
        await set_enabled(s, name, "feed", True)


async def due(sm):
    async with sm() as s:
        return [r.name for r in (await s.execute(DUE_SQL)).all()]


async def test_due_order_prefers_priority_then_activity_and_skips_disabled_and_open_circuits(sm):
    async with sm() as s:
        await route(s, "low-quiet", priority=50)
        await route(s, "low-busy", priority=50)
        await route(s, "high", priority=90)
        await route(s, "off", enabled=False)
        await route(s, "broken", priority=99)
        await s.execute(text("UPDATE source_registry SET activity_score = 80 WHERE name = 'low-busy'"))
        await s.execute(text("UPDATE source_registry SET activity_score = 5 WHERE name = 'low-quiet'"))
        await s.execute(text("UPDATE source_registry SET circuit_open_until = now() + interval '5 minutes' WHERE name = 'broken'"))
        await s.commit()
    assert await due(sm) == ["high", "low-busy", "low-quiet"]


async def test_next_poll_at_is_respected_and_old_routes_fall_back_to_last_run(sm):
    async with sm() as s:
        await route(s, "scheduled")
        await route(s, "legacy")
        await s.execute(text("UPDATE source_registry SET next_poll_at = now() + interval '10 minutes' WHERE name = 'scheduled'"))
        await s.execute(text("INSERT INTO source_runs (source_id, started_at, status) SELECT id, now(), 'ok' FROM source_registry WHERE name = 'legacy'"))
        await s.commit()
    assert await due(sm) == []  # legacy rows (no next_poll_at) behave as before: last run + interval (60 s)
    async with sm() as s:
        await s.execute(text("UPDATE source_registry SET next_poll_at = now() - interval '1 second' WHERE name = 'scheduled'"))
        await s.commit()
    assert await due(sm) == ["scheduled"]


async def test_fallback_route_is_polled_only_while_its_primary_is_down(sm):
    async with sm() as s:
        await route(s, "primary")
        await route(s, "backup", config={"kind": "feed", "url": "y", "fallback_for": "primary"})
        await s.commit()
    assert await due(sm) == ["primary"]
    async with sm() as s:
        await s.execute(text("UPDATE source_registry SET circuit_open_until = now() + interval '5 minutes' WHERE name = 'primary'"))
        await s.commit()
    assert await due(sm) == ["backup"]
    async with sm() as s:  # a primary the owner switched off on purpose does not call in its fallback
        await s.execute(text("UPDATE source_registry SET enabled = false, circuit_open_until = NULL, health_status = 'UNAVAILABLE' WHERE name = 'primary'"))
        await s.commit()
    assert await due(sm) == []


# ---------- circuit breaker and deferral ----------
async def test_circuit_opens_after_three_failures_allows_one_trial_and_closes_on_success(sm):
    async with sm() as s:
        await route(s, "flaky")
        await s.commit()
    for _ in range(2):
        await run_once(sm, Fake("flaky", fail=True))
    async with sm() as s:
        assert (await s.execute(text("SELECT circuit_open_until FROM source_registry WHERE name = 'flaky'"))).scalar_one() is None
    await run_once(sm, Fake("flaky", fail=True))
    async with sm() as s:
        r = (await s.execute(text("SELECT consecutive_failures, circuit_open_until - now() AS left FROM source_registry WHERE name = 'flaky'"))).one()
        assert r.consecutive_failures == 3 and timedelta(minutes=4) < r.left < timedelta(minutes=6)  # 300 s +-10%
        await s.execute(text("UPDATE source_registry SET circuit_open_until = now() - interval '1 second', next_poll_at = now() - interval '1 second'"))
        await s.commit()
    assert await due(sm) == ["flaky"]  # cooldown over: one trial
    await run_once(sm, Fake("flaky", fail=True))  # trial fails: longer cooldown (x2)
    async with sm() as s:
        assert (await s.execute(text("SELECT circuit_open_until - now() > interval '8 minutes' FROM source_registry"))).scalar_one()
        await s.execute(text("UPDATE source_registry SET circuit_open_until = now() - interval '1 second'"))
        await s.commit()
    assert (await run_once(sm, Fake("flaky", ["a"])))["status"] == "ok"
    async with sm() as s:
        r = (await s.execute(text("SELECT consecutive_failures, circuit_open_until FROM source_registry"))).one()
        assert (r.consecutive_failures, r.circuit_open_until) == (0, None)


class Limited(Fake):
    def __init__(self, name, wait=None, retry=None):
        super().__init__(name)
        self.wait, self.retry = wait, retry

    async def fetch(self, cursor=None):
        if self.wait:
            raise RateLimited(self.wait, "test")
        return FetchResult(items=["a"], cursor={"n": 1}, retry_after_s=self.retry)


async def test_rate_limit_defers_without_counting_as_a_failure(sm):
    async with sm() as s:
        await route(s, "api")
        await s.commit()
    assert (await run_once(sm, Limited("api", wait=900)))["status"] == "deferred"
    async with sm() as s:
        r = (await s.execute(text("""SELECT consecutive_failures, next_poll_at - now() AS wait,
            (SELECT status FROM source_runs LIMIT 1) AS run FROM source_registry"""))).one()
    assert r.consecutive_failures == 0 and r.run == "skipped" and timedelta(seconds=850) < r.wait < timedelta(seconds=1000)


async def test_partial_batch_is_stored_and_the_next_poll_waits_for_the_quota(sm):
    async with sm() as s:
        await route(s, "api")
        await s.commit()
    assert (await run_once(sm, Limited("api", retry=3000)))["status"] == "ok"
    async with sm() as s:
        assert (await s.execute(text("SELECT next_poll_at - now() > interval '49 minutes' FROM source_registry"))).scalar_one()
        assert (await s.execute(text("SELECT count(*) FROM source_events"))).scalar_one() == 1


# ---------- activity ----------
async def test_activity_score_follows_new_items_and_idle_sources_are_marked(sm):
    async with sm() as s:
        for n in ("busy", "idle", "new"):
            await route(s, n)
        for name, items in (("busy", 14), ("idle", 0)):
            await s.execute(text("INSERT INTO source_runs (source_id, status, items_new) SELECT id, 'ok', :n FROM source_registry WHERE name = :s"), {"n": items, "s": name})
        assert await update_activity(s) == 2
        await s.commit()
        scores = dict((await s.execute(text("SELECT name, activity_score FROM source_registry"))).all())
    assert scores == {"busy": 40, "idle": 0, "new": None}  # 25 * log2(1 + 14/7) = 39.6 ; a source with no runs stays unscored


# ---------- watchlist expansion ----------
async def company_feed(s, name, *, hot=False, india=False, kind="blog_feed", url=None):
    cid = (await s.execute(text("""INSERT INTO companies (name, canonical_name, is_india_linked, hot_until)
        VALUES (:n, :n, :i, CASE WHEN :h THEN now() + interval '30 days' END) RETURNING id"""), {"n": name, "i": india, "h": hot})).scalar_one()
    rid = await register_source(s, f"{name} {kind}", "feed", config={"kind": "feed", "url": url or f"https://{name}.test/feed"})
    await s.execute(text("INSERT INTO company_sources (company_id, source_id, kind, url, status) VALUES (:c, :r, :k, :u, 'discovered')"),
                    {"c": cid, "r": rid, "k": kind, "u": url or f"https://{name}.test/feed"})
    return str(cid)


async def test_expansion_is_off_at_cap_zero_ranks_by_hot_then_india_and_demotes_failures(sm):
    async with sm() as s:
        await company_feed(s, "plain")
        await company_feed(s, "hot", hot=True)
        await company_feed(s, "indian", hot=True, india=True)
        await company_feed(s, "gh", hot=True, kind="github", url="https://github.com/gh.atom")  # GitHub is polled by its own route
        await s.commit()
        assert await expansion.expand_sources(s, 0) == 0
        assert await expansion.expand_sources(s, 1) == 1
        assert (await s.execute(text("SELECT r.name FROM source_registry r WHERE r.enabled"))).scalar_one() == "indian blog_feed"
        assert await expansion.expand_sources(s, 1) == 0  # full
        assert await expansion.expand_sources(s, 3) == 1  # one more: the other hot company; "plain" is not watched/hot/funded, "gh" is skipped
        await s.commit()
        assert {r[0] for r in (await s.execute(text("SELECT r.name FROM source_registry r WHERE r.enabled"))).all()} == {"indian blog_feed", "hot blog_feed"}
        await s.execute(text("UPDATE source_registry SET consecutive_failures = 3 WHERE name = 'hot blog_feed'"))
        assert await expansion.demote_sources(s) == 1
        assert not (await s.execute(text("SELECT enabled FROM source_registry WHERE name = 'hot blog_feed'"))).scalar_one()
        assert (await s.execute(text("SELECT status FROM company_sources cs JOIN source_registry r ON r.id = cs.source_id WHERE r.name = 'hot blog_feed'"))).scalar_one() == "discovered"


async def test_github_org_list_is_synced_into_the_route_config(sm):
    async with sm() as s:
        await register_source(s, expansion.GITHUB_ROUTE, "api", config={"kind": "github", "orgs": []})
        a = await company_feed(s, "acme", hot=True, kind="github", url="https://github.com/acme-inc.atom")
        await company_feed(s, "cold", kind="github", url="https://github.com/cold.atom")  # not watched, hot or funded: not polled
        assert await expansion.sync_github_orgs(s) == 1
        cfg = (await s.execute(text("SELECT config FROM source_registry WHERE name = :n"), {"n": expansion.GITHUB_ROUTE})).scalar_one()
    assert cfg["orgs"] == [{"org": "acme-inc", "company_id": a}] and cfg["kind"] == "github"


# ---------- GitHub connector ----------
def repo(i, created, name="acme/tool", **kw):
    return {"id": i, "name": name.split("/")[1], "full_name": name, "html_url": f"https://github.com/{name}", "description": "a tool", "created_at": created, "fork": False,
            "archived": False, "homepage": "https://www.acme.com", "stargazers_count": 3, "language": "Python", **kw}


def iso(days_ago):
    return (now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_github_reports_only_new_original_repos_and_uses_etags():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"etag": '"v1"'}, json=[repo(3, iso(1)), repo(2, iso(2), name="acme/fork", fork=True),
                                                                   repo(1, iso(100), name="acme/old"),
                                                                   repo(4, iso(1), name="acme/.github")])  # the org profile repo is not a launch

    ad = GithubAdapter("GitHub Orgs", [{"org": "acme", "company_id": "c1"}], token="tok", client=gh_client(handler))
    r = await ad.fetch()
    assert [x["repo"]["id"] for x in r.items] == [3]  # not the fork, the .github profile repo or the 100-day-old repo (first-poll horizon is 30 days)
    assert seen[0].headers["authorization"] == "Bearer tok" and seen[0].url.params["sort"] == "created" and ad.provider == "github"
    e = await ad.normalize(r.items[0])
    assert (e.event_type, e.source_event_id, e.title, e.company_domain) == ("repo", "repo:3", "acme/tool", "acme.com")
    assert e.raw_metadata["company_id"] == "c1" and e.published_at is not None
    again = await ad.fetch(r.cursor)
    assert again.items == [] and seen[1].headers["if-none-match"] == '"v1"'  # unchanged org: a 304


async def test_github_without_a_token_uses_the_small_quota_and_sends_no_credentials():
    seen = []
    ad = GithubAdapter("g", [{"org": "acme"}], client=gh_client(lambda req: seen.append(req) or httpx.Response(200, json=[])))
    await ad.fetch()
    assert ad.provider == "github_anon" and "authorization" not in seen[0].headers


async def test_github_rate_limit_blocks_the_provider_and_keeps_what_was_collected(sm):
    reset = int((now() + timedelta(seconds=500)).timestamp())

    def handler(req: httpx.Request):
        if "/orgs/first/" in str(req.url):
            return httpx.Response(200, json=[repo(7, iso(1), name="first/x")])
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}, json={"message": "API rate limit exceeded"})

    ad = GithubAdapter("g", [{"org": "first"}, {"org": "second"}, {"org": "third"}], token="t", client=gh_client(handler))
    ad.limiter = Limiter(sm, "github")
    r = await ad.fetch()
    assert [x["repo"]["id"] for x in r.items] == [7] and 480 < r.retry_after_s < 520
    with pytest.raises(RateLimited) as e:
        await Limiter(sm, "github").take()  # every route now waits for GitHub's reset
    assert e.value.why == "provider asked us to wait"


async def test_github_skips_a_missing_org_instead_of_failing_the_batch():
    ad = GithubAdapter("g", [{"org": "gone"}, {"org": "here"}], client=gh_client(
        lambda req: httpx.Response(404) if "/gone/" in str(req.url) else httpx.Response(200, json=[repo(5, iso(1))])))
    assert [x["repo"]["id"] for x in (await ad.fetch()).items] == [5]


# ---------- Jooble connector ----------
KEY = "SECRET-KEY-123"


def jooble(handler, **kw):
    return JoobleAdapter("Jooble India", KEY, "https://jooble.org/api/", kw.pop("keywords", ["software engineer", "data scientist"]), "India",
                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def test_jooble_is_gated_by_terms_confirmation_and_a_key():
    cfg = {"kind": "jooble", "keywords": ["x"], "location": "India", "terms_confirmed": False}
    with pytest.raises(ValueError, match="terms_confirmed"):
        build_adapter("Jooble India", cfg, secrets={"jooble_api_key": KEY})
    with pytest.raises(ValueError, match="JOOBLE_API_KEY"):
        build_adapter("Jooble India", {**cfg, "terms_confirmed": True}, secrets={})
    assert isinstance(build_adapter("Jooble India", {**cfg, "terms_confirmed": True}, secrets={"jooble_api_key": KEY}), JoobleAdapter)
    seed = yaml.safe_load((quota.CONFIG.parent / "sources.yaml").read_text())
    assert seed["jooble"]["terms_confirmed"] is False  # shipped off


async def test_jooble_posts_per_keyword_and_normalizes_jobs():
    bodies = []

    def handler(req: httpx.Request):
        bodies.append((str(req.url), req.content))
        return httpx.Response(200, json={"totalCount": 1, "jobs": [{"id": 11, "title": "Backend Engineer", "location": "Bengaluru", "snippet": "Build <b>APIs</b>&nbsp;now",
                                                                    "salary": "", "source": "x.in", "type": "Full-time", "link": "https://in.jooble.org/desc/11",
                                                                    "company": "Zypto", "updated": "2026-11-16T10:00:00.0000000"}]})

    ad = jooble(handler)
    r = await ad.fetch()
    assert len(bodies) == 2 and bodies[0][0] == f"https://jooble.org/api/{KEY}" and b'"keywords":"software engineer"' in bodies[0][1]
    assert len(r.items) == 1  # the same posting from two keywords is one job
    e = await ad.normalize(r.items[0])
    assert (e.event_type, e.source_event_id, e.company_name, e.country) == ("job", "11", "Zypto", "India")
    assert e.content.split() == ["Build", "APIs", "now"] and e.canonical_url == "https://in.jooble.org/desc/11" and e.published_at == datetime(2026, 11, 16, 10, tzinfo=UTC)


async def test_jooble_daily_limit_stops_until_midnight_utc_and_errors_never_leak_the_key(sm):
    ad = jooble(lambda req: httpx.Response(429, json={"error": "Daily request limit exceeded"}))
    ad.limiter = Limiter(sm, "jooble")
    r = await ad.fetch()
    assert r.items == [] and 0 < r.retry_after_s <= 86400
    with pytest.raises(RateLimited):
        await Limiter(sm, "jooble").take()

    def boom(req):
        raise httpx.ConnectError("boom for " + str(req.url))

    for bad in (lambda req: httpx.Response(403, text="Access Denied"), boom):
        with pytest.raises(RuntimeError) as e:
            await jooble(bad).fetch()
        assert KEY not in str(e.value) and "jooble.org" not in str(e.value)


# ---------- launch signals stay out of the alert path ----------
async def test_repo_events_are_stored_and_correlated_but_never_alerted(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme', 'acme') RETURNING id"))).scalar_one()
        sid = await register_source(s, "GitHub Orgs", "api")
        eid = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content, published_at, raw_metadata)
            VALUES (:s, 'repo:1', 'repo', 'acme/hiring-tool', 'We are hiring! Acme raises $5M seed', now() - interval '2 days', CAST(:m AS jsonb)) RETURNING id"""),
            {"s": sid, "m": f'{{"company_id": "{cid}"}}'})).scalar_one()
        assert await process_event(s, str(eid)) == "none"  # a repo blurb that matches funding/hiring keywords must not alert
        assert (await s.execute(text("SELECT count(*) FROM alert_deliveries"))).scalar_one() == 0
        await correlate_company(s, str(cid), now())
        sig = (await s.execute(text("SELECT signal_type, data FROM company_signals"))).one()
        assert sig.signal_type == "new_repo" and sig.data["repos"] == 1


async def test_github_org_must_resemble_the_company_before_its_repos_are_credited_to_it(sm):
    assert expansion.org_matches_company("daisytuner", "daisytuner") and expansion.org_matches_company("esp-mosaico", "mosaico")
    assert expansion.org_matches_company("acme-inc", "Acme") and not expansion.org_matches_company("espressif", "mosaico")
    assert not expansion.org_matches_company("ab", "ab")  # too short to mean anything
    async with sm() as s:
        await register_source(s, expansion.GITHUB_ROUTE, "api", config={"kind": "github", "orgs": []})
        c = await company_feed(s, "mosaico", hot=True, kind="github", url="https://github.com/espressif.atom")  # a partner the site links to
        await s.execute(text("INSERT INTO company_sources (company_id, kind, url, status) VALUES (:c, 'github', 'https://github.com/esp-mosaico.atom', 'discovered')"), {"c": c})
        assert await expansion.sync_github_orgs(s) == 1
        cfg = (await s.execute(text("SELECT config FROM source_registry WHERE name = :n"), {"n": expansion.GITHUB_ROUTE})).scalar_one()
    assert cfg["orgs"] == [{"org": "esp-mosaico", "company_id": c}]  # the company's own org, not the partner's
