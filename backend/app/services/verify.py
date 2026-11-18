"""Phase 7 verification: unverified claims -> evidence -> funding_rounds, one company at a time. Idempotent: every pass recomputes a
company's rounds from its non-rejected claims, so a new article, a rejected claim or a changed config all converge on the next pass.

Independence: copies are one voice. Events that are `syndicated_copy` of each other, share a content hash, or come from the same
source form one syndication group; a group counts once however many articles it holds. Different outlets that wrote separate
articles (`same_event`) are independent. Known limit: two outlets rewriting one press release look independent here."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import scoring
from app.services.claims import _same_amount
from app.services.companies import canonical
from app.services.pipeline import INDIA

NIL = "00000000-0000-0000-0000-000000000000"


def _groups(rows: list[dict], pairs: list[tuple]) -> dict:
    """event id -> syndication group id (smallest member id), by union-find over shared source, content hash and syndicated_copy links."""
    parent = {r["eid"]: r["eid"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    first_by: dict = {}
    for r in rows:
        for key in (("src", r["source_id"]), ("hash", r["content_hash"])):
            if key[1] is not None:
                union(r["eid"], first_by.setdefault(key, r["eid"]))
    for a, b in pairs:
        union(a, b)
    members: dict = {}
    for e in parent:
        members.setdefault(find(e), []).append(e)
    return {e: min(map(str, m)) for m in members.values() for e in m}


def _etype(c: dict, company_canonical: str) -> str:
    """Evidence type of a claim's source. An SEC filing is official only when its issuer (named in the filing title) is this company:
    a Form D from another entity proves nothing about this one, so it is scored as an unlisted source."""
    t = scoring.evidence_type(c["source"])
    if t == "official_filing" and canonical(c["event_company"] or "") != canonical(company_canonical):
        return "community"
    return t


def cluster(claims: list[dict]) -> list[list[dict]]:
    """Claims that describe one round: same currency (or unknown), amount within tolerance, within the window of the cluster's first
    claim. A claim with no amount joins a cluster of the same stage; with neither amount nor stage it cannot be placed."""
    v = scoring.cfg()["verification"]
    win, out = timedelta(days=v["cluster_window_days"]), []
    for c in sorted(claims, key=lambda c: c["seen"]):
        if c["amount_value"] is None:
            continue
        for k in out:
            h = k[0]
            if (c["currency"] in (None, h["currency"]) or h["currency"] is None) and c["seen"] - h["seen"] <= win \
                    and abs(Decimal(str(c["amount_value"])) - Decimal(str(h["amount_value"]))) <= Decimal(str(h["amount_value"])) * Decimal(str(v["amount_tolerance"])):
                k.append(c)
                break
        else:
            out.append([c])
    for c in sorted(claims, key=lambda c: c["seen"]):
        if c["amount_value"] is not None or not c["stage"]:
            continue
        for k in out:
            if any(x["stage"] == c["stage"] for x in k) and c["seen"] - k[0]["seen"] <= win:
                k.append(c)
                break
        else:
            out.append([c])
    return out


def _stage(k: list[dict]) -> str | None:
    s = [c["stage"] for c in k if c["stage"]]
    return max(set(s), key=s.count) if s else None


async def _load(s: AsyncSession, company_id: str) -> list[dict]:
    rows = (await s.execute(text("""
        SELECT c.id, c.source_event_id AS eid, c.amount_value, c.currency, c.stage, c.is_rumour, c.status, c.funding_round_id,
               c.evidence, e.title, e.company_name AS event_company, e.canonical_url, e.content_hash, e.source_id, e.detected_at, e.raw_metadata, r.name AS source,
               coalesce(e.published_at, e.detected_at) AS seen
        FROM funding_round_claims c JOIN source_events e ON e.id = c.source_event_id JOIN source_registry r ON r.id = e.source_id
        WHERE c.company_id = :c AND c.status IN ('unverified', 'verified') ORDER BY seen, c.id"""), {"c": company_id})).mappings().all()
    return [dict(r) for r in rows]


async def verify_company(s: AsyncSession, company_id: str, now: datetime | None = None) -> dict:
    now, v = now or datetime.now(UTC), scoring.cfg()["verification"]
    claims = await _load(s, company_id)
    co = (await s.execute(text("SELECT is_india_linked, canonical_name FROM companies WHERE id = :c"), {"c": company_id})).one()
    ids = [c["eid"] for c in claims]
    pairs = [(r.event_id, r.related_event_id) for r in (await s.execute(text("""SELECT event_id, related_event_id FROM event_relationships
        WHERE kind = 'syndicated_copy' AND (event_id = ANY(:i) OR related_event_id = ANY(:i))"""), {"i": ids})).all()] if ids else []
    group = _groups(claims, pairs)
    plans = []
    for k in cluster(claims):
        ev = [{"claim": c, "group": group[c["eid"]], "type": (t := _etype(c, co.canonical_name)), "weight": scoring.weight(t), "supports": True}
              for c in k]
        a = scoring.assess(ev, rumour_only=all(c["is_rumour"] for c in k))
        if any(scoring.evidence_type(c["source"]) == "official_filing" and e["type"] != "official_filing"
               for c, e in zip(k, ev, strict=True)):
            a["flags"].append("issuer_mismatch")
        if any((c["evidence"] or {}).get("alternatives") for c in k):
            a["flags"].append("extraction_disagreement")
        if not any(c["amount_value"] is not None for c in k):
            a["flags"].append("no_amount")
        plans.append({**a, "stage": _stage(k), "claims": k, "evidence": ev, "contradicts": [], "round_ids": {c["funding_round_id"] for c in k if c["funding_round_id"]}})
    live = [p for p in plans if p["status"] != "UNVERIFIED"]
    for i, a in enumerate(live):  # contradiction: different amounts, same currency, same window, both at least partially verified
        for b in live[i + 1:]:
            ha, hb = a["claims"][0], b["claims"][0]
            if ha["amount_value"] is not None and hb["amount_value"] is not None and ha["currency"] == hb["currency"] \
                    and abs(ha["seen"] - hb["seen"]) <= timedelta(days=v["cluster_window_days"]) and not _same_amount(ha["amount_value"], hb["amount_value"]):
                if a["stage"] and a["stage"] == b["stage"]:
                    scoring.decide_conflict(a, b)  # same stage: they describe one round, so one of the amounts is wrong
                else:  # a seed extension and a Series A can both be real: keep both, flag for review, change nothing else
                    for x in (a, b):
                        if "possible_conflicting_amount" not in x["flags"]:
                            x["flags"].append("possible_conflicting_amount")
    for p in plans:
        newest = max(c["seen"] for c in p["claims"])
        if p["status"] == "PARTIALLY_VERIFIED" and now - newest > timedelta(days=v["expire_partial_days"]):
            p["status"] = "EXPIRED"
    counts = {}
    for p in plans:
        if p["status"] == "UNVERIFIED" and not p["round_ids"]:
            await s.execute(text("UPDATE funding_round_claims SET funding_round_id = NULL WHERE id = ANY(:i) AND funding_round_id IS NOT NULL"),
                            {"i": [c["id"] for c in p["claims"]]})
            counts["UNVERIFIED"] = counts.get("UNVERIFIED", 0) + 1
            continue
        await _write_round(s, company_id, p, bool(co.is_india_linked), now)
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    # a round whose claims were all rejected since (e.g. the model relabelled the article as not funding) is no longer supported
    await s.execute(text("""UPDATE funding_rounds f SET verification_status = 'UNVERIFIED', confidence = 0, verified_at = NULL,
        risk_flags = '["all_claims_rejected"]'::jsonb, updated_at = now()
        WHERE f.company_id = :c AND f.verification_status NOT IN ('UNVERIFIED') AND NOT EXISTS (
            SELECT 1 FROM funding_round_claims x WHERE x.funding_round_id = f.id AND x.status IN ('unverified', 'verified'))"""), {"c": company_id})
    await _reconcile_people(s, company_id)
    return counts


async def _reconcile_people(s: AsyncSession, company_id: str) -> None:
    """Promotion is reversible. A row Phase 7 wrote (promoted_by = 'phase7') survives only while a non-rejected person claim still backs it
    through a funding claim whose round is VERIFIED right now. Rows without the marker (manual, other jobs) are never touched, and a
    relationship backed by another VERIFIED round keeps its own row for that round."""
    backed = """EXISTS (SELECT 1 FROM {t} x JOIN funding_round_claims fc ON fc.source_event_id = x.source_event_id
        JOIN funding_rounds f ON f.id = fc.funding_round_id
        WHERE x.company_id = :c AND x.status <> 'rejected' AND fc.status IN ('unverified', 'verified') AND f.verification_status = 'VERIFIED'{extra})"""
    await s.execute(text(f"""DELETE FROM company_investors ci USING investors i WHERE ci.company_id = :c AND ci.promoted_by = 'phase7' AND i.id = ci.investor_id
        AND NOT {backed.format(t="company_investor_claims", extra=" AND x.canonical_name = i.canonical_name AND fc.funding_round_id = ci.funding_round_id")}"""), {"c": company_id})
    await s.execute(text(f"""DELETE FROM founders fo WHERE fo.company_id = :c AND fo.promoted_by = 'phase7'
        AND NOT {backed.format(t="company_founder_claims", extra=" AND x.name = fo.name")}"""), {"c": company_id})
    for t in ("company_investor_claims", "company_founder_claims"):  # claims that were marked verified fall back with their round
        await s.execute(text(f"""UPDATE {t} x SET status = 'unverified' WHERE x.company_id = :c AND x.status = 'verified' AND NOT EXISTS (
            SELECT 1 FROM funding_round_claims fc JOIN funding_rounds f ON f.id = fc.funding_round_id
            WHERE fc.source_event_id = x.source_event_id AND f.verification_status = 'VERIFIED')"""), {"c": company_id})


async def _write_round(s: AsyncSession, company_id: str, p: dict, india_company: bool, now: datetime) -> None:
    k, ev = p["claims"], p["evidence"]
    head = k[0]
    amount = Decimal(str(head["amount_value"])) if head["amount_value"] is not None else None
    currency = next((c["currency"] for c in k if c["currency"]), None)
    stage = _stage(k)
    announced = min(c["seen"] for c in k)
    india = india_company or any((c["raw_metadata"] or {}).get("region") == "india" or INDIA.search(c["title"] or "") for c in k)
    best = max((e["weight"] for e in ev if e["supports"]), default=0)
    flags = list(p["flags"])
    si, sg = scoring.funding_scores(stage, best, india, announced, p["confidence"], flags, now)
    key = None
    if amount is not None and p["status"] in ("PARTIALLY_VERIFIED", "VERIFIED"):
        key = f"{company_id}|{currency or ''}|{amount}|{announced:%Y-%m}"
        if (await s.execute(text("SELECT 1 FROM funding_rounds WHERE identity_key = :k AND id <> ALL(CAST(:r AS uuid[]))"),
                            {"k": key, "r": [str(r) for r in p["round_ids"]] or [NIL]})).first():
            key = None  # another round already holds this identity: keep both, do not claim they are the same
    rounds = sorted(p["round_ids"], key=str)
    rid = rounds[0] if rounds else None
    row = {"c": company_id, "st": stage, "am": amount, "cur": currency, "usd": amount if currency == "USD" else None, "an": announced,
           "det": min(c["detected_at"] for c in k), "vs": p["status"], "cf": p["confidence"], "ru": all(c["is_rumour"] for c in k),
           "fl": json.dumps(flags), "ik": key, "si": si, "sg": sg, "isv": p["status"] == "VERIFIED"}
    sql_vals = """stage = :st, amount_original = :am, currency = :cur, amount_usd = :usd, announced_at = :an, first_detected_at = :det,
        verification_status = :vs, confidence = :cf, is_rumour = :ru, risk_flags = CAST(:fl AS jsonb), identity_key = :ik,
        score_india = :si, score_global = :sg, updated_at = now(),
        verified_at = CASE WHEN :isv THEN coalesce(funding_rounds.verified_at, now()) END"""
    if rid is None:
        rid = (await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, amount_original, currency, amount_usd, announced_at,
            first_detected_at, verification_status, confidence, is_rumour, risk_flags, identity_key, score_india, score_global, verified_at)
            VALUES (:c, :st, :am, :cur, :usd, :an, :det, :vs, :cf, :ru, CAST(:fl AS jsonb), :ik, :si, :sg,
                    CASE WHEN :isv THEN now() END) RETURNING id"""), row)).scalar_one()
    else:
        if rounds[1:]:  # two rounds are becoming one: free their identity keys first, or the survivor cannot take the key a duplicate still holds
            await s.execute(text("UPDATE funding_rounds SET identity_key = NULL WHERE id = ANY(CAST(:o AS uuid[]))"), {"o": [str(r) for r in rounds[1:]]})
        await s.execute(text(f"UPDATE funding_rounds SET {sql_vals} WHERE id = :id"), {**row, "id": rid})
        for other in rounds[1:]:  # claims that used to form two rounds now form one: keep the oldest id
            for tbl in ("evidence", "company_investors"):
                await s.execute(text(f"DELETE FROM {tbl} WHERE funding_round_id = :o"), {"o": other})
            await s.execute(text("DELETE FROM funding_rounds WHERE id = :o"), {"o": other})
    eids = {}
    for e in ev + [{"claim": c, "group": None, "type": scoring.evidence_type(c["source"]), "weight": scoring.weight(scoring.evidence_type(c["source"])),
                    "supports": False} for c in p["contradicts"]]:
        c = e["claim"]
        quotes = (c["evidence"] or {}).get("quotes") or {}
        snippet = next((q for q in quotes.values() if isinstance(q, str)), None) or c["title"]
        grp = e["group"] or hashlib.sha1(str(c["eid"]).encode()).hexdigest()[:16]
        eids[c["eid"]] = (await s.execute(text("""INSERT INTO evidence (company_id, funding_round_id, source_event_id, evidence_type, weight, url, snippet,
            syndication_group, supports) VALUES (:c, :r, :e, :t, :w, :u, :sn, :g, :su)
            ON CONFLICT (funding_round_id, source_event_id) WHERE funding_round_id IS NOT NULL AND source_event_id IS NOT NULL
            DO UPDATE SET evidence_type = EXCLUDED.evidence_type, weight = EXCLUDED.weight, url = EXCLUDED.url, snippet = EXCLUDED.snippet,
                syndication_group = EXCLUDED.syndication_group, supports = EXCLUDED.supports RETURNING id"""),
            {"c": company_id, "r": rid, "e": c["eid"], "t": e["type"], "w": e["weight"], "u": c["canonical_url"], "sn": snippet, "g": str(grp)[:64],
             "su": e["supports"]})).scalar_one()
    await s.execute(text("DELETE FROM evidence WHERE funding_round_id = :r AND source_event_id IS NOT NULL AND source_event_id <> ALL(:keep)"),
                    {"r": rid, "keep": list(eids)})
    await s.execute(text("UPDATE funding_round_claims SET funding_round_id = :r, status = :st WHERE id = ANY(:i)"),
                    {"r": rid, "st": "verified" if p["status"] == "VERIFIED" else "unverified", "i": [c["id"] for c in k]})
    if p["status"] == "VERIFIED":
        await _promote_people(s, company_id, rid, [c["eid"] for c in k], eids)


async def _promote_people(s: AsyncSession, company_id: str, round_id, event_ids: list, evidence_ids: dict) -> None:
    """Investor and founder claims from the events behind a VERIFIED round become authoritative rows (with the evidence row as proof)."""
    for r in (await s.execute(text("""SELECT id, source_event_id, name, canonical_name, role FROM company_investor_claims
            WHERE company_id = :c AND source_event_id = ANY(:e) AND status <> 'rejected'"""), {"c": company_id, "e": event_ids})).all():
        inv = (await s.execute(text("""INSERT INTO investors (name, canonical_name) VALUES (:n, :c)
            ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name RETURNING id"""),
            {"n": r.name, "c": r.canonical_name})).scalar_one()
        await s.execute(text("""INSERT INTO company_investors (company_id, investor_id, funding_round_id, role, evidence_id, promoted_by)
            VALUES (:c, :i, :f, :r, :ev, 'phase7') ON CONFLICT (company_id, investor_id, funding_round_id)
            DO UPDATE SET role = coalesce(company_investors.role, EXCLUDED.role)"""),
            {"c": company_id, "i": inv, "f": round_id, "r": r.role, "ev": evidence_ids.get(r.source_event_id)})
        await s.execute(text("UPDATE company_investor_claims SET status = 'verified' WHERE id = :i"), {"i": r.id})
    for r in (await s.execute(text("""SELECT id, source_event_id, name FROM company_founder_claims
            WHERE company_id = :c AND source_event_id = ANY(:e) AND status <> 'rejected'"""), {"c": company_id, "e": event_ids})).all():
        await s.execute(text("""INSERT INTO founders (company_id, name, evidence_id, promoted_by) VALUES (:c, :n, :ev, 'phase7')
            ON CONFLICT (company_id, name) DO NOTHING"""), {"c": company_id, "n": r.name, "ev": evidence_ids.get(r.source_event_id)})
        await s.execute(text("UPDATE company_founder_claims SET status = 'verified' WHERE id = :i"), {"i": r.id})


async def verify_all(sm) -> dict:
    """Every company that has claims. One transaction per company: a bad row costs that company's pass, not the others'."""
    async with sm() as s:
        ids = [str(r) for r in (await s.execute(text("""SELECT DISTINCT company_id FROM funding_round_claims WHERE company_id IS NOT NULL
            UNION SELECT company_id FROM funding_rounds"""))).scalars()]
    total: dict = {}
    for cid in ids:
        async with sm() as s:
            for k, n in (await verify_company(s, cid)).items():
                total[k] = total.get(k, 0) + n
            await s.commit()
    return total
