"""RSS/Atom feed connector with conditional requests (ETag / If-Modified-Since)."""
import calendar
import hashlib
from datetime import UTC, datetime

import feedparser
import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.services.safefetch import guarded_client

USER_AGENT = "startup-intel-engine/0.1 (personal research; polite poller)"


class FeedAdapter(SourceAdapter):
    connector_type = "feed"

    def __init__(self, name: str, url: str, region: str = "global", client: httpx.AsyncClient | None = None):
        self.name, self.url, self.region = name, url, region
        self._client = client
        self.user_agent = USER_AGENT

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        headers = {"User-Agent": self.user_agent, "Accept": "application/rss+xml, application/atom+xml, */*"}
        if cursor:
            if cursor.get("etag"):
                headers["If-None-Match"] = cursor["etag"]
            if cursor.get("last_modified"):
                headers["If-Modified-Since"] = cursor["last_modified"]
        client = self._client or guarded_client(timeout=20, follow_redirects=True)
        try:
            r = await client.get(self.url, headers=headers)
        finally:
            if self._client is None:
                await client.aclose()
        if r.status_code == 304:
            return FetchResult(items=[], cursor=cursor)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"unparseable feed: {parsed.bozo_exception}")
        new_cursor = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}
        return FetchResult(items=list(parsed.entries), cursor=new_cursor)

    async def normalize(self, item) -> NormalizedEvent | None:
        link = item.get("link")
        title = item.get("title")
        if not (link or title):
            return None
        pub = item.get("published_parsed") or item.get("updated_parsed")
        # feedparser returns a UTC time tuple. calendar.timegm reads it as UTC; time.mktime read it as LOCAL time, which stored every
        # published_at 5.5 h early on this IST host (found in Phase 11). ts_fixed marks events written with the correct conversion.
        published = datetime.fromtimestamp(calendar.timegm(pub), UTC) if pub else None
        eid = item.get("id") or link or hashlib.sha256((title or "").encode()).hexdigest()
        return NormalizedEvent(
            source_name=self.name, source_event_id=eid, source_url=link, canonical_url=link,
            event_type="article", title=title, content=item.get("summary"), published_at=published,
            raw_metadata={"region": self.region, "tags": [t.get("term") for t in item.get("tags", [])][:10], "ts_fixed": True},
            permissions_metadata={"store": "title, summary and link only"},
        )

    async def health_check(self) -> bool:
        async with guarded_client(timeout=15, follow_redirects=True) as c:
            return (await c.head(self.url, headers={"User-Agent": USER_AGENT})).status_code < 400

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, incremental_sync=True, sync_methods=["etag", "poll"],
                            permissions="public RSS; robots.txt checked", quota="none published")
