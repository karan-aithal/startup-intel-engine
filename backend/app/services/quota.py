"""Quota manager: a persisted token bucket plus a hard window cap per provider (config/acquisition.yaml), and 429 blocking.

Persisted so a worker restart cannot hand a provider a fresh quota. Each `take` is its own short transaction that locks the
provider's row, so concurrent routes share one budget. Providers not listed in the config are not limited."""
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text

CONFIG = Path(__file__).resolve().parents[3] / "config" / "acquisition.yaml"


@lru_cache
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text())


class RateLimited(Exception):
    """Not a source failure: the provider (or our own quota for it) says wait. The scheduler defers; the breaker is not involved."""

    def __init__(self, wait_s: float, why: str):
        super().__init__(f"rate limited ({why}); retry in {wait_s:.0f}s")
        self.wait_s, self.why = max(wait_s, 1.0), why


class Limiter:
    def __init__(self, sm, provider: str):
        self.sm, self.provider = sm, provider

    async def take(self, cost: int = 1, now: datetime | None = None) -> None:
        """Spend `cost` tokens or raise RateLimited with the wait."""
        p = cfg()["providers"].get(self.provider)
        if p is None:
            return
        now = now or datetime.now(UTC)
        async with self.sm() as s:
            await s.execute(text("""INSERT INTO rate_limits (provider, tokens, refilled_at, window_started_at) VALUES (:p, :b, :n, :n)
                ON CONFLICT (provider) DO NOTHING"""), {"p": self.provider, "b": float(p["burst"]), "n": now})
            r = (await s.execute(text("SELECT tokens, refilled_at, window_started_at, window_used, blocked_until FROM rate_limits WHERE provider = :p FOR UPDATE"),
                                 {"p": self.provider})).one()
            tokens = min(float(p["burst"]), r.tokens + max((now - r.refilled_at).total_seconds(), 0) * p["per_second"])
            started, used = r.window_started_at, r.window_used
            if (now - started).total_seconds() >= p["window_seconds"]:
                started, used = now, 0
            wait, why = 0.0, ""
            if r.blocked_until and r.blocked_until > now:
                wait, why = (r.blocked_until - now).total_seconds(), "provider asked us to wait"
            elif p.get("cap") and used + cost > p["cap"]:
                wait, why = (started + timedelta(seconds=p["window_seconds"]) - now).total_seconds(), "window cap reached"
            elif tokens < cost:
                wait, why = (cost - tokens) / p["per_second"], "bucket empty"
            if not wait:
                tokens, used = tokens - cost, used + cost
            await s.execute(text("""UPDATE rate_limits SET tokens = :t, refilled_at = :n, window_started_at = :w, window_used = :u WHERE provider = :p"""),
                            {"t": tokens, "n": now, "w": started, "u": used, "p": self.provider})
            await s.commit()
        if wait:
            raise RateLimited(wait, why)

    async def block(self, wait_s: float, now: datetime | None = None) -> None:
        """The provider answered 429/403-with-limit: nothing is taken from this provider until the wait is over."""
        now = now or datetime.now(UTC)
        async with self.sm() as s:
            await s.execute(text("""INSERT INTO rate_limits (provider, tokens, blocked_until) VALUES (:p, 0, :u)
                ON CONFLICT (provider) DO UPDATE SET blocked_until = greatest(coalesce(rate_limits.blocked_until, :u), :u)"""),
                            {"p": self.provider, "u": now + timedelta(seconds=wait_s)})
            await s.commit()
