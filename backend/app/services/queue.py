"""Durable Postgres job queue: SKIP LOCKED claiming, leases, backoff, dead-letter, recovery."""
import random
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

BACKOFF_BASE_S = 15
BACKOFF_CAP_S = 3600


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff with jitter: 15s, 30s, 60s ... capped at 1h."""
    base = min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_CAP_S)
    return base * random.uniform(0.8, 1.2)


async def enqueue(
    s: AsyncSession, kind: str, payload: dict, idempotency_key: str,
    *, priority: int = 50, delay_s: float = 0, max_attempts: int = 5,
) -> str | None:
    """Insert a job. Returns its id, or None if (kind, idempotency_key) already exists."""
    row = await s.execute(
        text("""
        INSERT INTO job_queue (kind, payload, idempotency_key, priority, max_attempts, run_at)
        VALUES (:kind, CAST(:payload AS jsonb), :key, :prio, :maxa, now() + make_interval(secs => :delay))
        ON CONFLICT (kind, idempotency_key) DO NOTHING
        RETURNING id
        """),
        {"kind": kind, "payload": _json(payload), "key": idempotency_key, "prio": priority,
         "maxa": max_attempts, "delay": delay_s},
    )
    r = row.scalar_one_or_none()
    return str(r) if r else None


async def recover_abandoned(s: AsyncSession) -> int:
    """Jobs whose worker died (lease expired) go back to pending, or dead if out of attempts."""
    res = await s.execute(text("""
        UPDATE job_queue SET
          status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
          locked_by = NULL, lease_expires_at = NULL,
          last_error = coalesce(last_error, 'lease expired (worker died)'),
          finished_at = CASE WHEN attempts >= max_attempts THEN now() END
        WHERE status = 'running' AND lease_expires_at < now()
    """))
    return res.rowcount or 0


async def claim(
    s: AsyncSession, worker: str, *, kinds: list[str] | None = None, limit: int = 1, lease_s: int = 300,
) -> list[dict[str, Any]]:
    """Atomically lease up to `limit` due jobs. Concurrent workers never get the same job."""
    await recover_abandoned(s)
    res = await s.execute(
        text("""
        UPDATE job_queue SET status = 'running', locked_by = :w, attempts = attempts + 1,
               lease_expires_at = now() + make_interval(secs => :lease)
        WHERE id IN (
            SELECT id FROM job_queue
            WHERE status = 'pending' AND run_at <= now() AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(:kinds))
            ORDER BY priority, run_at
            FOR UPDATE SKIP LOCKED
            LIMIT :n)
        RETURNING id, kind, payload, idempotency_key, attempts, max_attempts
        """),
        {"w": worker, "lease": lease_s, "kinds": kinds, "n": limit},
    )
    return [dict(r) for r in res.mappings()]


async def extend_lease(s: AsyncSession, job_id: str, worker: str, lease_s: int = 300) -> bool:
    res = await s.execute(
        text("""UPDATE job_queue SET lease_expires_at = now() + make_interval(secs => :l)
                WHERE id = :id AND locked_by = :w AND status = 'running'"""),
        {"l": lease_s, "id": job_id, "w": worker},
    )
    return bool(res.rowcount)


async def complete(s: AsyncSession, job_id: str, worker: str) -> bool:
    res = await s.execute(
        text("""UPDATE job_queue SET status = 'done', finished_at = now(), locked_by = NULL,
                lease_expires_at = NULL WHERE id = :id AND locked_by = :w AND status = 'running'"""),
        {"id": job_id, "w": worker},
    )
    return bool(res.rowcount)


async def fail(s: AsyncSession, job_id: str, worker: str, error: str) -> str | None:
    """Retry with backoff, or dead-letter when attempts are exhausted. Returns the new status."""
    row = (await s.execute(
        text("SELECT attempts, max_attempts FROM job_queue WHERE id = :id AND locked_by = :w AND status = 'running'"),
        {"id": job_id, "w": worker},
    )).first()
    if row is None:
        return None
    attempts, max_attempts = row
    dead = attempts >= max_attempts
    await s.execute(
        text("""UPDATE job_queue SET status = :st, last_error = :err, locked_by = NULL, lease_expires_at = NULL,
                run_at = now() + make_interval(secs => :delay),
                finished_at = CASE WHEN :dead THEN now() END
                WHERE id = :id"""),
        {"st": "dead" if dead else "pending", "err": error[:2000], "delay": backoff_seconds(attempts),
         "dead": dead, "id": job_id},
    )
    return "dead" if dead else "pending"


async def heartbeat(s: AsyncSession, worker: str, info: dict | None = None) -> None:
    await s.execute(
        text("""INSERT INTO worker_heartbeats (worker_name, info) VALUES (:w, CAST(:i AS jsonb))
                ON CONFLICT (worker_name) DO UPDATE SET last_seen_at = now(), info = EXCLUDED.info"""),
        {"w": worker, "i": _json(info or {})},
    )


async def counts(s: AsyncSession) -> dict[str, int]:
    res = await s.execute(text("SELECT status, count(*) FROM job_queue GROUP BY status"))
    return {k: v for k, v in res.all()}


def _json(v: dict) -> str:
    import json

    return json.dumps(v, default=str)
