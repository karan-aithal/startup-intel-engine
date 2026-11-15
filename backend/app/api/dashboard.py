"""Read-only endpoints for the dashboard."""
from pathlib import Path

import yaml
from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import get_sessionmaker

router = APIRouter(prefix="/api", tags=["dashboard"])
CHECKLIST = Path(__file__).resolve().parents[3] / "config" / "alert_checklist.yaml"


@router.get("/alert-checklist")
async def alert_checklist() -> dict:
    return yaml.safe_load(CHECKLIST.read_text())


@router.get("/source-latency")
async def source_latency(days: int = 7) -> list[dict]:
    """Median seconds between source publication and our detection, per source."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(text("""
            SELECT r.name, count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM e.detected_at - e.published_at)) AS median_s
            FROM source_events e JOIN source_registry r ON r.id = e.source_id
            WHERE e.published_at IS NOT NULL AND e.detected_at > now() - make_interval(days => :d)
            GROUP BY r.name ORDER BY n DESC"""), {"d": max(1, min(days, 90))})).all()
    return [{"source": r.name, "events": r.n, "median_seconds": round(r.median_s)} for r in rows]
