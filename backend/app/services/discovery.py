"""Feed auto-discovery: find a company's own feeds/profiles from its website, store each with evidence,
and register the route DISABLED (enabling and watchlist caps are a user decision)."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.feed import USER_AGENT
from app.services.aggregators import is_aggregator
from app.services.registry import register_source
from app.services.safefetch import guarded_client

PROBES = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/blog/feed", "/blog/rss")
GITHUB_RESERVED = {"features", "sponsors", "about", "pricing", "topics", "orgs", "login", "enterprise", "marketplace"}
BLUESKY_SOURCE = "Bluesky Jetstream"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds: list[str] = []
        self.anchors: list[tuple[str, str]] = []  # (href, rel)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        href = a.get("href")
        if not href:
            return
        if tag == "link" and "alternate" in (a.get("rel") or "") and (a.get("type") or "") in (
                "application/rss+xml", "application/atom+xml"):
            self.feeds.append(href)
        elif tag == "a":
            self.anchors.append((href, a.get("rel") or ""))


def classify_links(base: str, html: str) -> list[dict]:
    """Pure: every feed/profile reference on the page as {kind, url, href, via}. Evidence = the company's own page links to it."""
    p = _Links()
    p.feed(html)
    host = urlparse(base).netloc.removeprefix("www.")
    out = [{"kind": "blog_feed", "url": urljoin(base, h), "href": h, "via": "link rel=alternate"} for h in p.feeds]
    for href, rel in p.anchors:
        u = urlparse(urljoin(base, href))
        h, parts = u.netloc.lower().removeprefix("www."), [x for x in u.path.split("/") if x]
        hit = None
        if h == "youtube.com" and len(parts) == 2 and parts[0] == "channel":
            hit = ("youtube", f"https://www.youtube.com/feeds/videos.xml?channel_id={parts[1]}")
        elif h == "youtube.com" and parts and parts[0].startswith("@"):
            hit = ("youtube_handle", f"https://www.youtube.com/{parts[0]}")  # channel id resolved by the caller
        elif h == "github.com" and len(parts) == 1 and parts[0].lower() not in GITHUB_RESERVED:
            hit = ("github", f"https://github.com/{parts[0]}.atom")
        elif h.endswith(".substack.com") and h != "substack.com":
            hit = ("substack", f"https://{h}/feed")
        elif h == "medium.com" and parts:
            hit = ("medium", f"https://medium.com/feed/{parts[0]}")
        elif h == "bsky.app" and len(parts) == 2 and parts[0] == "profile":
            hit = ("bluesky", parts[1])
        elif h == "t.me" and len(parts) == 1 and not parts[0].startswith("+") and parts[0] != "joinchat":
            hit = ("telegram", parts[0])
        elif h == host and not is_aggregator(h) and parts and re.fullmatch(r"(careers?|jobs?|join-us|work-with-us)", parts[0], re.I) and len(parts) == 1:
            hit = ("careers", u.geturl())
        if hit:
            out.append({"kind": hit[0], "url": hit[1], "href": href, "via": "link on company website"})
    seen, uniq = set(), []
    for d in out:
        if d["url"] not in seen:
            seen.add(d["url"])
            uniq.append(d)
    return uniq


async def _get(c: httpx.AsyncClient, url: str) -> httpx.Response:
    return await c.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


async def find_candidates(website: str, client: httpx.AsyncClient) -> list[dict]:
    if is_aggregator(website):  # a job aggregator reposts other employers' jobs: it is not this company's site
        return []
    r = await _get(client, website)
    r.raise_for_status()
    found = classify_links(str(r.url), r.text)
    for d in [x for x in found if x["kind"] == "youtube_handle"]:
        found.remove(d)
        try:
            m = re.search(r'channel_id=(UC[\w-]{20,})|"channelId":"(UC[\w-]{20,})"', (await _get(client, d["url"])).text)
        except httpx.HTTPError:
            continue
        if m:
            cid = m.group(1) or m.group(2)
            found.append({**d, "kind": "youtube", "url": f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"})
    if not any(d["kind"] == "blog_feed" for d in found):
        for path in PROBES:
            try:
                pr = await _get(client, urljoin(str(r.url), path))
                if pr.status_code == 200 and feedparser.parse(pr.content).entries:
                    found.append({"kind": "blog_feed", "url": str(pr.url), "href": path, "via": "probe: path returns a feed"})
                    break
            except httpx.HTTPError:
                continue
    for d in found:
        d["found_on"] = str(r.url)
    return found


def _route(company: str, d: dict) -> tuple[str, str, dict] | None:
    k = d["kind"]
    if k in ("blog_feed", "youtube", "github", "substack", "medium"):
        return f"{company} {k}", "feed", dict(category="company", access_method="RSS/Atom (discovered)",
            poll_interval_seconds=3600, config={"kind": "feed", "url": d["url"], "region": "global"})
    if k == "telegram":
        return f"telegram:{d['url']}", "public_preview_poller", dict(category="community",
            access_method="t.me/s preview (discovered)", poll_interval_seconds=600,
            config={"kind": "telegram", "channel": d["url"]})
    if k == "careers":
        return f"{company} careers", "page_monitor", dict(category="hiring", access_method="sitemap + JobPosting (discovered)",
            poll_interval_seconds=21600, config={"kind": "page_monitor", "careers_url": d["url"], "company": company})
    return None  # bluesky is a watchlist account, not a route


async def store_discovered(s: AsyncSession, company_id: str, company: str, found: list[dict]) -> int:
    """Insert company_sources rows with evidence; register routes (disabled). Returns count of new rows."""
    new = 0
    for d in found:
        route, source_id = _route(company, d), None
        if route:
            source_id = await register_source(s, route[0], route[1], **route[2])
        elif d["kind"] == "bluesky":
            source_id = (await s.execute(text("SELECT id FROM source_registry WHERE name = :n AND connector_type = 'stream'"),
                                         {"n": BLUESKY_SOURCE})).scalar_one_or_none()
            if source_id:  # added to the watchlist disabled: the user turns accounts on
                await s.execute(text("""INSERT INTO source_accounts (source_id, handle, url, company_id, role, evidence, enabled)
                    VALUES (:s, :h, :u, :c, 'company', CAST(:e AS jsonb), false) ON CONFLICT DO NOTHING"""),
                    {"s": source_id, "h": d["url"], "u": f"https://bsky.app/profile/{d['url']}", "c": company_id,
                     "e": json.dumps(d)})
        res = await s.execute(text("""INSERT INTO company_sources (company_id, source_id, kind, url, evidence, status)
            VALUES (:c, :s, :k, :u, CAST(:e AS jsonb), 'discovered') ON CONFLICT (company_id, url) DO NOTHING"""),
            {"c": company_id, "s": source_id, "k": d["kind"],
             "u": d["url"] if d["kind"] != "bluesky" else f"https://bsky.app/profile/{d['url']}", "e": json.dumps(d)})
        new += res.rowcount
    return new


async def discover_company(s: AsyncSession, company_id: str, client: httpx.AsyncClient) -> int:
    row = (await s.execute(text("SELECT name, website_url FROM companies WHERE id = :i"), {"i": company_id})).one()
    found = await find_candidates(row.website_url, client)
    n = await store_discovered(s, company_id, row.name, found)
    await s.execute(text("""UPDATE companies SET meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('feeds_discovered_at', now()::text),
        careers_url = coalesce(careers_url, (SELECT url FROM company_sources WHERE company_id = :i AND kind = 'careers' LIMIT 1))
        WHERE id = :i"""), {"i": company_id})
    return n


async def discover_pending(sm, limit: int = 5, client: httpx.AsyncClient | None = None) -> int:
    """Worker pass: companies with a website that were never scanned. A failing site is marked scanned so it isn't retried every pass."""
    async with sm() as s:
        ids = (await s.execute(text("""SELECT id FROM companies WHERE website_url IS NOT NULL AND NOT muted
            AND coalesce(meta, '{}'::jsonb)->'feeds_discovered_at' IS NULL LIMIT :n"""), {"n": limit})).scalars().all()
    total = 0
    c = client or guarded_client(timeout=15)
    try:
        for cid in ids:
            async with sm() as s:
                try:
                    total += await discover_company(s, str(cid), c)
                except Exception as exc:  # noqa: BLE001
                    await s.rollback()
                    await s.execute(text("UPDATE companies SET meta = coalesce(meta, '{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :i"),
                                    {"i": cid, "m": json.dumps({"feeds_discovered_at": "failed", "feeds_discovery_error": str(exc)[:300]})})
                await s.commit()
    finally:
        if client is None:
            await c.aclose()
    return total
