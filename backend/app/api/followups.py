"""/api/followups (J8): the GETs are read-only; every write goes through the J1 write guard (decision 100). Reminders only: nothing sends."""
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.guard import local_write
from app.db.session import get_sessionmaker
from app.services import followups

router = APIRouter(prefix="/api/followups", tags=["followups"])


class Log(BaseModel):
    channel: str = Field(max_length=40)
    contact: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=2000)
    on: date | None = None  # the day it happened; default today
    next_on: date | None = None  # optionally pin the next date in the same step


class Pin(BaseModel):
    on: date | None = None  # null clears the pin


class Stop(BaseModel):
    stopped: bool


async def _run(fn, write: bool = True):
    """fn(session, cfg). The file is read per call; a write while follow-ups are off is refused."""
    c = followups.load_cfg()
    if write and not c["enabled"]:
        raise HTTPException(409, "follow-ups are off (config/followups.yaml)")
    async with get_sessionmaker()() as s:
        try:
            out = await fn(s, c)
        except followups.FollowupError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return out


@router.get("")
async def overview() -> dict:
    return await _run(followups.overview, write=False)


@router.get("/enabled")
async def enabled() -> dict:
    """What the nav link asks on every page: one small file read."""
    return {"enabled": followups.load_cfg()["enabled"]}


@router.post("/{tid}/log", dependencies=[Depends(local_write)])
async def log(tid: str, body: Log) -> dict:
    now = datetime.now(UTC)
    return {"id": await _run(lambda s, c: followups.log(s, tid, body.channel, body.contact, body.notes, body.on, body.next_on, c, now))}


@router.put("/{tid}/pin", dependencies=[Depends(local_write)])
async def pin(tid: str, body: Pin) -> dict:
    await _run(lambda s, c: followups.pin(s, tid, body.on))
    return {"ok": True}


@router.put("/{tid}/stop", dependencies=[Depends(local_write)])
async def stop(tid: str, body: Stop) -> dict:
    await _run(lambda s, c: followups.stop(s, tid, body.stopped))
    return {"ok": True}


@router.delete("/log/{lid}", dependencies=[Depends(local_write)])
async def delete_log(lid: str) -> dict:
    await _run(lambda s, c: followups.delete_log(s, lid))
    return {"ok": True}
