"""Transactional outbox for notifications (alert_deliveries).

Write the row in the same transaction as the fact that triggers the alert; a notification
worker then claims and delivers it, retrying with backoff. No alert is lost on a crash.
"""
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.queue import backoff_seconds


async def add_alert(
    s: AsyncSession, dedup_key: str, payload: dict, *, topic: str | None = None,
    channel: str = "telegram", company_id: str | None = None,
    funding_round_id: str | None = None, job_posting_id: str | None = None,
) -> str | None:
    """Queue an alert. Returns None if the same (channel, dedup_key) was already queued."""
    res = await s.execute(
        text("""INSERT INTO alert_deliveries (channel, topic, payload, dedup_key, company_id, funding_round_id, job_posting_id)
                VALUES (:ch, :topic, CAST(:p AS jsonb), :k, :c, :f, :j)
                ON CONFLICT (channel, dedup_key) DO NOTHING RETURNING id"""),
        {"ch": channel, "topic": topic, "p": json.dumps(payload, default=str), "k": dedup_key,
         "c": company_id, "f": funding_round_id, "j": job_posting_id},
    )
    r = res.scalar_one_or_none()
    return str(r) if r else None


async def claim_pending(s: AsyncSession, *, channel: str = "telegram", limit: int = 10, lease_s: int = 120) -> list[dict[str, Any]]:
    """Lease due deliveries (also re-claims ones stuck in 'sending' after a crash)."""
    res = await s.execute(
        text("""
        UPDATE alert_deliveries SET status = 'sending', attempts = attempts + 1,
               lease_expires_at = now() + make_interval(secs => :lease)
        WHERE id IN (
            SELECT id FROM alert_deliveries
            WHERE channel = :ch AND (
                (status IN ('pending','failed') AND next_attempt_at <= now())
                OR (status = 'sending' AND lease_expires_at < now()))
            ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT :n)
        RETURNING id, topic, payload, attempts, max_attempts
        """),
        {"ch": channel, "lease": lease_s, "n": limit},
    )
    return [dict(r) for r in res.mappings()]


async def mark_sent(s: AsyncSession, delivery_id: str, external_message_id: str | None = None) -> None:
    await s.execute(
        text("""UPDATE alert_deliveries SET status = 'sent', sent_at = now(), lease_expires_at = NULL,
                external_message_id = :m WHERE id = :id"""),
        {"id": delivery_id, "m": external_message_id},
    )


async def mark_failed(s: AsyncSession, delivery_id: str, error: str) -> str:
    row = (await s.execute(text("SELECT attempts, max_attempts FROM alert_deliveries WHERE id = :id"), {"id": delivery_id})).one()
    dead = row.attempts >= row.max_attempts
    await s.execute(
        text("""UPDATE alert_deliveries SET status = :st, last_error = :e, lease_expires_at = NULL,
                next_attempt_at = now() + make_interval(secs => :d) WHERE id = :id"""),
        {"st": "dead" if dead else "failed", "e": error[:2000], "d": backoff_seconds(row.attempts), "id": delivery_id},
    )
    return "dead" if dead else "failed"
