"""Share-to-bot: links and text you send or forward to the Telegram bot (X, LinkedIn, WhatsApp, anywhere).
We never fetch the shared page; only what you sent is stored."""
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter

URL = re.compile(r"https?://[^\s<>\"']+")
X_HOSTS = {"twitter.com", "mobile.twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"}


def canonical(url: str) -> str:
    u = urlparse(url.rstrip(".,)"))
    host = u.netloc.lower().removeprefix("www.")
    if host in X_HOSTS:  # one canonical form so the same post shared twice dedups
        return urlunparse(("https", "x.com", u.path.rstrip("/"), "", "", ""))
    q = urlencode([(k, v) for k, v in parse_qsl(u.query) if not k.startswith("utm_") and k not in ("fbclid", "gclid")])
    return urlunparse((u.scheme, host, u.path.rstrip("/") or "/", "", q, ""))


class ShareBotAdapter(SourceAdapter):
    connector_type = "share_to_bot"

    def __init__(self, name: str, token: str, allowed_ids: set[int], client: httpx.AsyncClient | None = None):
        if not token or not allowed_ids:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID are required for share-to-bot")
        self.name, self.allowed, self._client = name, allowed_ids, client
        self.url = f"https://api.telegram.org/bot{token}/getUpdates"

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        offset = (cursor or {}).get("offset", 0)
        c = self._client or httpx.AsyncClient(timeout=20)
        try:
            r = await c.get(self.url, params={"offset": offset, "timeout": 0, "allowed_updates": '["message","channel_post"]'})
        finally:
            if self._client is None:
                await c.aclose()
        r.raise_for_status()
        ups = r.json()["result"]
        # chats/senders outside the allow-list are dropped here, but still acknowledged so they don't pile up
        mine = [u["message"] for u in ups if (m := u.get("message")) and
                ({m["chat"]["id"], (m.get("from") or {}).get("id")} & self.allowed)]
        return FetchResult(items=mine, cursor={"offset": max([u["update_id"] for u in ups], default=offset - 1) + 1})

    async def normalize(self, m) -> list[NormalizedEvent]:
        body = (m.get("text") or m.get("caption") or "").strip()
        urls = list(dict.fromkeys(canonical(u) for u in URL.findall(body)))
        prose = " ".join(URL.sub("", body).split())
        when = datetime.fromtimestamp(m["date"], UTC)
        origin = (m.get("forward_origin") or {}).get("type")
        return [NormalizedEvent(
            source_name=self.name, source_event_id=f"{m['chat']['id']}/{m['message_id']}/{i}", source_url=u, canonical_url=u,
            event_type="shared_link", title=(prose or u)[:200], content=prose or None, published_at=when,
            raw_metadata={"shared_from": urlparse(u).netloc, "forwarded": bool(origin)},
            permissions_metadata={"store": "text you sent and the link only; the page is not fetched"})
            for i, u in enumerate(urls)]

    async def health_check(self) -> bool:
        async with httpx.AsyncClient(timeout=15) as c:
            return (await c.get(self.url.replace("getUpdates", "getMe"))).status_code == 200

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(incremental_sync=True, sync_methods=["update_offset"], auth_type="bot_token",
                            permissions="messages you send to your own bot", quota="Bot API getUpdates")
