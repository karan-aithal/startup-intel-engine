"""/api/pipeline (J7): the GETs are read-only; every write goes through the J1 write guard (decision 100). Tracking only: nothing applies or sends."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.guard import local_write
from app.db.models.tracked import STATES
from app.db.session import get_sessionmaker
from app.services import tracked

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])
State = Literal[STATES]  # type: ignore[valid-type]


class Track(BaseModel):
    event_ids: list[str] = Field(max_length=tracked.MAX_BULK)
    status: Literal["saved", "skipped"]


class Bulk(BaseModel):
    ids: list[str] = Field(max_length=tracked.MAX_BULK)
    status: State
    note: str | None = Field(None, max_length=500)


class Patch(BaseModel):
    status: State | None = None
    note: str | None = Field(None, max_length=500)  # goes on the history row of the status change
    notes: str | None = None
    contact: str | None = None
    salary_text: str | None = None
    next_step: str | None = None
    applied_at: datetime | None = None


async def _run(fn, *args):
    async with get_sessionmaker()() as s:
        try:
            out = await fn(s, *args)
        except tracked.TrackError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return out


@router.get("")
async def pipeline(tab: str = "inbox", q: str = Query("", max_length=200), sort: str = "updated", mode: Literal["all", "india", "remote"] = "all",
                   days: int = Query(7, ge=1, le=30), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)) -> dict:
    return await _run(tracked.overview, tab, q, sort, mode, days, offset, limit)


@router.post("/track", dependencies=[Depends(local_write)])
async def track(body: Track) -> dict:
    return await _run(tracked.track, body.event_ids, body.status)


@router.post("/bulk", dependencies=[Depends(local_write)])
async def bulk(body: Bulk) -> dict:
    return await _run(tracked.set_status, body.ids, body.status, body.note)


@router.get("/{tid}")
async def one(tid: str) -> dict:
    return await _run(tracked.detail, tid)


@router.patch("/{tid}", dependencies=[Depends(local_write)])
async def patch(tid: str, body: Patch) -> dict:
    sent = body.model_fields_set
    async with get_sessionmaker()() as s:
        try:
            await tracked.edit(s, tid, {k: getattr(body, k) for k in tracked.FIELD_MAX if k in sent}, body.applied_at, "applied_at" in sent)
            if body.status:
                await tracked.set_status(s, [tid], body.status, body.note)
        except tracked.TrackError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return {"ok": True}


@router.delete("/{tid}", dependencies=[Depends(local_write)])
async def restore(tid: str) -> dict:
    await _run(tracked.restore, tid)
    return {"restored": True}
