"""Bluesky Jetstream stream: posts from watched accounts. Cursor = last time_us, resumed on reconnect."""
import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import httpx
import structlog
from sqlalchemy import text

from app.connectors.base import NormalizedEvent
from app.services.ingest import ingest_batch, load_cursor
from app.services.registry import is_enabled, record_failure, record_success

log = structlog.get_logger()
JETSTREAM = "wss://jetstream2.us-east.bsky.network/subscribe"
RESOLVE = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
OVERLAP_US = 5_000_000  # resume 5 s early; (source, event id) uniqueness drops the repeats
MAX_SESSION_S = 600  # reconnect every 10 min so newly discovered accounts join the filter


def normalize_post(name: str, msg: dict) -> NormalizedEvent | None:
    c = msg.get("commit") or {}
    if msg.get("kind") != "commit" or c.get("operation") != "create" or c.get("collection") != "app.bsky.feed.post":
        return None
    rec = c.get("record") or {}
    body = (rec.get("text") or "").strip()
    if not body:
        return None
    did, url = msg["did"], f"https://bsky.app/profile/{msg['did']}/post/{c['rkey']}"
    try:
        pub = datetime.fromisoformat(rec["createdAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        pub = None
    return NormalizedEvent(
        source_name=name, source_event_id=f"{did}/{c['rkey']}", source_url=url, canonical_url=url,
        event_type="post", title=body.split("\n", 1)[0][:200], content=body[:4000], published_at=pub,
        author_url=f"https://bsky.app/profile/{did}", raw_metadata={"did": did},
        permissions_metadata={"store": "public post text and link only"})


async def jetstream_connect(dids: list[str], cursor_us: int | None) -> AsyncIterator[dict]:
    import websockets  # a uvicorn[standard] dependency

    q = "wantedCollections=app.bsky.feed.post&" + "&".join(f"wantedDids={d}" for d in dids)
    if cursor_us:
        q += f"&cursor={cursor_us - OVERLAP_US}"
    async with websockets.connect(f"{JETSTREAM}?{q}", max_size=2**20) as ws:
        async for raw in ws:
            yield json.loads(raw)


async def resolve_did(handle: str, client: httpx.AsyncClient) -> str:
    if handle.startswith("did:"):
        return handle
    r = await client.get(RESOLVE, params={"handle": handle.lstrip("@")})
    r.raise_for_status()
    return r.json()["did"]


async def watched_dids(sm, source_id: str, config_handles: list[str]) -> list[str]:
    async with sm() as s:
        rows = (await s.execute(text("SELECT handle FROM source_accounts WHERE source_id = :i AND enabled"),
                                {"i": source_id})).scalars().all()
    async with httpx.AsyncClient(timeout=15) as c:
        out = []
        for h in {*config_handles, *rows}:
            try:
                out.append(await resolve_did(h, c))
            except Exception as exc:  # noqa: BLE001 - one bad handle must not stop the stream
                log.warning("bluesky_resolve_failed", handle=h, error=str(exc))
    return out


async def stream_once(sm, name: str, source_id: str, dids: list[str], connect: Callable = jetstream_connect,
                      max_session_s: float = MAX_SESSION_S) -> int:
    """One connection. Each message commits with its cursor, so a drop loses nothing."""
    async with sm() as s:
        cur = await load_cursor(s, source_id)
    n = 0
    try:
        async with asyncio.timeout(max_session_s):
            async for msg in connect(dids, (cur or {}).get("time_us")):
                ev = normalize_post(name, msg)
                async with sm() as s:
                    await ingest_batch(s, source_id, [ev] if ev else [], {"time_us": msg["time_us"]})
                    await record_success(s, source_id)
                    await s.commit()
                n += 1
    except TimeoutError:
        pass  # planned reconnect
    return n


async def stream_forever(sm, name: str, config: dict, connect: Callable = jetstream_connect,
                         max_backoff: float = 300, max_session_s: float = MAX_SESSION_S) -> None:
    """Reconnect with exponential backoff; resume from the stored cursor. Idles while disabled or unwatched."""
    async with sm() as s:
        source_id = str((await s.execute(
            text("SELECT id FROM source_registry WHERE name = :n AND connector_type = 'stream'"), {"n": name})).scalar_one())
    delay = 1.0
    while True:
        try:
            async with sm() as s:
                on = await is_enabled(s, name, "stream")
            dids = await watched_dids(sm, source_id, config.get("accounts", [])) if on else []
            if not dids:
                await asyncio.sleep(60)
                continue
            n = await stream_once(sm, name, source_id, dids, connect, max_session_s)
            delay = 1.0 if n else min(delay * 2, max_backoff)  # a server that closes at once must not be hammered
            await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            log.warning("stream_dropped", source=name, error=str(exc), retry_in=delay)
            async with sm() as s:
                await record_failure(s, source_id, f"{type(exc).__name__}: {exc}")
                await s.commit()
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)
