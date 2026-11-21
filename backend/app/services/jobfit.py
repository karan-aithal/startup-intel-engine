"""Explore v2 (J5): fit hint, role category and seniority from a job title, by the keyword rules in config/jobs.yaml. Zero tokens, no LLM, derived at read time
and never stored (decision 69). The career-ops idea (a title-based fit hint against your targets), written from its description, not copied."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[3] / "config" / "jobs.yaml"


@lru_cache
def cfg() -> dict:
    c = yaml.safe_load(CONFIG.read_text())

    def rx(words):  # whole words; an entry "stem:engineer" is a word that STARTS with engineer (engineers, engineering), as in career-ops title-keywords.mjs
        parts = [re.escape(w[5:]) + r"\w*" if w.startswith("stem:") else re.escape(w) + r"\b" for w in words]
        return re.compile(r"\b(?:" + "|".join(parts) + ")", re.IGNORECASE) if words else None

    c["_role"] = {k: rx(v) for k, v in c["role"].items()}
    c["_exclude"] = rx(c.get("exclude_words") or [])
    c["_places"] = rx(c["targets"]["prefer_places"])
    return c


# Seniority from the title. Ported from career-ops classify-tier.mjs (MIT; read, not copied wholesale), mapped to this app's levels:
# above (chief, VP, director, principal, staff, head of: outside the targets), senior, mid, junior, intern. No marker: None (career-ops says mid).
SENIOR_NOUN = (r"chief|vp|vice\s+president|director|principal|staff|lead(?![\s-]+(?:generation\b|gen\b(?![\s-]*ai\b)))|"
               r"senior(?![\s-]+living\b)|sr\.?|head\s+of|partner")
JUNIOR_WORD = r"intern(?:ship)?|trainee|co-op|graduate|junior|entry(?:-level)?"
MARKERS = [  # (pattern, level). The LEFTMOST marker in the title is the role's own level; list order breaks a tie at the same offset
    (r"\b(?:chief|vp|vice\s+president|director|principal|staff|head\s+of)\b", "above"),
    (r"\blead\b(?![\s-]+(?:generation\b|gen\b(?![\s-]*ai\b)))", "senior"), (r"\bsenior\b(?![\s-]+living\b)", "senior"), (r"\bsr\b", "senior"),
    (r"\b[a-z]{2,}[\s-](?:iii|iv|v)\b", "senior"),
    (r"\bmid\b", "mid"), (r"\b[a-z]{2,}[\s-]ii\b", "mid"), (r"\b(?:l4|l5)\b", "mid"),
    (r"\b(?:entry|associate|junior|fresher)\b", "junior"), (r"\bnew\s+grad\b", "junior"), (r"\b[a-z]{2,}[\s-]i\b", "junior"), (r"\b(?:l1|l2)\b", "junior"),
    (r"\b(?:internship|intern|trainee|co-op|apprentice|working\s+student)\b", "intern"),
    (r"\bgraduate\b(?=.*\b(?:program|scheme)\b)", "intern"),  # "Graduate Engineer Program", not a bare "Graduate Engineer"
]
MARKERS = [(re.compile(p, re.IGNORECASE), lvl) for p, lvl in MARKERS]
ASSOCIATE_OF = re.compile(r"^\s+(?:[a-z]+\s+){0,2}(director|vice\s+president|vp|principal|partner|chief|head\s+of|professor|dean|provost|chancellor|superintendent|general\s+counsel)\b", re.IGNORECASE)
PROGRAM_BRIDGE = re.compile(rf"\b(?:{JUNIOR_WORD})\s+(?:program|scheme|talent|cohort)\b", re.IGNORECASE)


def seniority(title: str | None) -> str | None:
    """above | senior | mid | junior | intern | None. Position decides, not rank: "Summer Intern, Director of Product" is an internship and "Senior Intern
    Coordinator" is senior. Two guards from career-ops: "Associate Director" and the like are above, and "Intern Program Director" is read as its noun."""
    t = re.sub(r"\bA\.\s?I\.?|\bI\.\s?T\.?|\bi/o\b", lambda m: re.sub(r"[./\s]", "", m.group()).upper(), title or "")  # "A.I." is not a level "I"
    a = re.search(r"\bassociate\b", t, re.IGNORECASE)
    if a and (not (j := re.search(rf"\b(?:{JUNIOR_WORD})\b", t, re.IGNORECASE)) or j.start() > a.start()) and ASSOCIATE_OF.match(t[a.end():]):
        return "above"
    if (b := PROGRAM_BRIDGE.search(t)) and re.search(rf"\b(?:{SENIOR_NOUN})(?!\w)", rest := (t[:b.start()] + " " + t[b.end():]).strip(), re.IGNORECASE):
        return seniority(rest)
    best, at = None, len(t) + 1
    for rx, lvl in MARKERS:
        m = rx.search(t)
        if m and m.start() < at:
            best, at = lvl, m.start()
    if best == "senior" and MARKERS[0][0].search(t):
        return "above"  # "Senior Staff Engineer" is a staff role
    return best


def role_category(title: str | None) -> str:
    t = title or ""
    return next((k for k, r in cfg()["_role"].items() if r.search(t)), "other")


def fit(title: str | None, location: str | None, remote: bool) -> str:
    """strong: a target level (named in the title) at a preferred place or remote. related: a target level elsewhere, or no level named. weak: above your
    levels, or an excluded word. Geo comes in already decided (services/geo.py); this never recomputes it."""
    c, level = cfg(), seniority(title)
    if (c["_exclude"] and c["_exclude"].search(title or "")) or (level and level not in c["targets"]["seniority"]):
        return "weak"
    if level and (remote or c["_places"].search(location or "")):
        return "strong"
    return "related"
