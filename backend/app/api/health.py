from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.db.session import check_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict:
    return {"status": "ok", "time_utc": datetime.now(UTC).isoformat()}


@router.get("/ready")
async def ready(response: Response) -> dict:
    try:
        db = await check_db()
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        response.status_code = 503
        return {"status": "degraded", "database": {"ok": False, "error": type(exc).__name__}}
    missing = {"vector", "pg_trgm"} - set(db["extensions"])
    ok = not missing and db["migration"] is not None
    if not ok:
        response.status_code = 503
    return {
        "status": "ok" if ok else "degraded",
        "database": {"ok": True, **db, "missing_extensions": sorted(missing)},
    }
