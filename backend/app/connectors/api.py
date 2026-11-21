"""Hacker News (Algolia), SEC EDGAR Form D, and Greenhouse / Lever / Ashby job boards (more vendors: ats_vendors.py).

All are public, keyless endpoints. Field names were checked against live responses on 2026-11-12.
"""
import re
import time
from datetime import UTC, datetime

import httpx

from app.connectors.ats_vendors import VENDORS
from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.feed import USER_AGENT, FeedAdapter
from app.services.safefetch import guarded_client

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
SEC_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&count=40&output=atom"
ATS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
}


def board_url(provider: str, token: str) -> str:
    """The public board URL for any ATS provider."""
    return VENDORS[provider].board_url(token) if provider in VENDORS else ATS_URLS[provider].format(t=token)


def workplace(v) -> str | None:
    """remote | hybrid | onsite from an ATS workplaceType field (Lever: on-site/remote/hybrid/unspecified; Ashby: OnSite/Remote/Hybrid), else None."""
    s = v.strip().lower().replace("-", "") if isinstance(v, str) else ""
    return s if s in ("remote", "hybrid", "onsite") else None


async def _get_json(client: httpx.AsyncClient | None, url: str, **kw):
    c = client or guarded_client(timeout=20, follow_redirects=True)
    try:
        r = await c.get(url, headers={"User-Agent": USER_AGENT}, **kw)
        r.raise_for_status()
        return r.json()
    finally:
        if client is None:
            await c.aclose()


def _iso(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


class HackerNewsAdapter(SourceAdapter):
    connector_type = "api"

    def __init__(self, name: str, queries: list[str], client: httpx.AsyncClient | None = None):
        self.name, self.queries, self._client = name, queries, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        since = (cursor or {}).get("since") or int(time.time()) - 6 * 3600
        hits: dict[str, dict] = {}
        for q in self.queries:
            data = await _get_json(self._client, HN_API, params={
                "query": q, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 50})
            for h in data["hits"]:
                hits[h["objectID"]] = h
        newest = max((h["created_at_i"] for h in hits.values()), default=since)
        return FetchResult(items=list(hits.values()), cursor={"since": newest})

    async def normalize(self, item) -> NormalizedEvent | None:
        if not item.get("title"):
            return None
        url = item.get("url") or f"https://news.ycombinator.com/item?id={item['objectID']}"
        return NormalizedEvent(
            source_name=self.name, source_event_id=item["objectID"], source_url=url, canonical_url=url,
            event_type="article", title=item["title"], published_at=_iso(item.get("created_at")),
            raw_metadata={"region": "global", "points": item.get("points")},
            permissions_metadata={"store": "title and link only"})

    async def health_check(self) -> bool:
        await _get_json(self._client, HN_API, params={"query": "startup", "hitsPerPage": 1})
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(search=True, history=True, incremental_sync=True, public_feed=True,
                            sync_methods=["created_at_i cursor"], permissions="public Algolia HN API")


class SecFormDAdapter(FeedAdapter):
    """EDGAR current Form D filings. SEC requires a User-Agent with contact details; keep under 10 req/s."""

    def __init__(self, name: str, contact_email: str, client: httpx.AsyncClient | None = None):
        if not contact_email:
            raise ValueError("SEC_CONTACT_EMAIL is required for SEC EDGAR")
        super().__init__(name, SEC_ATOM, "global", client)
        self.user_agent = f"startup-intel-engine/0.1 {contact_email}"

    async def normalize(self, item) -> NormalizedEvent | None:
        title = item.get("title") or ""
        m = re.match(r"D - (.+?) \(\d+\)", title)  # skips "D/A" amendments
        if not m:
            return None
        ev = await super().normalize(item)
        if ev:
            ev.event_type, ev.company_name = "form_d", m.group(1)
            ev.permissions_metadata = {"store": "title and link only (public SEC data)"}
        return ev


class AtsAdapter(SourceAdapter):
    """One job board. Emits one 'job' event per posting; the (source, id) key makes repeats no-ops."""

    connector_type = "api"
    full_listing = True

    def __init__(self, provider: str, token: str, client: httpx.AsyncClient | None = None):
        if provider not in ATS_URLS and provider not in VENDORS:
            raise ValueError(f"unknown ATS provider {provider}")
        if provider in VENDORS:
            VENDORS[provider].check(token)  # a token is part of a host name: refuse anything that is not a plain board id
        self.provider, self.token, self._client = provider, token, client
        self.name = f"{provider}:{token}"

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        if self.provider in VENDORS:
            rows, complete = await VENDORS[self.provider].fetch(self._client, self.token)
            return FetchResult(items=rows, cursor={"count": len(rows)}, partial=not complete)
        data = await _get_json(self._client, ATS_URLS[self.provider].format(t=self.token))
        jobs = data if self.provider == "lever" else data["jobs"]
        return FetchResult(items=jobs, cursor={"count": len(jobs)})

    async def normalize(self, j) -> NormalizedEvent | None:
        p = self.provider
        if p in VENDORS:
            f = {**j, "wp": None}  # rows from ats_vendors are already {id, title, url, loc, pub, team}
        elif p == "greenhouse":
            f = dict(id=str(j["id"]), title=j["title"], url=j["absolute_url"], loc=(j.get("location") or {}).get("name"),
                     pub=_iso(j.get("first_published") or j.get("updated_at")), team=None, wp=None)
        elif p == "lever":
            c = j.get("categories") or {}
            f = dict(id=j["id"], title=j["text"], url=j["hostedUrl"], loc=c.get("location"),
                     pub=datetime.fromtimestamp(j["createdAt"] / 1000, UTC) if j.get("createdAt") else None,
                     team=c.get("team"), wp=workplace(j.get("workplaceType")))
        else:
            if j.get("isListed") is False:
                return None
            f = dict(id=j["id"], title=j["title"], url=j["jobUrl"], loc=j.get("location"),
                     pub=_iso(j.get("publishedAt")), team=j.get("department"), wp=workplace(j.get("workplaceType")))
        return NormalizedEvent(
            source_name=self.name, source_event_id=f["id"], source_url=f["url"], canonical_url=f["url"],
            event_type="job", title=f["title"], company_name=self.token.split("/")[0].split(".")[0] if p in VENDORS else self.token, location=f["loc"],
            published_at=f["pub"], raw_metadata={"ats": p, "board": self.token, "team": f["team"], **({"workplace": f["wp"]} if f["wp"] else {})},
            permissions_metadata={"store": "title, location, team and link only"})

    async def health_check(self) -> bool:
        await self.fetch()
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="public job board API", quota="none published")
