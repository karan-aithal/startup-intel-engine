"""Rule-based keyword pre-filter. Classifies funding vs hiring vs rumour vs spam. No LLM."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[3] / "config" / "keywords.yaml"


@lru_cache
def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def _hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", text)]


def classify(title: str | None, content: str | None = None) -> dict:
    """Returns {label, score, amount, matches}. label: funding|rumour|hiring|spam|none."""
    c = _cfg()
    text = f"{title or ''} {content or ''}".lower()
    f = c["funding"]
    verbs, stages, inv = (_hits(text, f[k]["terms"]) for k in ("verbs", "stages", "investors"))
    amounts = [m.group(0) for rx in f["amount_regex"] for m in re.finditer(rx, f"{title or ''} {content or ''}", re.I)]
    rumour = _hits(text, f["not_funding"])
    hire = _hits(text, c["hiring"]["direct"]["terms"])
    spam = _hits(text, c["spam"]["terms"])
    slow = _hits(text, c["spam"]["slowdown"])
    fscore = f["verbs"]["weight"] * bool(verbs) + f["stages"]["weight"] * bool(stages) + f["investors"]["weight"] * bool(inv) + 2 * bool(amounts)
    if spam:
        label = "spam"
    elif rumour:
        label = "rumour"
    elif (verbs and (stages or amounts)) or fscore >= 6:
        label = "funding"
    elif hire:
        label = "hiring"
    else:
        label = "none"
    return {"label": label, "score": fscore, "amount": amounts[0] if amounts else None,
            "matches": {"verbs": verbs, "stages": stages, "investors": inv, "hiring": hire, "slowdown": slow}}
