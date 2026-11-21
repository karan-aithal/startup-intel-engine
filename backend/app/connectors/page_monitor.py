"""Careers-page monitor: schema.org JobPosting data on the page, plus new job-like URLs in the sitemap.
Only runs where robots.txt allows it."""
import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.feed import USER_AGENT
from app.services.safefetch import guarded_client

JOB_URL = re.compile(r"/(jobs?|careers?|openings?|positions?)/[^/]+", re.I)
MAX_NEW_PAGES = 25  # job pages fetched per run
MAX_SEEN = 5000  # sitemap URLs remembered in the cursor


class _LdJson(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._on = False
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        self._on = tag == "script" and dict(attrs).get("type") == "application/ld+json"
        self._in_title = tag == "title"
        if self._on:
            self.blocks.append("")

    def handle_endtag(self, tag):
        self._on = self._in_title = False

    def handle_data(self, data):
        if self._on:
            self.blocks[-1] += data
        elif self._in_title:
            self.title += data


def job_postings(html: str) -> list[dict]:
    """All JobPosting objects in the page's JSON-LD (handles lists and @graph)."""
    p = _LdJson()
    p.feed(html)
    out, stack = [], []
    for b in p.blocks:
        try:
            stack.append(json.loads(b))
        except ValueError:
            continue
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, dict):
            stack.extend(o.get("@graph", []))
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                out.append(o)
    return out


def sitemap_urls(xml: str) -> tuple[list[str], list[str]]:
    """(page urls, child sitemap urls)."""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    return ([u for u in locs if not u.endswith(".xml")], [u for u in locs if u.endswith(".xml")]) if "<sitemapindex" in xml \
        else (locs, [])


def _location(j: dict) -> str | None:
    loc = j.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    a = (loc or {}).get("address") if isinstance(loc, dict) else None
    if isinstance(a, dict):
        return ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry")) if isinstance(x, str)) or None
    return a if isinstance(a, str) else None


class PageMonitorAdapter(SourceAdapter):
    connector_type = "page_monitor"

    def __init__(self, name: str, careers_url: str, company: str | None = None, client: httpx.AsyncClient | None = None):
        self.name, self.careers_url, self.company, self._client = name, careers_url, company, client
        u = urlparse(careers_url)
        self.origin = f"{u.scheme}://{u.netloc}"

    async def _get(self, c: httpx.AsyncClient, url: str) -> httpx.Response:
        r = await c.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r

    async def _allowed(self, c: httpx.AsyncClient, url: str) -> tuple[bool, list[str]]:
        r = await c.get(f"{self.origin}/robots.txt", headers={"User-Agent": USER_AGENT})
        if r.status_code >= 400:  # no robots.txt = no restrictions
            return True, []
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url), rp.site_maps() or []

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        c = self._client or guarded_client(timeout=20, follow_redirects=True)
        try:
            ok, maps = await self._allowed(c, self.careers_url)
            if not ok:
                raise PermissionError(f"robots.txt disallows {self.careers_url}")
            items = [{"posting": j, "url": self.careers_url} for j in job_postings((await self._get(c, self.careers_url)).text)]
            seen = set((cursor or {}).get("seen", []))
            urls: list[str] = []
            try:
                queue = list(maps or [f"{self.origin}/sitemap.xml"])
                while queue and len(urls) < 50_000:
                    pages, children = sitemap_urls((await self._get(c, queue.pop())).text)
                    urls += pages
                    queue += children[:20]
            except httpx.HTTPError:
                pass  # no sitemap is fine; the page itself was checked
            hashes = {hashlib.sha1(u.encode()).hexdigest()[:12]: u for u in urls if JOB_URL.search(u)}
            remember = set(hashes) if cursor is None else set()  # first run records a baseline instead of flooding
            if cursor is not None:
                new_urls = [u for h, u in hashes.items() if h not in seen][:MAX_NEW_PAGES]  # the rest wait for next run
                remember = {hashlib.sha1(u.encode()).hexdigest()[:12] for u in new_urls}
                for u in new_urls:
                    try:
                        html = (await self._get(c, u)).text
                    except httpx.HTTPError:
                        continue
                    found = job_postings(html)
                    items += [{"posting": j, "url": u} for j in found] or [{"posting": None, "url": u, "html": html}]
        finally:
            if self._client is None:
                await c.aclose()
        return FetchResult(items=items, cursor={"seen": list({*seen, *remember})[-MAX_SEEN:]})

    async def normalize(self, item) -> NormalizedEvent | None:
        j, url = item["posting"], item["url"]
        if j is None:  # job-like URL with no structured data
            p = _LdJson()
            p.feed(item["html"])
            title = " ".join(p.title.split())
            return NormalizedEvent(source_name=self.name, source_event_id=url, source_url=url, canonical_url=url,
                                   event_type="job", title=title or url, company_name=self.company,
                                   raw_metadata={"origin": "sitemap"}, permissions_metadata={"store": "title and link only"})
        link = j.get("url") or url
        org = j.get("hiringOrganization")
        try:
            pub = datetime.fromisoformat(j["datePosted"]) if j.get("datePosted") else None
        except ValueError:
            pub = None
        ident = j.get("identifier")
        ident = ident.get("value") if isinstance(ident, dict) else ident
        return NormalizedEvent(
            source_name=self.name, source_event_id=str(ident or link), source_url=link, canonical_url=urljoin(url, link),
            event_type="job", title=j.get("title"), location=_location(j),
            company_name=(org.get("name") if isinstance(org, dict) else org) or self.company, published_at=pub,
            raw_metadata={"origin": "jsonld"}, permissions_metadata={"store": "title, location and link only"})

    async def health_check(self) -> bool:
        async with guarded_client(timeout=15, follow_redirects=True) as c:
            return (await c.head(self.careers_url, headers={"User-Agent": USER_AGENT})).status_code < 400

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, incremental_sync=True, sync_methods=["sitemap_diff", "jsonld"],
                            permissions="robots.txt checked before every run", quota="poll every few hours")
