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


@router.get("/llm-metrics")
async def llm_metrics(days: int = 7) -> list[dict]:
    """Per task: calls, validity rate, retries, latency percentiles and tokens/s."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(text("""
            SELECT task, model, count(*) AS n, avg(valid::int) AS valid_rate, avg(attempts) AS attempts,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                   sum(eval_tokens) * 1000.0 / nullif(sum(latency_ms), 0) AS tokens_per_s
            FROM llm_runs WHERE created_at > now() - make_interval(days => :d) GROUP BY task, model"""),
            {"d": max(1, min(days, 90))})).all()
    return [{"task": r.task, "model": r.model, "calls": r.n, "valid_rate": round(r.valid_rate, 3), "avg_attempts": round(r.attempts, 2),
             "p50_ms": round(r.p50), "p95_ms": round(r.p95), "tokens_per_s": round(r.tokens_per_s or 0, 1)} for r in rows]
