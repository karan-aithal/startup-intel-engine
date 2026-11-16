"""Alert sinks (Telegram or console) and the delivery loop over the outbox."""
import asyncio
import json

import httpx
import structlog

from app.core.config import Settings
from app.services import outbox
from app.services.pipeline import format_alert

log = structlog.get_logger()


class ConsoleSink:
    async def send(self, topic: str | None, message: str) -> str | None:
        print(f"--- ALERT [{topic}] ---\n{message}\n", flush=True)
        return None


class TelegramSink:
    """Bot API sendMessage into a supergroup topic (message_thread_id)."""

    def __init__(self, token: str, chat_id: str, topic_ids: dict[str, int], client: httpx.AsyncClient | None = None):
        self.url, self.chat_id, self.topic_ids = f"https://api.telegram.org/bot{token}/sendMessage", chat_id, topic_ids
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, topic: str | None, message: str) -> str | None:
        body = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        if topic in self.topic_ids:
            body["message_thread_id"] = self.topic_ids[topic]
        r = await self._client.post(self.url, json=body)
        if r.status_code != 200:  # includes 429 flood control: the outbox retries with backoff
            raise RuntimeError(f"telegram {r.status_code}: {r.text[:200]}")
        return str(r.json()["result"]["message_id"])


def make_sink(s: Settings):
    if s.telegram_bot_token and s.telegram_chat_id:
        return TelegramSink(s.telegram_bot_token, s.telegram_chat_id, json.loads(s.telegram_topic_ids or "{}"))
    return ConsoleSink()


async def deliver_pending(sm, sink, tz: str = "Asia/Kolkata", limit: int = 10, pause_s: float = 1.1) -> int:
    """Claim due alerts, send, mark sent or failed. A crash mid-send re-delivers after the lease expires."""
    async with sm() as s:
        batch = await outbox.claim_pending(s, limit=limit)
        await s.commit()
    sent = 0
    for d in batch:
        try:
            msg_id = await sink.send(d["topic"], format_alert(d["topic"], d["payload"], tz, resent=bool(d.get("possible_duplicate"))))
            async with sm() as s:
                await outbox.mark_sent(s, str(d["id"]), msg_id)
                await s.commit()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("alert_send_failed", error=str(exc))
            async with sm() as s:
                await outbox.mark_failed(s, str(d["id"]), str(exc))
                await s.commit()
        await asyncio.sleep(pause_s)
    return sent
