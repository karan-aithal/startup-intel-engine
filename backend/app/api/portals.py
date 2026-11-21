"""/api/portals (J4): the one GET is read-only; the three writes go through the J1 write guard (Host, Origin, JSON content type)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.guard import local_write
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.services import portals

router = APIRouter(prefix="/api/portals", tags=["portals"])


class AddPortal(BaseModel):
    company_id: UUID | None = None
    company_name: str | None = Field(default=None, max_length=200)  # a company not tracked yet; used when company_id is absent
    url: str = Field(min_length=8, max_length=500)


class Enabled(BaseModel):
    enabled: bool


def _caps() -> tuple[int, int]:
    s = get_settings()
    return s.auto_watch_cap, s.auto_board_cap


@router.get("")
async def list_portals() -> dict:
    async with get_sessionmaker()() as s:
        return await portals.portals(s, *_caps())


@router.post("", dependencies=[Depends(local_write)])
async def add_portal(body: AddPortal) -> dict:
    async with get_sessionmaker()() as s:
        try:
            results = await portals.add_portal(s, str(body.company_id) if body.company_id else None, body.url, *_caps(), company_name=body.company_name)
        except portals.PortalError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return {"results": results}


@router.post("/{route_id}/enabled", dependencies=[Depends(local_write)])
async def set_enabled(route_id: str, body: Enabled) -> dict:
    async with get_sessionmaker()() as s:
        try:
            out = await portals.set_route_enabled(s, route_id, body.enabled, *_caps())
        except portals.PortalError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return out


@router.post("/{route_id}/recheck", dependencies=[Depends(local_write)])
async def recheck(route_id: str) -> dict:
    async with get_sessionmaker()() as s:
        try:
            out = await portals.request_recheck(s, route_id)
        except portals.PortalError as e:
            await s.rollback()
            raise HTTPException(e.status, e.detail) from e
        await s.commit()
    return out
