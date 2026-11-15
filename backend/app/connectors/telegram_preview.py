"""Telegram public channel poller: parses https://t.me/s/<channel> (no login, last ~20 posts per page)."""
from datetime import datetime
from html.parser import HTMLParser

import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.feed import USER_AGENT


class _Posts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self._depth = 0  # >0 while inside the message text div

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-post"):
            self.posts.append({"post": a["data-post"], "text": "", "time": None})
        elif not self.posts:
            return
        elif tag == "div" and self._depth:
            self._depth += 1
        elif tag == "div" and "tgme_widget_message_text" in (a.get("class") or ""):
            self._depth = 1
        elif tag == "time" and a.get("datetime"):
            self.posts[-1]["time"] = a["datetime"]
        elif tag == "br" and self._depth:
            self.posts[-1]["text"] += "\n"

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.posts[-1]["text"] += data


def parse_channel_page(html: str) -> list[dict]:
    p = _Posts()
    p.feed(html)
    return p.posts


class TelegramPreviewAdapter(SourceAdapter):
    connector_type = "public_preview_poller"

    def __init__(self, channel: str, client: httpx.AsyncClient | None = None):
        self.channel = channel.lstrip("@")
        self.name = f"telegram:{self.channel}"
        self._client = client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        try:
            r = await client.get(f"https://t.me/s/{self.channel}", headers={"User-Agent": USER_AGENT})
        finally:
            if self._client is None:
                await client.aclose()
        r.raise_for_status()
        posts = parse_channel_page(r.text)
        if not posts and "tgme_channel_info" not in r.text:  # t.me redirects non-previewable channels to the profile page
            raise ValueError(f"channel {self.channel} has no public preview")
        last = (cursor or {}).get("last_id", 0)
        new = [p for p in posts if int(p["post"].rsplit("/", 1)[1]) > last]
        top = max([last, *(int(p["post"].rsplit("/", 1)[1]) for p in new)])
        return FetchResult(items=new, cursor={"last_id": top})

    async def normalize(self, item) -> NormalizedEvent | None:
        text = item["text"].strip()
        if not text:  # media-only post
            return None
        url = f"https://t.me/{item['post']}"
        return NormalizedEvent(
            source_name=self.name, source_event_id=item["post"], source_url=url, canonical_url=url,
            event_type="post", title=text.split("\n", 1)[0][:200], content=text[:4000],
            published_at=datetime.fromisoformat(item["time"]) if item["time"] else None,
            raw_metadata={"channel": self.channel},
            permissions_metadata={"store": "public channel preview text and link only"})

    async def health_check(self) -> bool:
        await self.fetch()
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, incremental_sync=True, sync_methods=["message_id"],
                            permissions="public t.me/s preview, no login", quota="poll every 5-15 min")
