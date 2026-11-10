"""Source registry: register routes, toggle them independently, track health."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REGISTER_FIELDS = {
    "category", "access_method", "docs_url", "auth_type", "capabilities", "retention_days",
    "rate_limit", "cost_class", "cost_per_read", "priority", "poll_interval_seconds", "config",
}
JSON_FIELDS = {"capabilities", "rate_limit", "config"}
FAILURES_UNAVAILABLE = 3


async def register_source(s: AsyncSession, name: str, connector_type: str, **fields) -> str:
    """Create or update a route. Never changes `enabled` (a new route starts disabled)."""
    import json

    bad = set(fields) - REGISTER_FIELDS
    if bad:
        raise ValueError(f"unknown source fields: {sorted(bad)}")
    cols = ["name", "connector_type", *fields]
    vals = [":name", ":connector_type", *[f"CAST(:{k} AS jsonb)" if k in JSON_FIELDS else f":{k}" for k in fields]]
    params = {"name": name, "connector_type": connector_type,
              **{k: (json.dumps(v) if k in JSON_FIELDS else v) for k, v in fields.items()}}
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields) or "name = EXCLUDED.name"
    res = await s.execute(
        text(f"""INSERT INTO source_registry ({", ".join(cols)}) VALUES ({", ".join(vals)})
                 ON CONFLICT (name, connector_type) DO UPDATE SET {updates}, updated_at = now()
                 RETURNING id"""),
        params,
    )
    return str(res.scalar_one())


async def set_enabled(s: AsyncSession, name: str, connector_type: str, enabled: bool) -> bool:
    res = await s.execute(
        text("""UPDATE source_registry SET enabled = :e, updated_at = now(),
                health_status = CASE WHEN :e THEN 'RECOVERING' ELSE 'DISABLED' END,
                consecutive_failures = 0
                WHERE name = :n AND connector_type = :t"""),
        {"e": enabled, "n": name, "t": connector_type},
    )
    return bool(res.rowcount)


async def is_enabled(s: AsyncSession, name: str, connector_type: str) -> bool:
    """The scheduler asks this before every collection."""
    r = await s.execute(
        text("SELECT enabled FROM source_registry WHERE name = :n AND connector_type = :t"),
        {"n": name, "t": connector_type},
    )
    return bool(r.scalar_one_or_none())


async def record_success(s: AsyncSession, source_id: str) -> None:
    await s.execute(
        text("""UPDATE source_registry SET health_status = 'HEALTHY', consecutive_failures = 0,
                last_success_at = now(), updated_at = now() WHERE id = :id AND enabled"""),
        {"id": source_id},
    )


async def record_failure(s: AsyncSession, source_id: str, error: str) -> None:
    await s.execute(
        text("""UPDATE source_registry SET consecutive_failures = consecutive_failures + 1,
                last_error_at = now(), last_error = :e, updated_at = now(),
                health_status = CASE WHEN consecutive_failures + 1 >= :lim THEN 'UNAVAILABLE' ELSE 'DEGRADED' END
                WHERE id = :id AND enabled"""),
        {"id": source_id, "e": error[:2000], "lim": FAILURES_UNAVAILABLE},
    )
