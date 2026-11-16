"""Phase 7 correlation: rule-based company signals and the emerging-startup score. Signals describe sequences, never causes.

Jobs are source events from ATS routes (`provider:token`), tied to a company through ats_boards. Dates use the job's own
published_at, not detected_at: a board registered today would otherwise make every old job look new."""
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scoring

JOBS = """SELECT e.id, e.published_at AS at, l.result->>'seniority' AS seniority
    FROM source_events e JOIN source_registry r ON r.id = e.source_id
    JOIN ats_boards b ON r.name = b.provider || ':' || b.board_token AND r.connector_type = 'api'
    LEFT JOIN llm_runs l ON l.source_event_id = e.id AND l.task = 'job' AND l.valid
    WHERE e.event_type = 'job' AND b.company_id = :c AND e.published_at IS NOT NULL"""


async def _signal(s: AsyncSession, company_id: str, kind: str, key: str, at: datetime, data: dict) -> None:
    await s.execute(text("""INSERT INTO company_signals (company_id, signal_type, signal_key, occurred_at, data)
        VALUES (:c, :t, :k, :a, CAST(:d AS jsonb)) ON CONFLICT (company_id, signal_type, signal_key) DO NOTHING"""),
        {"c": company_id, "t": kind, "k": key, "a": at, "d": json.dumps(data | {"note": "sequence observed; no causal claim"})})


async def correlate_company(s: AsyncSession, company_id: str, now: datetime | None = None) -> None:
    now, co = now or datetime.now(UTC), scoring.cfg()["correlation"]
    jobs = (await s.execute(text(JOBS), {"c": company_id})).all()
    rounds = (await s.execute(text("""SELECT id, coalesce(announced_at, first_detected_at) AS at FROM funding_rounds
        WHERE company_id = :c AND verification_status IN ('PARTIALLY_VERIFIED', 'VERIFIED')"""), {"c": company_id})).all()
    for r in rounds:  # funding followed by hiring
        after = sorted(j.at for j in jobs if r.at < j.at <= r.at + timedelta(days=co["hiring_window_days"]))
        if after:
            await _signal(s, company_id, "funding_then_hiring", str(r.id), after[0],
                          {"round_id": str(r.id), "jobs": len(after), "window_days": co["hiring_window_days"]})
    recent = [j for j in jobs if now - timedelta(days=co["surge_days"]) <= j.at <= now]
    month = f"{now:%Y-%m}"
    if len(recent) >= co["surge_jobs"]:
        await _signal(s, company_id, "hiring_surge", month, now, {"jobs": len(recent), "days": co["surge_days"]})
    senior = [j for j in recent if j.seniority in co["senior_levels"]]
    if len(senior) >= co["senior_roles"]:
        await _signal(s, company_id, "senior_hiring", month, now, {"roles": len(senior), "days": co["surge_days"]})
    # an investor that appears in a later verified round but in none of the earlier ones
    for r in (await s.execute(text("""SELECT ci.investor_id, f.id AS round_id, f.announced_at FROM company_investors ci
        JOIN funding_rounds f ON f.id = ci.funding_round_id WHERE ci.company_id = :c AND f.verification_status = 'VERIFIED'
        AND EXISTS (SELECT 1 FROM funding_rounds o WHERE o.company_id = :c AND o.verification_status = 'VERIFIED' AND o.announced_at < f.announced_at)
        AND NOT EXISTS (SELECT 1 FROM company_investors x JOIN funding_rounds o ON o.id = x.funding_round_id
            WHERE x.company_id = :c AND x.investor_id = ci.investor_id AND o.announced_at < f.announced_at)"""), {"c": company_id})).all():
        await _signal(s, company_id, "new_investor", str(r.investor_id), r.announced_at, {"round_id": str(r.round_id)})
    await _emerging(s, company_id, jobs, now)


async def _emerging(s: AsyncSession, company_id: str, jobs: list, now: datetime) -> None:
    c = (await s.execute(text("SELECT is_india_linked, created_at FROM companies WHERE id = :c"), {"c": company_id})).one()
    sig = (await s.execute(text("SELECT count(*) FROM company_signals WHERE company_id = :c AND occurred_at > :t"),
                           {"c": company_id, "t": now - timedelta(days=90)})).scalar_one()
    best, last = (await s.execute(text("""SELECT coalesce(max(confidence), 0), max(announced_at) FROM funding_rounds
        WHERE company_id = :c AND verification_status IN ('PARTIALLY_VERIFIED', 'VERIFIED')"""), {"c": company_id})).one()
    if not (sig or last):
        return  # nothing observed about this company yet: leave the score null instead of publishing a number made of defaults
    latest = max([x for x in [last, *(j.at for j in jobs)] if x], default=None)
    si, sg = scoring.emerging_scores(sig, (now - c.created_at).total_seconds() / 86400, bool(c.is_india_linked), best, latest, now)
    await s.execute(text("UPDATE companies SET emerging_india = :i, emerging_global = :g WHERE id = :c"), {"i": si, "g": sg, "c": company_id})


async def correlate_all(sm) -> int:
    async with sm() as s:
        ids = [str(r) for r in (await s.execute(text("""SELECT DISTINCT company_id FROM funding_rounds
            UNION SELECT company_id FROM ats_boards WHERE company_id IS NOT NULL"""))).scalars()]
    for cid in ids:
        async with sm() as s:
            await correlate_company(s, cid)
            await s.commit()
    return len(ids)
