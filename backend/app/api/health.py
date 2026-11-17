from datetime import UTC, datetime

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

from app.db.session import check_db, get_sessionmaker
from app.services import ops

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


# Not under /health so Prometheus gets the conventional path. Read-only; the API only listens on 127.0.0.1.
metrics_router = APIRouter()


@metrics_router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    async with get_sessionmaker()() as s:
        return await ops.metrics_text(s)
