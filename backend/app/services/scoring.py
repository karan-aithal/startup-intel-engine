"""Phase 7 rules as pure functions: evidence weights, status, confidence, risk flags, scores. Config: config/scoring.yaml.
No DB access here, so every rule is testable without a database."""
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"


@lru_cache
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def evidence_type(source_name: str) -> str:
    return cfg()["source_types"].get(source_name, "community")


def weight(etype: str) -> int:
    return cfg()["evidence_weights"][etype]


def assess(evidence: list[dict], rumour_only: bool) -> dict:
    """evidence: [{group, type, weight, supports}]. Copies in one syndication group count once (their best weight).
    Returns {status, confidence, flags, independent}. Status is the pre-contradiction result."""
    v, ok = cfg()["verification"], [e for e in evidence if e["supports"]]
    best: dict[str, int] = {}
    for e in ok:
        best[e["group"]] = max(best.get(e["group"], 0), e["weight"])
    ws = sorted(best.values(), reverse=True)
    conf = (ws[0] + v["corroboration_bonus"] * min(len(ws) - 1, 2)) if ws else 0
    if rumour_only:
        conf = min(conf, v["rumour_confidence_cap"])
    official = any(e["type"] in cfg()["official_types"] for e in ok)
    if rumour_only:
        status = "UNVERIFIED"  # one rumour, however many outlets repeat it, is not a confirmed round
    elif official or sum(w >= v["strong_weight"] for w in ws) >= 2:
        status = "VERIFIED"
    elif (ws and ws[0] >= v["partial_weight"]) or sum(w >= v["weak_weight"] for w in ws) >= 2:
        status = "PARTIALLY_VERIFIED"
    else:
        status = "UNVERIFIED"
    flags = []
    if len(ws) == 1:
        flags.append("single_independent_source")
    if ws and ws[0] < v["weak_weight"]:
        flags.append("community_only")
    if rumour_only:
        flags.append("rumour_only")
    return {"status": status, "confidence": min(conf, 100), "flags": flags, "independent": len(ws)}


def freshness(since: datetime | None, now: datetime) -> float:
    if since is None:
        return 0.0
    return 100 * 0.5 ** (max((now - since).total_seconds(), 0) / 86400 / cfg()["scores"]["freshness_half_life_days"])


def _blend(w: dict, c: dict) -> tuple[int, int]:
    """(india score, global score). Global drops the India term and renormalises: India relevance is never required."""
    rest = sum(w[k] * c[k] for k in w if k != "I") / (1 - w["I"])
    return round(sum(w[k] * c[k] for k in w)), round(rest)


def _net(scores: tuple[int, int], flags: list[str]) -> tuple[int, int]:
    pen = sum(cfg()["risk_flags"].get(f, 0) for f in flags)
    return tuple(max(0, min(100, x - pen)) for x in scores)


def funding_scores(stage: str | None, best_weight: int, india: bool, announced: datetime | None, confidence: int,
                   flags: list[str], now: datetime) -> tuple[int, int]:
    """F = 0.30S + 0.25R + 0.20I + 0.15N + 0.10V, minus risk-flag penalties. Returns (india, global)."""
    sc = cfg()["scores"]
    c = {"S": sc["stage_relevance"].get((stage or "").lower(), sc["stage_unknown"]), "R": best_weight,
         "I": 100 if india else 0, "N": freshness(announced, now), "V": confidence}
    return _net(_blend(sc["funding"], c), flags)


def emerging_scores(signals: int, age_days: float, india: bool, best_confidence: int, latest: datetime | None,
                    now: datetime) -> tuple[int, int]:
    """E = 0.30G + 0.25D + 0.20I + 0.15R + 0.10N. G: correlation signals; D: how recently the company was discovered."""
    sc, co = cfg()["scores"], cfg()["correlation"]
    c = {"G": min(100, signals * co["growth_per_signal"]), "D": 100 * 0.5 ** (max(age_days, 0) / sc["novelty_half_life_days"]),
         "I": 100 if india else 0, "R": best_confidence, "N": freshness(latest, now)}
    return _blend(sc["emerging"], c)


def decide_conflict(a: dict, b: dict) -> None:
    """Two rounds with different amounts in the same window. Mutates both: the clearly weaker one is CONTRADICTED; with no clear
    winner neither can be VERIFIED. Both lose confidence and carry conflicting_amount."""
    v = cfg()["verification"]
    for x in (a, b):
        if "conflicting_amount" not in x["flags"]:
            x["flags"].append("conflicting_amount")
    hi, lo = (a, b) if a["confidence"] >= b["confidence"] else (b, a)
    if hi["confidence"] - lo["confidence"] >= v["contradiction_margin"]:
        lo["status"] = "CONTRADICTED"
    else:
        for x in (a, b):
            if x["status"] == "VERIFIED":
                x["status"] = "PARTIALLY_VERIFIED"
    for x in (a, b):
        x["confidence"] = max(0, x["confidence"] - v["contradiction_penalty"])
    hi["contradicts"], lo["contradicts"] = lo["claims"], []  # the winner records the loser's claims as contradicting evidence


assert math.isclose(sum(cfg()["scores"]["funding"].values()), 1) and math.isclose(sum(cfg()["scores"]["emerging"].values()), 1)
