"""Adaptive acquisition: which sources are due and in what order, how long until the next poll, the circuit breaker, activity scores.

Interval = base interval x an activity factor (busy sources are polled more often, idle ones less), clamped, with jitter.
Hot-company ATS boards never slow down. After N consecutive failures the circuit opens: no polling until a cooldown that doubles with
each further failure (capped), then one trial. A source flagged `fallback_for` another is polled only while that primary is down."""
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.quota import cfg

DUE_SQL = text("""
    SELECT r.name, r.config FROM source_registry r
    WHERE r.enabled AND r.connector_type NOT IN ('stream', 'discovery')
      AND coalesce(r.next_poll_at, coalesce((SELECT max(started_at) FROM source_runs WHERE source_id = r.id), 'epoch')
                                   + make_interval(secs => coalesce(r.poll_interval_seconds, 900))) <= now()
      AND (r.circuit_open_until IS NULL OR r.circuit_open_until <= now())
      AND (r.config->>'fallback_for' IS NULL OR EXISTS (SELECT 1 FROM source_registry p WHERE p.name = r.config->>'fallback_for'
           AND p.enabled AND (p.circuit_open_until > now() OR p.health_status = 'UNAVAILABLE')))
    ORDER BY r.priority DESC, coalesce(r.activity_score, 0) DESC, r.name""")


def factor(score: int | None) -> float:
    c = cfg()["scheduling"]
    if score is None:
        return 1.0
    if score == 0:
        return float(c["idle_factor"])
    return next((float(f) for t, f in c["factors"] if score >= t), 1.0)


def next_interval(base: int, score: int | None, hot: bool = False, retry_after_s: float | None = None, rnd: float | None = None) -> float:
    c = cfg()["scheduling"]
    f = min(factor(score), 1.0) if hot else factor(score)
    iv = min(max(base * f, c["min_interval_seconds"]), max(c["max_interval_seconds"], base))
    iv *= 1 + c["jitter"] * (2 * (random.random() if rnd is None else rnd) - 1)
    return max(iv, retry_after_s or 0)


async def schedule_next(s: AsyncSession, source_id: str, retry_after_s: float | None = None, now: datetime | None = None) -> None:
    r = (await s.execute(text("""SELECT r.poll_interval_seconds, r.activity_score, r.config,
            coalesce((SELECT c.hot_until > now() FROM ats_boards b JOIN companies c ON c.id = b.company_id
                      WHERE b.provider || ':' || b.board_token = r.name LIMIT 1), false) AS hot
        FROM source_registry r WHERE r.id = :i"""), {"i": source_id})).one()
    iv = next_interval(r.poll_interval_seconds or 900, r.activity_score, bool(r.hot), retry_after_s)
    await s.execute(text("UPDATE source_registry SET next_poll_at = :t WHERE id = :i"),
                    {"t": (now or datetime.now(UTC)) + timedelta(seconds=iv), "i": source_id})


async def open_circuit_if_needed(s: AsyncSession, source_id: str, now: datetime | None = None, rnd: float | None = None) -> bool:
    """Call after record_failure. Opens (or lengthens) the circuit once consecutive failures reach the threshold."""
    b = cfg()["scheduling"]["breaker"]
    n = (await s.execute(text("SELECT consecutive_failures FROM source_registry WHERE id = :i"), {"i": source_id})).scalar_one()
    if n < b["failures"]:
        return False
    cool = min(b["cooldown_seconds"] * 2 ** (n - b["failures"]), b["max_cooldown_seconds"])
    cool *= 1 + cfg()["scheduling"]["jitter"] * (2 * (random.random() if rnd is None else rnd) - 1)
    await s.execute(text("UPDATE source_registry SET circuit_open_until = :t, next_poll_at = :t WHERE id = :i"),
                    {"t": (now or datetime.now(UTC)) + timedelta(seconds=cool), "i": source_id})
    return True


async def update_activity(s: AsyncSession) -> int:
    """Hourly: score each source from the new items its successful runs produced in the window. No runs in the window -> null."""
    a = cfg()["scheduling"]["activity"]
    res = await s.execute(text("""UPDATE source_registry r SET activity_score = LEAST(100, round(:k * log(2, 1 + x.n / CAST(:d AS numeric))))
        FROM (SELECT source_id, sum(items_new) AS n FROM source_runs WHERE status = 'ok'
              AND started_at > now() - make_interval(days => :d) GROUP BY source_id) x WHERE x.source_id = r.id"""),
        {"k": a["scale"], "d": a["window_days"]})
    return res.rowcount
