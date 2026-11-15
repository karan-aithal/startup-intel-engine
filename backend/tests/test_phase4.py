"""Phase 4: Telegram preview poller, Jetstream resume, page monitor, feed discovery with evidence."""
import httpx
import pytest
from sqlalchemy import text

from app.connectors.bluesky import normalize_post, stream_once
from app.connectors.page_monitor import PageMonitorAdapter
from app.connectors.telegram_preview import TelegramPreviewAdapter
from app.services import registry
from app.services.discovery import classify_links, discover_company

TG = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="ch/5">
<div class="tgme_widget_message_text js-message_text">Acme raises <b>$5M</b> seed<br/>led by X</div>
<a class="tgme_widget_message_date"><time datetime="2026-11-10T10:00:00+00:00"></time></a></div>
<div class="tgme_widget_message" data-post="ch/6"><div class="tgme_widget_message_text">older</div>
<time datetime="2026-11-10T11:00:00+00:00"></time></div></div>"""


def mock(routes: dict):
    def h(req: httpx.Request):
        r = routes.get(str(req.url))
        return httpx.Response(200, text=r) if isinstance(r, str) else httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(h), follow_redirects=True)


async def test_telegram_cursor_and_normalize():
    a = TelegramPreviewAdapter("ch", mock({"https://t.me/s/ch": TG}))
    r = await a.fetch(None)
    assert [i["post"] for i in r.items] == ["ch/5", "ch/6"] and r.cursor == {"last_id": 6}
    ev = await a.normalize(r.items[0])
    assert ev.content == "Acme raises $5M seed\nled by X" and ev.published_at.year == 2026
    assert (await a.fetch(r.cursor)).items == []  # nothing new


def jmsg(t, rkey="a"):
    return {"did": "did:plc:x", "time_us": t, "kind": "commit", "commit": {
        "operation": "create", "collection": "app.bsky.feed.post", "rkey": rkey,
        "record": {"text": "We raised a seed round", "createdAt": "2026-11-10T10:00:00Z"}}}


async def test_jetstream_drop_resumes_without_loss(sm):
    async with sm() as s:
        sid = await registry.register_source(s, "BS", "stream")
        await s.commit()
    seen_cursors = []

    async def dropping(dids, cur):
        seen_cursors.append(cur)
        yield jmsg(1000, "a")
        yield jmsg(2000, "b")
        raise ConnectionError("dropped")

    async def resumed(dids, cur):
        seen_cursors.append(cur)
        yield jmsg(2000, "b")  # replayed by the overlap: must dedup
        yield jmsg(3000, "c")

    with pytest.raises(ConnectionError):
        await stream_once(sm, "BS", sid, ["did:plc:x"], dropping)
    await stream_once(sm, "BS", sid, ["did:plc:x"], resumed)
    assert seen_cursors == [None, 2000]
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM source_events"))).scalar_one() == 3


def test_bluesky_ignores_non_posts():
    assert normalize_post("BS", {"kind": "identity"}) is None
    assert normalize_post("BS", jmsg(1)).canonical_url == "https://bsky.app/profile/did:plc:x/post/a"


JOB = '<script type="application/ld+json">{"@type":"JobPosting","title":"Eng","identifier":{"value":"7"},"datePosted":"2026-11-10","hiringOrganization":{"name":"Acme"},"jobLocation":{"address":{"addressLocality":"Bengaluru","addressCountry":"IN"}}}</script>'


async def test_page_monitor_jsonld_and_sitemap_diff(sm):
    pages = {"https://a.com/robots.txt": "User-agent: *\nAllow: /", "https://a.com/careers": JOB,
             "https://a.com/sitemap.xml": "<urlset><loc>https://a.com/jobs/old</loc></urlset>"}
    a = PageMonitorAdapter("Acme careers", "https://a.com/careers", "Acme", mock(pages))
    r1 = await a.fetch(None)
    ev = await a.normalize(r1.items[0])
    assert (ev.title, ev.location, ev.event_type) == ("Eng", "Bengaluru, IN", "job")
    assert len(r1.items) == 1  # baseline sitemap emits nothing
    pages["https://a.com/sitemap.xml"] = "<urlset><loc>https://a.com/jobs/old</loc><loc>https://a.com/jobs/new</loc></urlset>"
    pages["https://a.com/jobs/new"] = "<title>Designer</title>"
    a = PageMonitorAdapter("Acme careers", "https://a.com/careers", "Acme", mock(pages))
    r2 = await a.fetch(r1.cursor)
    titles = [(await a.normalize(i)).title for i in r2.items]
    assert titles == ["Eng", "Designer"]


async def test_page_monitor_respects_robots():
    a = PageMonitorAdapter("x", "https://a.com/careers", None, mock({"https://a.com/robots.txt": "User-agent: *\nDisallow: /"}))
    with pytest.raises(PermissionError):
        await a.fetch(None)


HOME = """<html><head><link rel="alternate" type="application/rss+xml" href="/blog/rss.xml"></head><body>
<a href="https://github.com/acme">gh</a><a href="https://acme.substack.com/">s</a><a href="https://bsky.app/profile/acme.com">b</a>
<a href="https://t.me/acmechan">t</a><a href="/careers">jobs</a>
<a href="https://github.com/features">no</a></body></html>"""


def test_classify_links():
    got = {d["kind"]: d["url"] for d in classify_links("https://acme.com/", HOME)}
    assert got == {"blog_feed": "https://acme.com/blog/rss.xml", "github": "https://github.com/acme.atom",
                   "substack": "https://acme.substack.com/feed", "bluesky": "acme.com",
                   "telegram": "acmechan", "careers": "https://acme.com/careers"}


async def test_discovery_stores_evidence_and_registers_disabled(sm):
    async with sm() as s:
        await registry.register_source(s, "Bluesky Jetstream", "stream")
        cid = (await s.execute(text(
            "INSERT INTO companies (name, canonical_name, website_url) VALUES ('Acme','acme','https://acme.com/') RETURNING id"))).scalar_one()
        await s.commit()
    async with sm() as s:
        n = await discover_company(s, str(cid), mock({"https://acme.com/": HOME}))
        await s.commit()
    assert n == 6
    async with sm() as s:
        rows = (await s.execute(text("SELECT kind, evidence FROM company_sources WHERE kind='github'"))).all()
        assert rows[0].evidence["found_on"] == "https://acme.com/" and rows[0].evidence["href"] == "https://github.com/acme"
        assert (await s.execute(text("SELECT count(*) FROM source_registry WHERE enabled"))).scalar_one() == 0
        assert (await s.execute(text("SELECT enabled FROM source_accounts"))).scalar_one() is False
        assert (await s.execute(text("SELECT careers_url FROM companies"))).scalar_one() == "https://acme.com/careers"


async def test_watchlist_api(sm, monkeypatch):
    from app.api import watchlist as w
    monkeypatch.setattr(w, "get_sessionmaker", lambda: sm)
    await w.add("telegram", w.Item(value="https://t.me/Some_Channel"))
    await w.add("bluesky", w.Item(value="@acme.bsky.social"))
    assert await w.list_all() == {"telegram": ["Some_Channel"], "bluesky": ["acme.bsky.social"]}
    await w.remove("telegram", "Some_Channel")
    await w.remove("bluesky", "acme.bsky.social")
    assert await w.list_all() == {"telegram": [], "bluesky": []}
    with pytest.raises(Exception):
        await w.add("telegram", w.Item(value="x y"))


def upd(uid, text, sender=7, mid=1):
    return {"update_id": uid, "message": {"message_id": mid, "chat": {"id": sender}, "from": {"id": sender}, "date": 1763200000, "text": text}}


async def test_share_bot_x_links_dedup_and_allowlist(sm):
    from app.connectors.runner import run_once
    from app.connectors.share_bot import ShareBotAdapter, canonical
    assert canonical("https://twitter.com/a/status/1?s=20&t=x") == canonical("https://www.x.com/a/status/1/") == "https://x.com/a/status/1"
    sent = [upd(10, "Acme raised $5M seed https://twitter.com/a/status/1?s=20"), upd(11, "again https://x.com/a/status/1", mid=2),
            upd(12, "spam https://evil.io", sender=99)]
    calls = []

    def h(req: httpx.Request):
        calls.append(req.url.params["offset"])
        return httpx.Response(200, json={"result": [u for u in sent if u["update_id"] >= int(req.url.params["offset"])]})

    a = ShareBotAdapter("Telegram Share", "tok", {7}, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    async with sm() as s:
        await registry.register_source(s, "Telegram Share", "share_to_bot")
        await registry.set_enabled(s, "Telegram Share", "share_to_bot", True)
        await s.commit()
    assert (await run_once(sm, a))["new"] == 1  # same X post twice = one event; stranger ignored
    await run_once(sm, a)
    assert calls == ["0", "13"]
    async with sm() as s:
        assert (await s.execute(text("SELECT canonical_url FROM source_events"))).scalar_one() == "https://x.com/a/status/1"
