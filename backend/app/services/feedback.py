"""Feedback capture, route A (docs/feedback-design.md). Writes ONLY the `feedback` table.

Feedback never changes an alert, an event, a round, a score, a verification status, `companies.watched` or `companies.muted`: it is
the owner's opinion, recorded next to the facts. An alert is named by a short reference (the first characters of its id, from `recent`).

Row shape: action, source_event_id, company_id and funding_round_id when the claims or evidence give them, and `note`, always a JSON
object: alert (full id), topic, source, company, via ('drawn' by `next`, or 'chosen'), and optional why, of, text."""
import json
import random
import re
from math import sqrt

from sqlalchemy import bindparam, text

from app.services import quality

ACTIONS = ("useful", "irrelevant", "duplicate", "incorrect", "watch", "mute", "save", "applied")
RATINGS = ("useful", "irrelevant", "incorrect", "duplicate")  # one active rating per alert; the rest are independent marks
WHY = ("wrong_label", "wrong_company", "wrong_amount", "wrong_stage", "not_startup_funding", "not_india", "other")
REF = re.compile(r"[0-9a-f][0-9a-f-]{5,35}")  # a prefix of the uuid text, dashes included
ALERT_OF = "(CASE WHEN f.note ~ '^\\{' THEN f.note::jsonb->>'alert' END)"  # the alert a feedback row is about (CASE: never cast a non-JSON note)
RATED = f"f.action IN ({', '.join(repr(r) for r in RATINGS)})"


class FeedbackError(ValueError):
    """Something the owner can fix by changing the command; nothing was written."""


def cfg() -> dict:
    return quality.cfg()["feedback"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p, d = k / n, 1 + z * z / n
    c, h = p + z * z / (2 * n), z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d, (c + h) / d


async def resolve(s, ref: str) -> dict:
    """One alert, and the one event it was sent for (dedup key + source + URL, unique for every alert so far). Refuses anything else."""
    ref = ref.strip().lower()
    if not REF.fullmatch(ref):
        raise FeedbackError("A reference is 6 or more hex characters (dashes allowed): the start of an alert id, as listed by `recent`.")
    alerts = (await s.execute(text("SELECT id::text AS id, topic, dedup_key, payload FROM alert_deliveries WHERE topic <> 'Ops' AND id::text LIKE :p"),
                              {"p": ref + "%"})).mappings().all()
    if not alerts:
        raise FeedbackError(f"No alert starts with {ref}.")
    if len(alerts) > 1:
        raise FeedbackError(f"{ref} matches {len(alerts)} alerts: use more characters.")
    a, p = alerts[0], alerts[0]["payload"]
    events = (await s.execute(text("""SELECT e.id::text AS id FROM source_events e JOIN source_registry r ON r.id = e.source_id
        WHERE :k = 'event:' || e.content_hash AND r.name = :src AND (e.source_url = :url OR e.canonical_url = :url)"""),
                              {"k": a["dedup_key"], "src": p.get("source"), "url": p.get("url")})).all()
    if len(events) != 1:
        raise FeedbackError(f"Alert {a['id'][:8]} resolves to {len(events)} events, expected exactly 1. This is an identity problem: nothing was written, report it.")
    return {"alert_id": a["id"], "topic": a["topic"], "source": p.get("source"), "company": p.get("company"), "title": p.get("title"), "event_id": events[0].id}


async def _links(s, event_id: str) -> tuple[str | None, str | None]:
    """company_id and funding_round_id for an event, when the claims or evidence name exactly one. Best effort: NULL is normal (hiring alerts)."""
    co = (await s.execute(text("SELECT company_id::text FROM funding_round_claims WHERE source_event_id = :e AND company_id IS NOT NULL"), {"e": event_id})).scalars().all()
    rd = (await s.execute(text("SELECT DISTINCT funding_round_id::text FROM evidence WHERE source_event_id = :e AND funding_round_id IS NOT NULL"), {"e": event_id})).scalars().all()
    return (co[0] if len(co) == 1 else None), (rd[0] if len(rd) == 1 else None)


async def _rows(s, alert_id: str) -> list[dict]:
    return [dict(r) for r in (await s.execute(text(f"SELECT f.id::text AS id, f.action, f.note FROM feedback f WHERE {ALERT_OF} = :a ORDER BY f.created_at"), {"a": alert_id})).mappings()]


async def add(s, action: str, ref: str, *, why: str | None = None, of: str | None = None, note: str | None = None, drawn: bool = False) -> str:
    """Record feedback on an alert. A rating (useful, irrelevant, incorrect, duplicate) replaces the alert's earlier rating, in the caller's
    transaction; saying the same thing twice is refused. Returns a one-line description of what was written."""
    if action not in ACTIONS:
        raise FeedbackError(f"Unknown action '{action}'. Choose one of: {', '.join(ACTIONS)}.")
    if action == "incorrect" and why not in WHY:
        raise FeedbackError(f"'incorrect' needs --why, one of: {', '.join(WHY)}.")
    if why and action != "incorrect":
        raise FeedbackError("--why only goes with 'incorrect'.")
    if of and action != "duplicate":
        raise FeedbackError("--of only goes with 'duplicate'.")
    if action == "duplicate" and not of:
        raise FeedbackError("'duplicate' needs --of REF, the alert it repeats.")
    if note and (len(note) > 500 or any(ord(c) < 32 for c in note)):
        raise FeedbackError("--text must be one line of at most 500 characters.")
    a = await resolve(s, ref)
    first = None
    if of:
        first = await resolve(s, of)
        if first["alert_id"] == a["alert_id"]:
            raise FeedbackError("An alert cannot be a duplicate of itself.")
    body = {"alert": a["alert_id"], "topic": a["topic"], "source": a["source"], "company": a["company"], "via": "drawn" if drawn else "chosen",
            **({"why": why} if why else {}), **({"of": first["alert_id"]} if first else {}), **({"text": note} if note else {})}
    old = await _rows(s, a["alert_id"])
    for r in old:  # saying the same thing twice (same action and the same reason or original) is refused
        if r["action"] == action and all(json.loads(r["note"]).get(k) == body.get(k) for k in ("why", "of")):
            raise FeedbackError(f"Already recorded: {action} on {a['alert_id'][:8]}. Use `undo` first to change it.")
    revised = None
    if action in RATINGS:
        for r in old:
            if r["action"] in RATINGS:
                await s.execute(text("DELETE FROM feedback WHERE id = :i"), {"i": r["id"]})  # the new rating replaces it
                revised = r["action"]
                body["via"] = json.loads(r["note"]).get("via", body["via"])  # a revision keeps how the alert was first rated: drawn stays drawn, chosen stays chosen
    company_id, round_id = await _links(s, a["event_id"])
    await s.execute(text("""INSERT INTO feedback (action, source_event_id, company_id, funding_round_id, note)
        VALUES (:a, :e, :c, :r, :n)"""), {"a": action, "e": a["event_id"], "c": company_id, "r": round_id, "n": json.dumps(body)})
    return f"{action} recorded on {a['alert_id'][:8]} ({a['topic']}: {(a['title'] or '')[:60]})" + (f"; replaced your earlier '{revised}'" if revised else "")


async def undo(s, ref: str, action: str | None = None) -> str:
    """Remove ONE feedback row of one alert: the rating, or the named action. The alert and its event are never touched."""
    a = await resolve(s, ref)
    rows = await _rows(s, a["alert_id"])
    pick = [r for r in rows if (r["action"] == action if action else r["action"] in RATINGS)]
    if not pick:
        have = ", ".join(sorted({r["action"] for r in rows})) or "nothing"
        raise FeedbackError(f"No {action or 'rating'} to remove on {a['alert_id'][:8]}. It has: {have}." + ("" if action else " Name one with --action."))
    await s.execute(text("DELETE FROM feedback WHERE id = :i"), {"i": pick[-1]["id"]})
    return f"removed {pick[-1]['action']} from {a['alert_id'][:8]}"


async def recent(s, topic: str | None = None, n: int = 20) -> list[dict]:
    return [dict(r) for r in (await s.execute(text(f"""SELECT left(a.id::text, 8) AS ref, a.topic, a.created_at, a.payload->>'source' AS source, left(a.payload->>'title', 80) AS title,
        (SELECT string_agg(f.action, ', ' ORDER BY f.created_at) FROM feedback f WHERE {ALERT_OF} = a.id::text) AS marks
        FROM alert_deliveries a WHERE a.topic <> 'Ops' AND (CAST(:t AS text) IS NULL OR a.topic = :t) ORDER BY a.created_at DESC LIMIT :n"""),
                                              {"t": topic, "n": n})).mappings()]


async def drawn_today(s, tz: str) -> dict[str, int]:
    """Drawn ratings made today (display timezone), by topic: the daily budget and the even spread both count these."""
    q = text("""SELECT f.note::jsonb->>'topic' AS topic, count(*) FROM feedback f WHERE f.note ~ '^\\{' AND f.action IN :r
        AND f.note::jsonb->>'via' = 'drawn' AND (f.created_at AT TIME ZONE :tz)::date = (now() AT TIME ZONE :tz)::date GROUP BY 1""").bindparams(bindparam("r", expanding=True))
    return {t: n for t, n in (await s.execute(q, {"r": list(RATINGS), "tz": tz})).all()}


async def draw(s, tz: str, topic: str | None = None, more: bool = False, rng: random.Random | None = None) -> dict | None:
    """A random unrated alert from the topic with the fewest drawn ratings today (ties broken at random), so the day's few ratings spread
    evenly across the four topics. None when today's budget is used up (unless `more`) or nothing is left to rate."""
    c, rng = cfg(), rng or random.Random()
    done = await drawn_today(s, tz)
    if not more and sum(done.values()) >= c["daily_budget"]:
        return None
    if topic and topic not in c["topics"]:
        raise FeedbackError(f"Unknown topic '{topic}'. Choose one of: {', '.join(c['topics'])}.")
    topics = [topic] if topic else list(c["topics"])
    unrated = {t: [dict(r) for r in (await s.execute(text(f"""SELECT left(a.id::text, 8) AS ref, a.topic, a.payload->>'source' AS source, a.payload->>'title' AS title, a.payload->>'company' AS company
        FROM alert_deliveries a WHERE a.topic = :t AND NOT EXISTS (SELECT 1 FROM feedback f WHERE {ALERT_OF} = a.id::text AND {RATED})"""), {"t": t})).mappings()]
               for t in topics}
    open_topics = [t for t in topics if unrated[t]]
    if not open_topics:
        return None
    fewest = min(done.get(t, 0) for t in open_topics)
    return rng.choice(unrated[rng.choice([t for t in open_topics if done.get(t, 0) == fewest])])


async def stats(s) -> dict:
    """Ratings so far. Precision comes only from DRAWN ratings (chosen ones are biased towards the memorable) and is reported as a measured
    result only from `min_ratings` per group; below that the group is 'insufficient' and no percentage is given."""
    c = cfg()
    rows = [dict(r) for r in (await s.execute(text(f"""WITH latest AS (SELECT DISTINCT ON (alert_id) alert_id, action, why, via FROM (
            SELECT {ALERT_OF} AS alert_id, f.action, CASE WHEN f.note ~ '^\\{{' THEN f.note::jsonb->>'why' END AS why,
                   CASE WHEN f.note ~ '^\\{{' THEN f.note::jsonb->>'via' END AS via, f.created_at
            FROM feedback f WHERE {RATED}) x WHERE alert_id IS NOT NULL ORDER BY alert_id, created_at DESC)
        SELECT a.topic, a.payload->>'source' AS source, l.action, l.why, l.via FROM latest l JOIN alert_deliveries a ON a.id::text = l.alert_id"""))).mappings()]

    def group(key: str) -> list[dict]:
        out = []
        for g in sorted({r[key] for r in rows}):
            mine = [r for r in rows if r[key] == g]
            drawn = [r for r in mine if r["via"] == "drawn"]
            k = sum(r["action"] == "useful" for r in drawn)
            m = {"group": g, "drawn": len(drawn), "chosen": len(mine) - len(drawn), "useful": k, "status": "measured" if len(drawn) >= c["min_ratings"] else "insufficient"}
            if m["status"] == "measured":
                m["precision"], (m["low"], m["high"]) = k / len(drawn), wilson(k, len(drawn))
            out.append(m)
        return out

    unrated = {r.topic: r.n for r in (await s.execute(text(f"""SELECT a.topic, count(*) AS n FROM alert_deliveries a WHERE a.topic <> 'Ops' AND NOT EXISTS (
        SELECT 1 FROM feedback f WHERE {ALERT_OF} = a.id::text AND {RATED}) GROUP BY 1"""))).all()}
    marks = {r.action: r.n for r in (await s.execute(text("SELECT action, count(*) AS n FROM feedback WHERE action IN ('watch', 'mute', 'save', 'applied') GROUP BY 1"))).all()}
    return {"rated": len(rows), "min_ratings": c["min_ratings"], "by_topic": group("topic"), "by_source": group("source"), "unrated": unrated, "marks": marks,
            "why": {w: sum(r["why"] == w and r["action"] == "incorrect" for r in rows) for w in WHY if any(r["why"] == w for r in rows)},
            "duplicates": sum(r["action"] == "duplicate" for r in rows)}
