"""ATS board detection, registration under the growth cap, hot-company polling, Common Crawl seeding, portfolio pages.

Add a provider: put its endpoint in connectors.api.ATS_URLS, its normalizer in AtsAdapter, and a DETECT regex here
(only after verifying the public endpoint in the provider's official docs). A vendor that needs more than one URL, a POST, or a token that
is not a single word goes in connectors/ats_vendors.py instead: it brings its own detect regex, board URL and fetch (J2, decision 102)."""
import json
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.api import ATS_URLS, AtsAdapter, board_url
from app.connectors.ats_vendors import VENDORS
from app.connectors.feed import USER_AGENT
from app.services.aggregators import is_aggregator
from app.services.registry import register_source, set_enabled
from app.services.safefetch import guarded_client

DETECT = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I),
}
NOT_TOKENS = {"embed", "jobs", "job_board", "static", "v1"}
HOT_POLL, NORMAL_POLL = 600, 1800
DOCS = {"greenhouse": "https://developers.greenhouse.io/job-board.html", "lever": "https://github.com/lever/postings-api",
        "ashby": "https://developers.ashbyhq.com", **{n: v.docs for n, v in VENDORS.items()}}
SMALL_BOARD = 5  # a board listing this many jobs or fewer is flagged, never rejected (career-ops lib/small-board.mjs: a demo tenant or a regional sub-board)
assert set(DETECT) == set(ATS_URLS)  # keep the two registries in step


def find_boards(html: str) -> list[tuple[str, str]]:
    out = []
    for provider, rx in DETECT.items():
        for tok in rx.findall(html):
            if tok.lower() not in NOT_TOKENS and (provider, tok) not in out:
                out.append((provider, tok))
    for name, v in VENDORS.items():
        for m in v.detect.finditer(html):
            tok = v.token(m)
            if tok and (name, tok) not in out:
                out.append((name, tok))
    return out


async def watched_count(s: AsyncSession) -> int:
    return (await s.execute(text("SELECT count(*) FROM companies WHERE watched"))).scalar_one()


async def active_boards(s: AsyncSession) -> int:
    return (await s.execute(text("SELECT count(*) FROM ats_boards WHERE status = 'active'"))).scalar_one()


async def register_board(s: AsyncSession, company_id: str, provider: str, token: str, via: str, company_cap: int, board_cap: int,
                         client: httpx.AsyncClient | None = None, evidence: dict | None = None) -> str:
    """Verify the board returns jobs, persist it with its evidence, then let promotion decide whether it is polled.
    Returns 'registered' (polling now) | 'candidate' (stored, waiting for room under either cap) | 'invalid'."""
    try:
        fetched = await AtsAdapter(provider, token, client).fetch()
    except Exception:  # noqa: BLE001 - dead or private board
        return "invalid"
    name = f"{provider}:{token}"
    hot = (await s.execute(text("SELECT coalesce(hot_until > now(), false) FROM companies WHERE id = :i"), {"i": company_id})).scalar_one()
    await register_source(s, name, "api", category="hiring", access_method=f"{provider} job board API", docs_url=DOCS[provider],
                          poll_interval_seconds=VENDORS[provider].poll if provider in VENDORS else HOT_POLL if hot else NORMAL_POLL,
                          config={"kind": "ats", "provider": provider, "token": token})
    await s.execute(text("""INSERT INTO ats_boards (company_id, provider, board_token, board_url, status, discovered_via, evidence,
            verification_status, verified_at, last_job_count)
        VALUES (:c, :p, :t, :u, 'candidate', :v, CAST(:ev AS jsonb), 'verified', now(), :n)
        ON CONFLICT (provider, board_token) DO UPDATE SET company_id = coalesce(ats_boards.company_id, :c),
            verification_status = 'verified', verified_at = now(), last_job_count = :n,
            evidence = CASE WHEN ats_boards.evidence = '{}'::jsonb THEN CAST(:ev AS jsonb)
                ELSE ats_boards.evidence || jsonb_build_object('small_board', CAST(:small AS boolean)) END"""),
        {"c": company_id, "p": provider, "t": token, "u": board_url(provider, token), "v": via, "n": len(fetched.items),
         "ev": json.dumps({**(evidence or {"method": via}), "small_board": len(fetched.items) <= SMALL_BOARD}),
         "small": len(fetched.items) <= SMALL_BOARD})
    await promote_candidates(s, company_cap, board_cap, client)
    return "registered" if (await s.execute(text("SELECT status = 'active' FROM ats_boards WHERE provider = :p AND board_token = :t"),
                                            {"p": provider, "t": token})).scalar_one() else "candidate"


async def promote_candidates(s: AsyncSession, company_cap: int, board_cap: int, client: httpx.AsyncClient | None = None, limit: int = 100) -> int:
    """Enable stored candidate boards while BOTH limits have room: companies auto-watched and active auto boards.
    Priority: recently funded, then India-linked, then oldest. Boards without a company are never promoted (seed list only)."""
    rows = (await s.execute(text("""SELECT b.id, b.provider, b.board_token, b.company_id, b.verification_status, c.watched,
            coalesce(c.hot_until > now(), false) AS hot, c.is_india_linked
        FROM ats_boards b JOIN companies c ON c.id = b.company_id
        WHERE b.status = 'candidate' AND NOT c.muted ORDER BY hot DESC, c.is_india_linked DESC, b.created_at LIMIT :n"""), {"n": limit})).all()
    boards, companies, promoted = await active_boards(s), await watched_count(s), 0
    for r in rows:
        if boards >= board_cap:
            break
        if not r.watched and companies >= company_cap:
            continue
        if r.verification_status != "verified":  # Common Crawl style candidates are verified only now
            try:
                await AtsAdapter(r.provider, r.board_token, client).fetch()
            except Exception:  # noqa: BLE001
                await s.execute(text("UPDATE ats_boards SET verification_status = 'failed' WHERE id = :i"), {"i": r.id})
                continue
            await s.execute(text("UPDATE ats_boards SET verification_status = 'verified', verified_at = now() WHERE id = :i"), {"i": r.id})
        await set_enabled(s, f"{r.provider}:{r.board_token}", "api", True)
        reason = "hot company" if r.hot else "india-linked" if r.is_india_linked else "queue order"
        await s.execute(text("UPDATE ats_boards SET status = 'active', promoted_at = now(), promotion_reason = :r WHERE id = :i"), {"r": reason, "i": r.id})
        await s.execute(text("UPDATE companies SET watched = true WHERE id = :i"), {"i": r.company_id})
        boards += 1
        companies += 0 if r.watched else 1
        promoted += 1
    return promoted


async def demote_stale(s: AsyncSession) -> int:
    """Active boards that keep failing, or stay empty for 60 days, go back to candidates and free their slot."""
    rows = (await s.execute(text("""SELECT b.id, b.company_id, b.provider || ':' || b.board_token AS name,
            CASE WHEN r.consecutive_failures >= 3 THEN 'demoted: 3 consecutive failures' ELSE 'demoted: no jobs for 60 days' END AS why
        FROM ats_boards b JOIN source_registry r ON r.name = b.provider || ':' || b.board_token AND r.connector_type = 'api'
        WHERE b.status = 'active' AND (r.consecutive_failures >= 3 OR (b.last_job_count = 0 AND b.promoted_at < now() - interval '60 days'))"""))).all()
    for r in rows:
        await set_enabled(s, r.name, "api", False)
        await s.execute(text("UPDATE ats_boards SET status = 'candidate', promoted_at = NULL, promotion_reason = :w WHERE id = :i"), {"w": r.why, "i": r.id})
        await s.execute(text("""UPDATE companies SET watched = false WHERE id = :c
            AND NOT EXISTS (SELECT 1 FROM ats_boards WHERE company_id = :c AND status = 'active')"""), {"c": r.company_id})
    # keep the small-board flag in step with the latest poll: advisory, it never changes whether a board is polled (career-ops small-board.mjs)
    await s.execute(text("""UPDATE ats_boards SET evidence = coalesce(evidence, '{}'::jsonb) || jsonb_build_object('small_board', last_job_count <= :n)
        WHERE status = 'active' AND last_job_count IS NOT NULL AND (evidence->'small_board') IS DISTINCT FROM to_jsonb(last_job_count <= CAST(:n AS integer))"""),
        {"n": SMALL_BOARD})
    return len(rows)


async def detect_for_company(s: AsyncSession, company_id: str, client: httpx.AsyncClient, company_cap: int, board_cap: int) -> list[str]:
    """Look for ATS links on the careers page (or homepage) and register verified boards."""
    row = (await s.execute(text("SELECT website_url, careers_url FROM companies WHERE id = :i"), {"i": company_id})).one()
    results = []
    for url in dict.fromkeys(u for u in (row.careers_url, row.website_url) if u and not is_aggregator(u)):  # a job aggregator is not the company's own page
        try:
            r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError:
            continue
        for provider, tok in find_boards(r.text if r.status_code == 200 else ""):
            ev = {"method": "link on careers/home page", "found_on": str(r.url), "provider": provider, "token": tok}
            results.append(f"{provider}:{tok}={await register_board(s, company_id, provider, tok, 'careers page link', company_cap, board_cap, client, ev)}")
        if results:
            break
    return results


async def detect_pending(sm, company_cap: int, board_cap: int, limit: int = 5, client: httpx.AsyncClient | None = None) -> int:
    """Companies with a website and no board yet, not scanned for ATS before."""
    async with sm() as s:
        ids = (await s.execute(text("""SELECT id FROM companies WHERE website_url IS NOT NULL AND NOT muted
            AND coalesce(meta, '{}'::jsonb)->'ats_scanned_at' IS NULL
            AND coalesce(meta, '{}'::jsonb)->'feeds_discovered_at' IS NOT NULL LIMIT :n"""), {"n": limit})).scalars().all()
    c, n = client or guarded_client(timeout=15), 0
    try:
        for cid in ids:
            async with sm() as s:
                try:
                    n += len(await detect_for_company(s, str(cid), c, company_cap, board_cap))
                except Exception:  # noqa: BLE001
                    await s.rollback()
                await s.execute(text("UPDATE companies SET meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('ats_scanned_at', now()::text) WHERE id = :i"), {"i": cid})
                await s.commit()
    finally:
        if client is None:
            await c.aclose()
    return n


async def refresh_hot_polling(s: AsyncSession) -> None:
    """Newly funded companies are polled every 10 min for 90 days; everyone else back to the normal rate."""
    for hot, interval in ((True, HOT_POLL), (False, NORMAL_POLL)):
        await s.execute(text("""UPDATE source_registry r SET poll_interval_seconds = :iv FROM ats_boards b JOIN companies c ON c.id = b.company_id
            WHERE r.name = b.provider || ':' || b.board_token AND r.connector_type = 'api'
            AND coalesce(c.hot_until > now(), false) = :hot AND r.poll_interval_seconds IS DISTINCT FROM :iv"""), {"iv": interval, "hot": hot})


async def seed_from_common_crawl(s: AsyncSession, client: httpx.AsyncClient, per_provider: int = 2000) -> int:
    """Index queries only (no WARC downloads). Boards are stored as candidates; nothing is polled until a company claims one."""
    crawls = (await client.get("https://index.commoncrawl.org/collinfo.json")).json()
    api, new = crawls[0]["cdx-api"], 0
    for provider, host in (("greenhouse", "boards.greenhouse.io"), ("lever", "jobs.lever.co"), ("ashby", "jobs.ashbyhq.com")):
        r = await client.get(api, params={"url": f"{host}/*", "output": "json", "fl": "url", "limit": per_provider})
        toks = {t for line in r.text.splitlines() if line.startswith("{")
                for t in DETECT[provider].findall(json.loads(line)["url"]) if t.lower() not in NOT_TOKENS}
        for t in toks:
            res = await s.execute(text("""INSERT INTO ats_boards (provider, board_token, board_url, status, discovered_via)
                VALUES (:p, :t, :u, 'candidate', 'commoncrawl') ON CONFLICT (provider, board_token) DO NOTHING"""),
                {"p": provider, "t": t, "u": board_url(provider, t)})
            new += res.rowcount
    return new


PORTFOLIO_SKIP = ("linkedin.", "twitter.", "x.com", "facebook.", "instagram.", "youtube.", "medium.", "github.", "crunchbase.")


async def discover_portfolio(s: AsyncSession, url: str, client: httpx.AsyncClient) -> int:
    """Outbound links on a VC/accelerator portfolio page become candidates (name from domain), with the page as evidence."""
    from app.services.discovery import _Links

    r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    r.raise_for_status()
    p, base, n, seen = _Links(), urlparse(str(r.url)).netloc, 0, set()
    p.feed(r.text)
    from app.services.companies import add_candidate

    for href, _ in p.anchors:
        u = urlparse(href)
        host = u.netloc.lower().removeprefix("www.")
        if u.scheme.startswith("http") and host and host != base.removeprefix("www.") and host not in seen and not any(x in host for x in PORTFOLIO_SKIP):
            seen.add(host)
            await add_candidate(s, host.split(".")[0].replace("-", " ").title(), None, host, {"found_on": str(r.url), "href": href, "via": "portfolio page"})
            n += 1
    return n
