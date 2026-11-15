"""ATS board detection, registration under the growth cap, hot-company polling, Common Crawl seeding, portfolio pages.

Add a provider: put its endpoint in connectors.api.ATS_URLS, its normalizer in AtsAdapter, and a DETECT regex here
(only after verifying the public endpoint in the provider's official docs)."""
import json
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.api import ATS_URLS, AtsAdapter
from app.connectors.feed import USER_AGENT
from app.services.registry import register_source, set_enabled

DETECT = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I),
}
NOT_TOKENS = {"embed", "jobs", "job_board", "static", "v1"}
HOT_POLL, NORMAL_POLL = 600, 1800
DOCS = {"greenhouse": "https://developers.greenhouse.io/job-board.html", "lever": "https://github.com/lever/postings-api",
        "ashby": "https://developers.ashbyhq.com"}
assert set(DETECT) == set(ATS_URLS)  # keep the two registries in step


def find_boards(html: str) -> list[tuple[str, str]]:
    out = []
    for provider, rx in DETECT.items():
        for tok in rx.findall(html):
            if tok.lower() not in NOT_TOKENS and (provider, tok) not in out:
                out.append((provider, tok))
    return out


async def watched_count(s: AsyncSession) -> int:
    return (await s.execute(text("SELECT count(*) FROM companies WHERE watched"))).scalar_one()


async def register_board(s: AsyncSession, company_id: str, provider: str, token: str, via: str, cap: int,
                         client: httpx.AsyncClient | None = None) -> str:
    """Verify the board returns jobs, record it, and start polling it if the company fits under the cap.
    Returns 'registered' | 'candidate' (over cap) | 'invalid'."""
    try:
        await AtsAdapter(provider, token, client).fetch()
    except Exception:  # noqa: BLE001 - dead or private board
        return "invalid"
    name = f"{provider}:{token}"
    watched = (await s.execute(text("SELECT watched FROM companies WHERE id = :i"), {"i": company_id})).scalar_one()
    room = watched or await watched_count(s) < cap
    hot = (await s.execute(text("SELECT coalesce(hot_until > now(), false) FROM companies WHERE id = :i"), {"i": company_id})).scalar_one()
    await register_source(s, name, "api", category="hiring", access_method=f"{provider} job board API", docs_url=DOCS[provider],
                          poll_interval_seconds=HOT_POLL if hot else NORMAL_POLL,
                          config={"kind": "ats", "provider": provider, "token": token})
    await s.execute(text("""INSERT INTO ats_boards (company_id, provider, board_token, board_url, status, discovered_via)
        VALUES (:c, :p, :t, :u, :st, :v) ON CONFLICT (provider, board_token)
        DO UPDATE SET company_id = coalesce(ats_boards.company_id, :c), status = :st"""),
        {"c": company_id, "p": provider, "t": token, "u": ATS_URLS[provider].format(t=token), "st": "active" if room else "candidate", "v": via})
    if room:
        await set_enabled(s, name, "api", True)
        await s.execute(text("UPDATE companies SET watched = true WHERE id = :i"), {"i": company_id})
        return "registered"
    return "candidate"


async def detect_for_company(s: AsyncSession, company_id: str, client: httpx.AsyncClient, cap: int) -> list[str]:
    """Look for ATS links on the careers page (or homepage) and register verified boards."""
    row = (await s.execute(text("SELECT website_url, careers_url FROM companies WHERE id = :i"), {"i": company_id})).one()
    results = []
    for url in dict.fromkeys(u for u in (row.careers_url, row.website_url) if u):
        try:
            r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError:
            continue
        for provider, tok in find_boards(r.text if r.status_code == 200 else ""):
            results.append(f"{provider}:{tok}={await register_board(s, company_id, provider, tok, 'careers page link', cap, client)}")
        if results:
            break
    return results


async def detect_pending(sm, cap: int, limit: int = 5, client: httpx.AsyncClient | None = None) -> int:
    """Companies with a website and no board yet, not scanned for ATS before."""
    async with sm() as s:
        ids = (await s.execute(text("""SELECT id FROM companies WHERE website_url IS NOT NULL AND NOT muted
            AND coalesce(meta, '{}'::jsonb)->'ats_scanned_at' IS NULL
            AND coalesce(meta, '{}'::jsonb)->'feeds_discovered_at' IS NOT NULL LIMIT :n"""), {"n": limit})).scalars().all()
    c, n = client or httpx.AsyncClient(timeout=15), 0
    try:
        for cid in ids:
            async with sm() as s:
                try:
                    n += len(await detect_for_company(s, str(cid), c, cap))
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
                {"p": provider, "t": t, "u": ATS_URLS[provider].format(t=t)})
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
