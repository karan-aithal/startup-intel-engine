"""Phase 11 measurement: classifier precision/recall, latency, source contribution, duplicate rates, ranking comparison.

Read-only. Nothing here changes alerts, scores or data; it only reports, so a tuning change can be judged before and after.
Thresholds live in config/quality.yaml."""
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text

CONFIG = Path(__file__).resolve().parents[3] / "config" / "quality.yaml"


@lru_cache
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def gold_metrics(rows: list[dict], classify) -> dict:
    """Per-label precision and recall of `classify(title, summary)` against rows with a filled correct_label.
    Rows sharing an event_group are one event covered by several sources: each group counts once in total."""
    size = Counter(r["event_group"] for r in rows if r.get("event_group"))
    tp, fp, fn, wrong = Counter(), Counter(), Counter(), []
    for r in rows:
        w = 1 / size[r["event_group"]] if r.get("event_group") else 1
        got, want = classify(r["title"], r.get("summary") or None)["label"], r["correct_label"].strip().lower()
        if got == want:
            tp[want] += w
        else:
            fp[got] += w
            fn[want] += w
            wrong.append((want, got, r["title"][:90]))
    out = {"rows": len(rows), "events": sum(1 / size[r["event_group"]] if r.get("event_group") else 1 for r in rows), "labels": {}, "wrong": wrong}
    for lab in ("funding", "rumour", "hiring"):
        n = tp[lab] + fn[lab]
        out["labels"][lab] = {"tp": tp[lab], "fp": fp[lab], "fn": fn[lab], "support": n,
                              "precision": tp[lab] / (tp[lab] + fp[lab]) if tp[lab] + fp[lab] else None, "recall": tp[lab] / n if n else None}
    return out


LATENCY = """SELECT topic,
    count(*) AS n,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from sent_at - (payload->>'detected_at')::timestamptz)) AS detect_to_sent_p50,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch from sent_at - (payload->>'detected_at')::timestamptz)) AS detect_to_sent_p90,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from sent_at - (payload->>'published_at')::timestamptz))
        FILTER (WHERE payload->>'published_at' IS NOT NULL AND (payload->>'published_at')::timestamptz <= sent_at) AS publish_to_sent_p50
    FROM alert_deliveries WHERE status = 'sent' AND topic <> 'Ops' AND payload ? 'detected_at' GROUP BY topic ORDER BY topic"""

# Items first seen on a source's first poll are a back-fill of its recent history, not detection delay: leave them out.
FIRST_POLL = "e.detected_at > (SELECT min(started_at) FROM source_runs WHERE source_id = r.id) + interval '1 hour'"
DETECTION = f"""SELECT r.name AS source, count(*) AS n,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from e.detected_at - e.published_at)) AS p50,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch from e.detected_at - e.published_at)) AS p90
    FROM source_events e JOIN source_registry r ON r.id = e.source_id
    WHERE e.detected_at > now() - interval '14 days' AND e.published_at <= e.detected_at AND e.published_at > e.detected_at - interval '7 days'
      AND e.event_type <> 'job' AND {FIRST_POLL} GROUP BY 1 ORDER BY p50"""

CONTRIBUTION = """WITH first AS (SELECT DISTINCT ON (ev.funding_round_id) ev.funding_round_id, e.source_id
        FROM evidence ev JOIN source_events e ON e.id = ev.source_event_id WHERE ev.funding_round_id IS NOT NULL
        ORDER BY ev.funding_round_id, e.detected_at),
    firsts AS (SELECT source_id, count(*) AS n FROM first GROUP BY 1),
    ev AS (SELECT e.source_id, count(*) AS events,
                  count(*) FILTER (WHERE e.processing_status IN ('funding', 'rumour', 'hiring')) AS relevant,
                  count(a.id) AS alerts
           FROM source_events e LEFT JOIN alert_deliveries a ON a.dedup_key = 'event:' || e.content_hash AND a.status = 'sent'
           GROUP BY 1)
    SELECT r.name AS source, r.connector_type AS route, ev.events, ev.relevant, ev.alerts, coalesce(f.n, 0) AS first_for_rounds
    FROM ev JOIN source_registry r ON r.id = ev.source_id LEFT JOIN firsts f ON f.source_id = ev.source_id
    ORDER BY ev.alerts DESC, ev.relevant DESC, ev.events DESC"""

INDIA_REASON = """SELECT CASE WHEN region AND loc THEN 'source region and location text'
                              WHEN region THEN 'source region only' WHEN loc THEN 'location text only' ELSE 'not India' END AS reason, count(*) AS n
    FROM (SELECT coalesce(e.raw_metadata->>'region', '') = 'india' AS region, coalesce(e.location, '') ~* :rx AS loc
          FROM source_events e WHERE e.processing_status IN ('funding', 'rumour')) x GROUP BY 1 ORDER BY 2 DESC"""

DUP_COMPANIES = """SELECT a.name AS a, b.name AS b, round(similarity(a.canonical_name, b.canonical_name)::numeric, 2) AS similarity
    FROM companies a JOIN companies b ON a.id < b.id
    WHERE similarity(a.canonical_name, b.canonical_name) >= :sim
       -- one name inside the other ("Dreamteam" / "AI-native CRM Dreamteam"): extraction left a description in the name
       OR (least(length(a.canonical_name), length(b.canonical_name)) >= 5
           AND greatest(word_similarity(a.canonical_name, b.canonical_name), word_similarity(b.canonical_name, a.canonical_name)) >= :wsim)"""

DUP_ROUNDS = """SELECT c.name AS company, a.stage AS stage_a, b.stage AS stage_b, a.amount_usd AS amount_a, b.amount_usd AS amount_b, b.id AS dup_id
    FROM funding_rounds a JOIN funding_rounds b ON a.company_id = b.company_id AND a.id < b.id JOIN companies c ON c.id = a.company_id
    WHERE abs(extract(epoch from coalesce(a.announced_at, a.first_detected_at) - coalesce(b.announced_at, b.first_detected_at))) <= :days * 86400
      AND (a.stage IS NULL OR b.stage IS NULL OR a.stage = b.stage)
      AND (a.amount_usd IS NULL OR b.amount_usd IS NULL OR abs(a.amount_usd - b.amount_usd) <= 0.005 * greatest(a.amount_usd, b.amount_usd))"""

DUP_ALERTS = """SELECT lower(btrim(b.payload->>'company')) AS company, b.payload->>'amount' AS amount, b.id AS dup_id
    FROM alert_deliveries a JOIN alert_deliveries b ON a.id <> b.id AND b.created_at > a.created_at
    WHERE a.topic LIKE '%Funding' AND b.topic LIKE '%Funding' AND a.channel = b.channel
      AND coalesce(btrim(a.payload->>'company'), '') <> '' AND lower(btrim(a.payload->>'company')) = lower(btrim(b.payload->>'company'))
      AND a.payload->>'amount' = b.payload->>'amount' AND b.created_at - a.created_at <= make_interval(days => :days)"""

RANKING = """SELECT f.id::text AS id, c.name AS company, f.stage, f.verification_status AS status, f.score_global, f.score_india
    FROM funding_rounds f JOIN companies c ON c.id = f.company_id
    WHERE {col} IS NOT NULL ORDER BY {col} DESC, f.id LIMIT :n"""


async def _rows(s, sql: str, **p) -> list[dict]:
    return [dict(r) for r in (await s.execute(text(sql), p)).mappings()]


async def latency(s) -> dict:
    return {"alerts": await _rows(s, LATENCY), "detection": await _rows(s, DETECTION)}


async def source_contribution(s) -> list[dict]:
    return await _rows(s, CONTRIBUTION)


async def india_reasons(s) -> list[dict]:
    from app.services.pipeline import INDIA

    return await _rows(s, INDIA_REASON, rx=INDIA.pattern.replace(r"\b", r"\y"))


async def duplicates(s) -> dict:
    """Possible-duplicate rates against their denominators, with the offending pairs. Each is judged against target_rate."""
    c = cfg()["duplicate_audit"]
    count = lambda sql: s.execute(text(sql))
    n_comp = (await count("SELECT count(*) FROM companies")).scalar_one()
    n_round = (await count("SELECT count(*) FROM funding_rounds")).scalar_one()
    n_alert = (await count("SELECT count(*) FROM alert_deliveries WHERE topic LIKE '%Funding'")).scalar_one()
    found = (("companies", await _rows(s, DUP_COMPANIES, sim=c["company_similarity"], wsim=c["company_word_similarity"]), n_comp, lambda r: r["b"]),
             ("rounds", await _rows(s, DUP_ROUNDS, days=c["round_window_days"]), n_round, lambda r: r["dup_id"]),
             ("funding_alerts", await _rows(s, DUP_ALERTS, days=c["alert_window_days"]), n_alert, lambda r: r["dup_id"]))
    out = {}
    for name, rows, denom, key in found:
        dup = len({key(r) for r in rows})
        rate = dup / denom if denom else 0.0
        out[name] = {"duplicates": dup, "total": denom, "rate": rate, "pairs": rows[:10], "ok": rate < c["target_rate"]}
    out["target_rate"] = c["target_rate"]
    return out


async def timestamp_basis(s) -> str:
    """Whether every feed timestamp is on the corrected (UTC) basis. Rankings saved on different bases differ for that reason alone."""
    n = (await s.execute(text("""SELECT count(*) FROM source_events e JOIN source_registry r ON r.id = e.source_id
        WHERE r.connector_type = 'feed' AND e.published_at IS NOT NULL AND NOT (e.raw_metadata ? 'ts_fixed')"""))).scalar_one()
    return "ts-corrected" if n == 0 else "legacy feed timestamps (5.5 h early)"


LEGACY_BASIS = "legacy feed timestamps (5.5 h early)"  # files saved before basis tracking were all on it


async def ranking(s, n: int = 25) -> dict[str, list[dict]]:
    return {col: await _rows(s, RANKING.format(col=f"f.score_{col}"), n=n) for col in ("global", "india")}


def compare_rankings(before: list[dict], after: list[dict]) -> dict:
    """How a ranking moved: overlap of the two top-N sets, mean rank shift of rounds in both, and who entered or left."""
    b, a = {r["id"]: i for i, r in enumerate(before)}, {r["id"]: i for i, r in enumerate(after)}
    both = b.keys() & a.keys()
    n = max(len(before), len(after)) or 1
    return {"overlap": len(both) / n, "mean_rank_shift": sum(abs(b[i] - a[i]) for i in both) / len(both) if both else None,
            "entered": [r["company"] for r in after if r["id"] not in b], "left": [r["company"] for r in before if r["id"] not in a],
            "unchanged": bool(before) and [r["id"] for r in before] == [r["id"] for r in after]}


# ---- company identity (report only) ----

REF_TABLES = """SELECT k.conrelid::regclass::text AS tbl, a.attname AS col FROM pg_constraint k
    JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
    WHERE k.contype = 'f' AND k.confrelid = 'companies'::regclass AND k.conrelid::regclass::text <> 'company_discovery_candidates' ORDER BY 1"""


def classify_name(canonical_name: str, c: dict | None = None) -> dict:
    """Pure: what is wrong with a company name. kinds: description_in_name, headline_fragment, investor_or_institution.
    `cleaned` is the part after the last descriptor word ("french robot maker genesis" -> "genesis"), or None when nothing is left."""
    c = c or cfg()["identity"]
    toks = canonical_name.split()
    kinds, cleaned = [], None
    hits = [i for i, t in enumerate(toks) if t in c["descriptor_words"]]
    if hits:
        kinds.append("description_in_name")
        cleaned = " ".join(toks[max(hits) + 1:]) or None
    if any(t in c["headline_verbs"] for t in toks):
        kinds.append("headline_fragment")
    if any(t in c["institution_words"] for t in toks):
        kinds.append("investor_or_institution")
    return {"kinds": kinds, "cleaned": cleaned}


async def _footprints(s) -> dict[str, dict[str, int]]:
    """company id -> {table: rows} for every table that references companies (read from the catalogue, so a new table is included)."""
    import re

    out: dict[str, dict[str, int]] = {}
    for tbl, col in (await s.execute(text(REF_TABLES))).all():
        assert re.fullmatch(r"[a-z_]+", tbl) and re.fullmatch(r"[a-z_]+", col)  # catalogue names, but never interpolate anything else
        for cid, n in (await s.execute(text(f"SELECT {col}::text, count(*) FROM {tbl} WHERE {col} IS NOT NULL GROUP BY 1"))).all():
            out.setdefault(cid, {})[tbl] = n
    return out


def to_usd(amount, currency: str | None, fx: dict) -> float | None:
    if amount is None:
        return None
    if currency in (None, "USD"):
        return float(amount)
    return float(amount) / fx[currency] if currency in fx else None


def same_round(a: dict, b: dict, window_days: int, c: dict | None = None) -> bool:
    """Two rounds of one company that could be one round: close in time, stage equal or unknown, amount equal or unknown (currencies converted roughly)."""
    c = c or cfg()["identity"]
    when = lambda r: r["announced_at"] or r["first_detected_at"]
    if abs((when(a) - when(b)).total_seconds()) > window_days * 86400:
        return False
    if a["stage"] and b["stage"] and a["stage"] != b["stage"]:
        return False
    ua, ub = to_usd(a["amount_original"], a["currency"], c["approx_fx"]), to_usd(b["amount_original"], b["currency"], c["approx_fx"])
    if ua is None or ub is None:
        return True
    tol = 0.005 if (a["currency"] or "USD") == (b["currency"] or "USD") else c["fx_tolerance"]
    return abs(ua - ub) <= tol * max(ua, ub)


async def identity(s) -> dict:
    """Company name problems, a proposed merge map with what each change would move, and round pairs that look like one round."""
    c = cfg()["identity"]
    comps = await _rows(s, "SELECT id::text AS id, name, canonical_name FROM companies ORDER BY name")
    foot = await _footprints(s)
    alerts = {r["name"]: r["n"] for r in await _rows(s, "SELECT payload->>'company' AS name, count(*) AS n FROM alert_deliveries WHERE payload ? 'company' GROUP BY 1")}
    named_investor = {r["n"] for r in await _rows(s, """SELECT canonical_name AS n FROM investors UNION SELECT canonical_name FROM company_investor_claims""")}
    by_canon = {x["canonical_name"]: x for x in comps}
    items = []
    for co in comps:
        k = classify_name(co["canonical_name"], c)
        kinds, cleaned = k["kinds"], k["cleaned"]
        if co["canonical_name"] in named_investor:
            kinds = [*kinds, "also_named_as_investor"]
        if not kinds:
            continue
        target, conf, action = None, "review", "review_name"
        if cleaned and "description_in_name" in kinds:
            if cleaned in by_canon and by_canon[cleaned]["id"] != co["id"]:
                target, conf, action = by_canon[cleaned], "high", "merge"
            else:
                hit = next((x for x in comps if x["id"] != co["id"] and (x["canonical_name"].startswith(cleaned + " ") or x["canonical_name"].endswith(" " + cleaned))), None)
                if hit:
                    target, conf, action = hit, "medium", "merge"
                else:
                    conf, action = "medium", "rename"
        elif "headline_fragment" in kinds:
            action = "review_name"
        elif "investor_or_institution" in kinds:
            conf, action = "medium", "not_a_company"
        elif "also_named_as_investor" in kinds:
            conf, action = "low", "not_a_company"
        f = foot.get(co["id"], {})
        items.append({"action": action, "confidence": conf, "company": co["name"], "company_id": co["id"], "survivor": target["name"] if target else "",
                      "survivor_id": target["id"] if target else "", "cleaned_name": cleaned.title() if cleaned else "",  # a suggestion: check the real spelling
                       "reason": ", ".join(kinds),
                      "footprint": f, "alerts_by_name": alerts.get(co["name"], 0), "survivor_footprint": foot.get(target["id"], {}) if target else {}})
    rounds = await _rows(s, """SELECT f.id::text AS id, f.company_id::text AS company_id, co.name AS company, f.stage, f.amount_original, f.currency, f.amount_usd,
        f.announced_at, f.first_detected_at, f.verification_status AS status,
        (SELECT string_agg(DISTINCT r.name || ': ' || left(e.title, 60), ' | ') FROM funding_round_claims cl JOIN source_events e ON e.id = cl.source_event_id
         JOIN source_registry r ON r.id = e.source_id WHERE cl.funding_round_id = f.id) AS sources
        FROM funding_rounds f JOIN companies co ON co.id = f.company_id ORDER BY co.name, f.announced_at""")
    pairs, byco = [], {}
    for r in rounds:
        byco.setdefault(r["company_id"], []).append(r)
    win = cfg()["duplicate_audit"]["round_window_days"]
    for rs in byco.values():
        pairs += [(a, b) for i, a in enumerate(rs) for b in rs[i + 1:] if same_round(a, b, win, c)]
    return {"companies": len(comps), "items": items, "round_pairs": pairs, "rounds": len(rounds)}


# ---- India relevance evaluation (report only) ----

CUE_RX = re.compile(r"₹|\bRs\.?\s?\d|\bcrore\b|\blakh\b|\bINR\b|\bIndian?\b", re.IGNORECASE)


def india_rules() -> dict:
    """Predicates over a sample row {region, location, title, summary}. `current` is exactly what the alert pipeline does today;
    the rest are candidates, evaluated offline only."""
    from app.services.pipeline import INDIA

    region = lambda r: r["region"] == "india"
    location = lambda r: bool(INDIA.search(r["location"] or ""))
    text_ = lambda r: bool(INDIA.search(f"{r['title']} {r['summary']}"))
    cues = lambda r: text_(r) or bool(CUE_RX.search(f"{r['title']} {r['summary']}"))
    return {"current (source region OR location text)": lambda r: region(r) or location(r),
            "source region only": region, "location text only": location,
            "candidate: India words in title or summary": text_,
            "candidate: current OR India words": lambda r: region(r) or location(r) or text_(r),
            "candidate: current OR India words and cues (Indian, Rs, crore, rupee sign)": lambda r: region(r) or location(r) or cues(r)}


def india_metrics(rows: list[dict], label: str = "is_india_relevant") -> list[dict]:
    """Precision and recall of every rule against rows whose `label` is y or n (anything else is skipped). A row's `weight` (default 1)
    scales its counts, so a sample drawn in strata can be turned back into an estimate for the whole pool."""
    rows = [r for r in rows if (r.get(label) or "").strip().lower() in ("y", "n")]
    out = []
    for name, rule in india_rules().items():
        tp = fp = fn = tn = 0
        wrong = []
        for r in rows:
            want, got, w = r[label].strip().lower() == "y", rule(r), float(r.get("weight") or 1)
            tp, fp, fn, tn = tp + w * (want and got), fp + w * (got and not want), fn + w * (want and not got), tn + w * (not want and not got)
            if want != got:
                wrong.append(("missed" if want else "false alarm", r["title"][:80]))
        out.append({"rule": name, "n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn, "positives": tp + fn,
                    "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None, "wrong": wrong})
    return out
