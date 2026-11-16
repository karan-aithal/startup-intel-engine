"""Claim staging: regex and LLM extractions are merged into unverified claims with evidence.
Nothing here writes funding_rounds, company_investors or founders. Phase 7 verifies claims and promotes them."""
import html
import json
import re
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.companies import canonical, extract_company, extract_founders, extract_investors
from app.services.normalize import parse_amounts, parse_stage
from app.services.prefilter import classify


ROUTED = "llm_label_none"


def plain(s: str | None) -> str:
    """Article HTML to text: tags (img src URLs break sentence splitting) and entities removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def sentence_with(body: str, needle: str) -> str | None:
    """The sentence containing `needle`; when it is long, a 400-char window around the name so the quote always contains it."""
    for sent in re.split(r"(?<=[.!?])\s+", body):
        i = sent.lower().find(needle.lower())
        if i >= 0:
            sent = sent.strip()
            i = max(0, sent.lower().find(needle.lower()))
            return sent if len(sent) <= 400 else sent[max(0, i - 150):][:400]
    return None


def regex_claim(title: str | None, content: str | None) -> dict:
    body = f"{plain(title)}. {plain(content)}"
    # the headline amount is the round; a body amount can be a total, a loan or a currency conversion
    amt = classify(title, "")["amount"] or classify(title, content)["amount"]
    parsed = parse_amounts(amt) if amt else []
    return {"company": extract_company(title), "stage": parse_stage(title or "") or parse_stage(body),
            "amount_text": amt, "amount_value": parsed[0][0] if parsed else None, "currency": parsed[0][1] if parsed else None,
            "investors": [(n, r) for n, r in extract_investors(body)], "founders": extract_founders(body), "body": body}


def _same_amount(a, b) -> bool:
    return a is not None and b is not None and abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(b)) * Decimal("0.005")


def merge(rx: dict, llm: dict | None) -> dict:
    """Per field: agree -> merged; one side only -> that side; disagree -> LLM (it is grounded) with the regex value kept as an alternative."""
    llm = llm or {}
    origins, alts, out = {}, {}, {}

    def pick(field, same):
        a, b = llm.get(field), rx.get(field)
        if a and b:
            out[field], origins[field] = a, "merged" if same(a, b) else "llm"
            if origins[field] == "llm":
                alts[field] = b
        elif a or b:
            out[field], origins[field] = a or b, "llm" if a else "regex"
        else:
            out[field] = None

    pick("company", lambda a, b: canonical(a) == canonical(b))
    pick("stage", lambda a, b: a == b)
    a, b = llm.get("amount_value"), rx.get("amount_value")
    if a and b:
        agree = _same_amount(a, b)
        out["amount_value"], out["currency"] = (a, llm.get("currency") or rx.get("currency"))
        origins["amount"] = "merged" if agree else "llm"
        if not agree:
            alts["amount"] = {"value": str(b), "currency": rx.get("currency"), "text": rx.get("amount_text")}
    elif a or b:
        src = llm if a else rx
        out["amount_value"], out["currency"], origins["amount"] = src.get("amount_value"), src.get("currency"), "llm" if a else "regex"
    out.setdefault("amount_value", None)
    out.setdefault("currency", None)
    out["amount_text"] = rx.get("amount_text")
    people = {}
    for kind in ("investors", "founders"):
        seen = {}
        for item in llm.get(kind, []):
            seen[canonical(item)] = {"name": item, "origin": "llm", "role": None}
        for item in rx.get(kind, []):
            name, role = item if isinstance(item, tuple) else (item, None)
            c = canonical(name)
            if c in seen:
                seen[c].update(origin="merged", role=role or seen[c]["role"])
            else:
                seen[c] = {"name": name, "origin": "regex", "role": role}
        people[kind] = seen
    out["people"] = people
    out["origins"], out["alternatives"] = origins, alts
    vals = set(origins.values()) | {p["origin"] for k in people.values() for p in k.values()}
    out["origin"] = "merged" if "merged" in vals or {"llm", "regex"} <= vals else (next(iter(vals)) if vals else "regex")
    return out


async def rebuild_claims(s: AsyncSession, event_id: str) -> None:
    """Idempotent: recompute this event's claims from the article plus the latest valid LLM result."""
    ev = (await s.execute(text("""SELECT title, content, processing_status FROM source_events WHERE id = :i"""), {"i": event_id})).one()
    if ev.processing_status not in ("funding", "rumour"):
        return
    llm = (await s.execute(text("SELECT result FROM llm_runs WHERE source_event_id = :i AND task = 'funding' AND valid"), {"i": event_id})).scalar_one_or_none()
    rx = regex_claim(ev.title, ev.content)
    m = merge(rx, llm)
    cid = (await s.execute(text("""SELECT matched_company_id FROM company_discovery_candidates
        WHERE source_event_id = :i AND status = 'accepted'"""), {"i": event_id})).scalar_one_or_none()
    rumour = ev.processing_status == "rumour" or (llm or {}).get("label") == "rumour"
    # Routing: model label "none" (not a funding event) parks this event's claims as rejected. Reversible and per event: the funding
    # claim carries evidence.rejected_by, and only rows rejected by this rule (never by Phase 7) are rewritten if a later run changes the label.
    status = "rejected" if (llm or {}).get("label") == "none" else "unverified"
    was = bool((await s.execute(text("""SELECT 1 FROM funding_round_claims WHERE source_event_id = :i AND status = 'rejected'
        AND evidence->>'rejected_by' = :r"""), {"i": event_id, "r": ROUTED})).first())
    evidence = {"quotes": (llm or {}).get("quotes", {}), "field_status": (llm or {}).get("field_status", {}),
                "origins": m["origins"], "alternatives": m["alternatives"], "llm_label": (llm or {}).get("label")}
    if status == "rejected":
        evidence["rejected_by"] = ROUTED
    await s.execute(text("""INSERT INTO funding_round_claims (source_event_id, company_id, company_name, stage, amount_value, currency,
        amount_text, is_rumour, origin, evidence, status) VALUES (:e, :c, :n, :st, :av, :cur, :at, :ru, :o, CAST(:ev AS jsonb), :s)
        ON CONFLICT (source_event_id) DO UPDATE SET company_id = EXCLUDED.company_id, company_name = EXCLUDED.company_name,
            stage = EXCLUDED.stage, amount_value = EXCLUDED.amount_value, currency = EXCLUDED.currency,
            amount_text = EXCLUDED.amount_text, is_rumour = EXCLUDED.is_rumour, origin = EXCLUDED.origin,
            evidence = EXCLUDED.evidence, status = EXCLUDED.status, updated_at = now()
        WHERE funding_round_claims.status = 'unverified' OR (funding_round_claims.status = 'rejected' AND CAST(:was AS boolean))"""),
        {"e": event_id, "c": cid, "n": m["company"], "st": m["stage"], "av": Decimal(str(m["amount_value"])) if m["amount_value"] is not None else None, "cur": m["currency"],
         "at": m["amount_text"], "ru": rumour, "o": m["origin"], "ev": json.dumps(evidence, default=str), "s": status, "was": was})
    if cid is None:  # person claims need a company; they are written when the company is resolved
        return
    quotes = (llm or {}).get("quotes", {})
    for kind, table in (("investors", "company_investor_claims"), ("founders", "company_founder_claims")):
        await s.execute(text(f"""DELETE FROM {table} WHERE source_event_id = :e AND (status = 'unverified' OR (status = 'rejected' AND CAST(:was AS boolean)))
            AND canonical_name <> ALL(CAST(:keep AS text[]))"""), {"e": event_id, "keep": list(m["people"][kind]), "was": was})
        for c, p in m["people"][kind].items():
            quote = quotes.get(f"{kind}:{p['name']}") or sentence_with(rx["body"], p["name"])
            upd = "role = EXCLUDED.role, " if kind == "investors" else ""
            await s.execute(text(f"""INSERT INTO {table} (source_event_id, company_id, name, canonical_name, evidence_quote, origin
                {', role' if kind == 'investors' else ''}, status) VALUES (:e, :c, :n, :cn, :q, :o{', :r' if kind == 'investors' else ''}, :s)
                ON CONFLICT (company_id, source_event_id, canonical_name) DO UPDATE SET {upd}evidence_quote = EXCLUDED.evidence_quote,
                origin = EXCLUDED.origin, status = EXCLUDED.status
                WHERE {table}.status = 'unverified' OR ({table}.status = 'rejected' AND CAST(:was AS boolean))"""),
                {"e": event_id, "c": cid, "n": p["name"], "cn": c, "q": quote, "o": p["origin"], "s": status, "was": was, **({"r": p["role"]} if kind == "investors" else {})})
