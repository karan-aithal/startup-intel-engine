"""Company discovery loop: event -> candidate -> company (identity resolved) -> website with evidence -> investors/founders."""
import json
import re
from html.parser import HTMLParser

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.feed import USER_AGENT
from app.services.quality import cfg as quality_cfg

SUFFIX = re.compile(r"\b(inc|ltd|llc|pvt|private|limited|corp|co|technologies|technology|labs?)\b\.?", re.I)
NAME_VERB = re.compile(
    r"^(?:exclusive:\s*|\[[^\]]+\]\s*)?(.{2,60}?)\s+(?:raises?|raised|secures?|bags?|closes?|lands?|picks up|mops up|"
    r"gets|nabs|announces|emerges from stealth|out of stealth)\b", re.I)
LED_BY = re.compile(r"(?:co-)?led by ([^.;]+?)(?:\s+(?:with|and participation|to|for|in|at)\b|[.;]|$)", re.I)
FROM = re.compile(r"(?:participation from|backed by|investors? including|existing investors?) ([^.;]+?)(?:\s+(?:to|for|in|at)\b|[.;]|$)", re.I)
FOUNDER = [re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,2}),? (?:the )?(?:co-?founder|founder|CEO)"),
           re.compile(r"(?:co-?founder|founder)(?: and CEO| & CEO)?,? ([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})")]
TLDS = ("com", "io", "ai", "co", "in", "app")


def canonical(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", SUFFIX.sub("", name.lower())).split())


def extract_company(title: str | None) -> str | None:
    m = NAME_VERB.match((title or "").strip())
    if not m:
        return None
    name = re.split(r"\s+(?:in talks|plans?|set to|looking|looks|eyes|seeks?|aims?|wants?|is|to)\b.*", m.group(1))[0]  # "X in talks to raise"
    name = re.split(r"(?:-based|, a| , an) ", name)[-1].strip(" ,:")
    name = re.sub(r"^.*\bstartup\s+", "", name, flags=re.I)
    return name if name and name[0].isupper() and len(name.split()) <= 4 else None


def _split_names(s: str) -> list[str]:
    parts = re.split(r",\s*|\s+and\s+|\s*&\s*", s)
    return [p.strip() for p in parts if p.strip() and p.strip()[0].isupper() and len(p.split()) <= 5]


def extract_investors(text_: str) -> list[tuple[str, str]]:
    """(name, role) pairs. Only names in capitalised form are kept."""
    out = [(n, "lead") for m in LED_BY.finditer(text_) for n in _split_names(m.group(1))]
    out += [(n, "participant") for m in FROM.finditer(text_) for n in _split_names(m.group(1))]
    seen = {}
    for n, r in out:
        seen.setdefault(n, r)
    return list(seen.items())


def extract_founders(text_: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for rx in FOUNDER for m in rx.finditer(text_)))


async def add_candidate(s: AsyncSession, name: str, event_id: str | None = None, domain: str | None = None, evidence: dict | None = None) -> None:
    await s.execute(text("""INSERT INTO company_discovery_candidates (name, domain, source_event_id, evidence)
        SELECT :n, :d, :e, CAST(:ev AS jsonb) WHERE CAST(:e AS uuid) IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM company_discovery_candidates WHERE lower(name) = lower(:n) AND status = 'pending')
        ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING"""),
        {"n": name, "d": domain, "e": event_id, "ev": json.dumps(evidence or {})})


async def match_company(s: AsyncSession, name: str) -> tuple[str | None, int]:
    """Identity resolution: exact alias/canonical first, then trigram similarity. Returns (company_id, confidence)."""
    c = canonical(name)
    r = (await s.execute(text("""SELECT id, 100 AS conf, 0 AS pref FROM companies WHERE canonical_name = :c
        UNION ALL SELECT company_id, confidence, 1 FROM company_aliases WHERE alias = :c
        ORDER BY 2 DESC, 3 DESC LIMIT 1"""), {"c": c})).first()  # on a tie the alias wins: a merged-away company keeps its row, its alias points at the survivor
    if r:
        return str(r.id), r.conf
    r = (await s.execute(text("""SELECT id, round(similarity(canonical_name, :s) * 100)::int AS conf FROM companies
        WHERE similarity(canonical_name, :s) >= :sim AND coalesce(excluded_reason, '') NOT LIKE 'merged into%' ORDER BY 2 DESC LIMIT 1"""), {"s": c, "sim": quality_cfg()["entity_resolution"]["similarity"]})).first()
    return (str(r.id), r.conf) if r else (None, 0)


class _Title(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names, self._t = [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._t = tag == "title"
        if tag == "meta" and a.get("property") == "og:site_name" and a.get("content"):
            self.names.append(a["content"])

    def handle_endtag(self, tag):
        self._t = False

    def handle_data(self, d):
        if self._t:
            self.names.append(d)


async def identify_website(name: str, client: httpx.AsyncClient, domain: str | None = None) -> dict | None:
    """Try likely domains; accept one only if its page title / og:site_name carries the company name. Returns evidence."""
    stem = canonical(name).replace(" ", "")
    for dom in ([domain] if domain else [f"{stem}.{t}" for t in TLDS]):
        try:
            r = await client.get(f"https://{dom}", headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        p = _Title()
        p.feed(r.text[:200_000])
        hit = next((n.strip() for n in p.names if stem and stem in canonical(n).replace(" ", "")), None)
        if hit:
            return {"method": "domain guess + page title match" if not domain else "domain listed on portfolio page + title match",
                    "url": str(r.url), "matched_text": hit[:200], "domain": dom}
    return None


async def resolve_candidate(s: AsyncSession, cand, client: httpx.AsyncClient) -> str | None:
    """One pending candidate -> company row. Returns company id, or None when unresolved."""
    cid, conf = await match_company(s, cand.name)
    if cid is None:
        cid = str((await s.execute(text("""INSERT INTO companies (name, canonical_name, hot_until) VALUES (:n, :c,
            CASE WHEN CAST(:ev AS text) IS NOT NULL THEN now() + interval '90 days' END) RETURNING id"""),
            {"n": cand.name, "c": canonical(cand.name), "ev": str(cand.source_event_id) if cand.source_event_id else None})).scalar_one())
        conf = 100
    elif conf < 100:
        await s.execute(text("INSERT INTO company_aliases (company_id, alias, confidence) VALUES (:i, :a, :c) ON CONFLICT DO NOTHING"),
                        {"i": cid, "a": canonical(cand.name), "c": conf})
    site = (await s.execute(text("SELECT website_url FROM companies WHERE id = :i"), {"i": cid})).scalar_one()
    if not site:
        ev = await identify_website(cand.name, client, cand.domain)
        if ev:
            ev_id = (await s.execute(text("""INSERT INTO evidence (company_id, source_event_id, evidence_type, weight, url, snippet)
                VALUES (:c, :e, 'website_identity', 60, :u, :s) RETURNING id"""),
                {"c": cid, "e": cand.source_event_id, "u": ev["url"], "s": json.dumps(ev)})).scalar_one()
            await s.execute(text("UPDATE companies SET website_url = :u WHERE id = :i"), {"u": f"https://{ev['domain']}", "i": cid})
            await s.execute(text("""INSERT INTO company_domains (company_id, domain, is_primary, verified, evidence_id)
                VALUES (:c, :d, true, true, :e) ON CONFLICT (domain) DO NOTHING"""), {"c": cid, "d": ev["domain"], "e": ev_id})
    await s.execute(text("""UPDATE company_discovery_candidates SET status = 'accepted', matched_company_id = :c,
        match_confidence = :m WHERE id = :i"""), {"c": cid, "m": conf, "i": cand.id})
    if cand.source_event_id:
        from app.services.claims import (
            rebuild_claims,  # claims need the resolved company id; imported late (claims imports this module)
        )

        await rebuild_claims(s, str(cand.source_event_id))
    return cid


async def resolve_pending(sm, limit: int = 10, client: httpx.AsyncClient | None = None) -> int:
    async with sm() as s:
        rows = (await s.execute(text("SELECT id, name, domain, source_event_id FROM company_discovery_candidates WHERE status = 'pending' ORDER BY created_at LIMIT :n"),
                                {"n": limit})).all()
    c, done = client or httpx.AsyncClient(timeout=10), 0
    try:
        for r in rows:
            async with sm() as s:
                try:
                    await resolve_candidate(s, r, c)
                    done += 1
                except Exception:  # noqa: BLE001 - park it so one bad name can't block the queue
                    await s.rollback()
                    await s.execute(text("UPDATE company_discovery_candidates SET status = 'unresolved' WHERE id = :i"), {"i": r.id})
                await s.commit()
    finally:
        if client is None:
            await c.aclose()
    return done
