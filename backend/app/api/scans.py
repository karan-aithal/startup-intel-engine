"""/api/scans (J6): the GET is read-only; every write goes through the J1 write guard (decision 100)."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.guard import local_write
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.services import scans

router = APIRouter(prefix="/api/scans", tags=["scans"])


class Filters(BaseModel):
    """The Explore parameters a search saves (the same names and limits as GET /api/explore)."""
    mode: Literal["all", "india", "remote"] = "all"
    q: str = Field("", max_length=200)
    exclude: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    source: str = Field("", max_length=120)
    days: int = Field(7, ge=1, le=30)
    sort: Literal["newest", "fit", "company", "funded"] = "newest"
    status: Literal["open", "all"] = "open"


class NewSearch(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: Filters = Filters()
    interval_value: int = Field(6, ge=1, le=10000)
    interval_unit: Literal["minutes", "hours", "days"] = "hours"
    start_at: datetime | None = None
    timezone: str = Field(default_factory=lambda: get_settings().display_timezone, max_length=60)


async def _write(fn, *args):
    async with get_sessionmaker()() as s:
        try:
            out = await fn(s, *args)
        except scans.ScanError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return out


@router.get("")
async def list_scans(search_id: str | None = None) -> dict:
    async with get_sessionmaker()() as s:
        return await scans.overview(s, search_id)


@router.post("", dependencies=[Depends(local_write)])
async def create(body: NewSearch) -> dict:
    return {"id": await _write(scans.create, body.name.strip(), body.filters.model_dump(), body.interval_value, body.interval_unit, body.start_at, body.timezone)}


@router.post("/{sid}/pause", dependencies=[Depends(local_write)])
async def pause(sid: str) -> dict:
    await _write(scans.set_status, sid, "paused")
    return {"status": "paused"}


@router.post("/{sid}/resume", dependencies=[Depends(local_write)])
async def resume(sid: str) -> dict:
    await _write(scans.set_status, sid, "active")
    return {"status": "active"}


@router.post("/{sid}/run", dependencies=[Depends(local_write)])
async def run(sid: str) -> dict:
    await _write(scans.run_now, sid)
    return {"queued": True}


@router.delete("/{sid}", dependencies=[Depends(local_write)])
async def delete(sid: str) -> dict:
    await _write(scans.remove, sid)
    return {"deleted": True}
