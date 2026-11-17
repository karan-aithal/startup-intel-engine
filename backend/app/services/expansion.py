"""Watchlist expansion. Discovery (Phase 4/5) registers company feeds and careers pages DISABLED; this turns the best ones on, up to
AUTO_SOURCE_CAP (0 = none, until the owner chooses a number), and turns them back off when they fail or go idle. Ranking mirrors the
ATS rule: recently funded, then verified funding, then India-linked, then the emerging score, then oldest.

GitHub is skipped here: its organisations are polled by the GitHub API route (sync_github_orgs), not as per-company Atom feeds."""
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.quota import cfg
from app.services.registry import set_enabled

KINDS = ("blog_feed", "youtube", "substack", "medium", "careers")
GITHUB_ROUTE = "GitHub Orgs"


async def active_sources(s: AsyncSession) -> int:
    return (await s.execute(text("SELECT count(*) FROM company_sources WHERE status = 'active'"))).scalar_one()


async def expand_sources(s: AsyncSession, cap: int) -> int:
    room = cap - await active_sources(s)
    if room <= 0:
        return 0
    rows = (await s.execute(text("""SELECT cs.id, r.name, r.connector_type FROM company_sources cs
        JOIN companies c ON c.id = cs.company_id JOIN source_registry r ON r.id = cs.source_id
        WHERE cs.status = 'discovered' AND NOT r.enabled AND NOT c.muted AND cs.kind = ANY(:k)
          AND (c.watched OR coalesce(c.hot_until > now(), false) OR EXISTS (SELECT 1 FROM funding_rounds f WHERE f.company_id = c.id
               AND f.verification_status IN ('PARTIALLY_VERIFIED', 'VERIFIED') AND f.announced_at > now() - interval '180 days'))
        ORDER BY coalesce(c.hot_until > now(), false) DESC,
                 EXISTS (SELECT 1 FROM funding_rounds f WHERE f.company_id = c.id AND f.verification_status = 'VERIFIED') DESC,
                 c.is_india_linked DESC, c.emerging_global DESC NULLS LAST, cs.created_at
        LIMIT :n"""), {"k": list(KINDS), "n": room})).all()
    for r in rows:
        await set_enabled(s, r.name, r.connector_type, True)
        await s.execute(text("UPDATE company_sources SET status = 'active', updated_at = now() WHERE id = :i"), {"i": r.id})
    return len(rows)


async def demote_sources(s: AsyncSession) -> int:
    """Back to 'discovered' (route disabled, slot freed): the circuit has been opened, the company was muted, or 30 days of nothing new."""
    rows = (await s.execute(text("""SELECT cs.id, r.name, r.connector_type FROM company_sources cs JOIN source_registry r ON r.id = cs.source_id
        JOIN companies c ON c.id = cs.company_id WHERE cs.status = 'active'
          AND (c.muted OR r.consecutive_failures >= 3 OR (r.activity_score = 0 AND cs.updated_at < now() - interval '30 days'))"""))).all()
    for r in rows:
        await set_enabled(s, r.name, r.connector_type, False)
        await s.execute(text("UPDATE company_sources SET status = 'discovered', updated_at = now() WHERE id = :i"), {"i": r.id})
    return len(rows)


async def sync_github_orgs(s: AsyncSession) -> int:
    """Write the organisations to poll into the GitHub route's config (capped, best companies first). No-op until the route is registered."""
    ex = cfg()["expansion"]
    rows = (await s.execute(text("""SELECT DISTINCT ON (c.id) c.id AS company_id, regexp_replace(cs.url, '^https://github.com/([^/.]+)\\.atom$', '\\1') AS org,
            coalesce(c.hot_until > now(), false) AS hot, c.is_india_linked, c.emerging_global
        FROM company_sources cs JOIN companies c ON c.id = cs.company_id
        WHERE cs.kind = 'github' AND cs.url ~ '^https://github.com/[^/.]+\\.atom$' AND NOT c.muted
          AND (c.watched OR coalesce(c.hot_until > now(), false) OR EXISTS (SELECT 1 FROM funding_rounds f WHERE f.company_id = c.id
               AND f.verification_status IN ('PARTIALLY_VERIFIED', 'VERIFIED')))
        ORDER BY c.id, cs.created_at"""))).all()
    top = sorted(rows, key=lambda r: (not r.hot, not r.is_india_linked, -(r.emerging_global or 0), r.org))[: ex["github_orgs"]]
    orgs = [{"org": r.org, "company_id": str(r.company_id)} for r in top]
    res = await s.execute(text("UPDATE source_registry SET config = config || CAST(:c AS jsonb) WHERE name = :n AND connector_type = 'api'"),
                          {"c": json.dumps({"orgs": orgs}), "n": GITHUB_ROUTE})
    return len(orgs) if res.rowcount else 0
