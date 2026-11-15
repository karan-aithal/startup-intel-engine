"""Editable watchlists: Telegram public channels and Bluesky accounts. Removing disables, so history stays."""
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.services.registry import register_source, set_enabled

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])
BLUESKY = "Bluesky Jetstream"
PATTERNS = {"telegram": re.compile(r"[A-Za-z0-9_]{4,64}"),
            "bluesky": re.compile(r"(did:plc:[a-z0-9]+|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)")}
Kind = Literal["telegram", "bluesky"]


class Item(BaseModel):
    value: str


async def ensure_bluesky(s) -> str:
    """The stream route exists once, enabled; it idles until an account is watched."""
    fields = dict(category="community", access_method="Jetstream WebSocket",
                  docs_url="https://github.com/bluesky-social/jetstream", config={"kind": "bluesky", "accounts": []})
    row = (await s.execute(text("SELECT id FROM source_registry WHERE name = :n AND connector_type = 'stream'"),
                           {"n": BLUESKY})).scalar_one_or_none()
    if row is None:
        row = await register_source(s, BLUESKY, "stream", **fields)
        await set_enabled(s, BLUESKY, "stream", True)
    return str(row)


def clean(kind: str, value: str) -> str:
    v = value.strip().removeprefix("https://t.me/s/").removeprefix("https://t.me/").removeprefix("@") if kind == "telegram" \
        else value.strip().removeprefix("https://bsky.app/profile/").removeprefix("@")
    if not PATTERNS[kind].fullmatch(v):
        raise HTTPException(422, f"not a valid {kind} name: {value!r}")
    return v


@router.get("")
async def list_all() -> dict:
    async with get_sessionmaker()() as s:
        tg = (await s.execute(text("""SELECT config->>'channel' FROM source_registry
            WHERE connector_type = 'public_preview_poller' AND enabled ORDER BY 1"""))).scalars().all()
        bs = (await s.execute(text("""SELECT a.handle FROM source_accounts a JOIN source_registry r ON r.id = a.source_id
            WHERE r.name = :n AND a.enabled ORDER BY 1"""), {"n": BLUESKY})).scalars().all()
    return {"telegram": tg, "bluesky": bs}


@router.post("/{kind}")
async def add(kind: Kind, item: Item) -> dict:
    v = clean(kind, item.value)
    async with get_sessionmaker()() as s:
        if kind == "telegram":
            name = f"telegram:{v}"
            await register_source(s, name, "public_preview_poller", category="community",
                access_method="t.me/s preview", docs_url="https://core.telegram.org/api/web-preview",
                poll_interval_seconds=600, config={"kind": "telegram", "channel": v})
            await set_enabled(s, name, "public_preview_poller", True)
        else:
            sid = await ensure_bluesky(s)
            await s.execute(text("""INSERT INTO source_accounts (source_id, handle, url, enabled) VALUES (:s, :h, :u, true)
                ON CONFLICT (source_id, handle) DO UPDATE SET enabled = true"""),
                {"s": sid, "h": v, "u": f"https://bsky.app/profile/{v}"})
        await s.commit()
    return {"added": v}


@router.delete("/{kind}")
async def remove(kind: Kind, value: str) -> dict:
    v = clean(kind, value)
    async with get_sessionmaker()() as s:
        if kind == "telegram":
            await set_enabled(s, f"telegram:{v}", "public_preview_poller", False)
        else:
            await s.execute(text("UPDATE source_accounts SET enabled = false WHERE handle = :h"), {"h": v})
        await s.commit()
    return {"removed": v}
