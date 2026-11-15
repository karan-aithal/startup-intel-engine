"""IMAP connector for the dedicated alerts inbox (imap-tools, app password, read-only: never marks mail seen)."""
import asyncio
from datetime import UTC, datetime, timedelta

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.email_parsers import parse

MAX_BODY = 200_000
FIRST_RUN_DAYS = 7  # no cursor yet: look back this far, not the whole mailbox
BATCH = 200


def _fetch_blocking(host: str, user: str, password: str, folder: str, cursor: dict | None) -> tuple[list[dict], dict]:
    """Not covered by unit tests (needs a live server). Returns (messages, new cursor)."""
    from imap_tools import AND, MailBox

    with MailBox(host).login(user, password, initial_folder=folder) as mb:
        validity = str(mb.folder.status(folder).get("UIDVALIDITY"))
        last = int(cursor["last_uid"]) if cursor and cursor.get("uidvalidity") == validity else 0
        crit = AND(uid=f"{last + 1}:*") if last else AND(date_gte=(datetime.now(UTC) - timedelta(days=FIRST_RUN_DAYS)).date())
        out = []
        for m in mb.fetch(crit, mark_seen=False, bulk=True, limit=BATCH):
            if int(m.uid) <= last:  # `UID n:*` always returns the newest message even if older than n
                continue
            out.append({"uid": int(m.uid), "from_email": m.from_values.email if m.from_values else "",
                        "subject": m.subject, "date": m.date.astimezone(UTC) if m.date else None,
                        "html": (m.html or "")[:MAX_BODY], "text": (m.text or "")[:MAX_BODY],
                        "message_id": (m.headers.get("message-id") or ("",))[0].strip("<> ") or None})
    newest = max((x["uid"] for x in out), default=last)
    return out, {"uidvalidity": validity, "last_uid": newest}


class ImapAdapter(SourceAdapter):
    connector_type = "email_alert_parser"

    def __init__(self, name: str, host: str = "imap.gmail.com", user: str = "", password: str = "",
                 folder: str = "INBOX", fetcher=None):
        if not (user and password) and fetcher is None:
            raise ValueError("IMAP_USER and IMAP_PASSWORD are required for the email route")
        self.name, self.host, self.user, self.password, self.folder = name, host, user, password, folder
        self._fetcher = fetcher

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        if self._fetcher:
            msgs, new = await self._fetcher(cursor)
        else:
            msgs, new = await asyncio.to_thread(_fetch_blocking, self.host, self.user, self.password, self.folder, cursor)
        return FetchResult(items=msgs, cursor=new)

    async def normalize(self, item) -> list[NormalizedEvent]:
        return parse(item, self.name)

    async def health_check(self) -> bool:
        await self.fetch({"uidvalidity": "x", "last_uid": 2**31 - 2})  # logs in, fetches nothing
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(incremental_sync=True, auth_type="app_password", sync_methods=["imap uid cursor"],
                            permissions="own dedicated mailbox", quota="Gmail IMAP bandwidth limits apply (verify)")
