"""Funding classifier evaluation: draw a labelling sample, then score the live rules and candidate changes against the labels.
Offline and read-only: nothing here changes the classifier, alerts, scoring or data.
Usage: cd backend && uv run python ../scripts/classifier_labels.py sample              write docs/classifier_labels.csv (refuses to overwrite it)
       ... sample-validation                                                          write docs/classifier_labels_validation.csv: a fresh seeded draw for the agreement-row check (refuses to overwrite)
       cd backend && uv run python ../scripts/classifier_labels.py evaluate [-v]       score the live rules against the owner's labels
       ... add --validation to evaluate or candidates to score docs/classifier_labels_validation.csv instead
       ... evaluate --use-suggested                                                    blank correct_label falls back to the assistant's PRELIMINARY label (marked, counted)
       ... candidates [--use-suggested] [-v]                                           score candidate rule changes next to the live rules

Labels for `correct_label`: funding (a startup raised or announced a round), rumour (in talks / plans to raise), hiring, spam, none (nothing
relevant), or one of the NOT-startup-funding categories: fund_close (a VC or PE fund closing), debt_or_project_finance, sponsorship_or_naming,
acquisition, grant, earnings_or_revenue, ipo_or_listing, layoffs_or_shutdown. Anything not funding is scored as `none` by the classifier
today; the categories say WHY a funding verdict was wrong. Use unsure when you cannot tell (skipped; its effect on the result is printed).

Sampling (events already in the gold set are left out; weight = stratum size / rows drawn, so results describe the whole pool):
  A  the live classifier says funding        B  it says rumour, hiring or spam
  C  it says none but the text has a funding cue (amount, raise, fund, series ...)      D  none and no cue (random draw)"""
import asyncio
import csv
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services import quality
from app.services.prefilter import classify

CSV = ROOT / "docs" / "classifier_labels.csv"
GOLD = ROOT / "docs" / "gold_set_candidates.csv"
COLS = ["row", "event_id", "stratum", "weight", "source", "title", "content", "url", "suggested", "suggested_reason", "correct_label", "note"]
POOL = """SELECT e.id::text AS event_id, r.name AS source, e.title, coalesce(e.content, '') AS content, coalesce(e.source_url, e.canonical_url) AS url,
    e.source_url, e.canonical_url FROM source_events e JOIN source_registry r ON r.id = e.source_id
    WHERE e.event_type = 'article' AND e.title IS NOT NULL ORDER BY e.id"""


async def pool_strata(exclude: set[str]) -> tuple[dict[str, list[dict]], int]:
    """The article pool in strata A to D, leaving out gold-set events and the event ids in `exclude`."""
    c = quality.cfg()["classifier_eval"]
    cue = [re.compile(x, re.IGNORECASE) for x in c["cue_regexes"]]
    gold_urls = {r["url"] for r in csv.DictReader(GOLD.open())}
    async with get_sessionmaker()() as s:
        pool = [dict(r) for r in (await s.execute(text(POOL))).mappings() if not ({r["source_url"], r["canonical_url"]} & gold_urls) and r["event_id"] not in exclude]
    await get_engine().dispose()
    strata = {"A flagged funding": [], "B flagged rumour/hiring/spam": [], "C unflagged, funding cue": [], "D unflagged, no cue": []}
    for r in pool:
        label = classify(r["title"], r["content"] or None)["label"]
        hit = any(rx.search(f"{r['title']} {r['content']}") for rx in cue)
        strata["A flagged funding" if label == "funding" else "B flagged rumour/hiring/spam" if label != "none" else "C unflagged, funding cue" if hit else "D unflagged, no cue"].append(r)
    return strata, len(pool)


async def sample() -> None:
    if CSV.exists():
        sys.exit(f"{CSV} exists: it may hold labels. Move it away to draw a new sample.")
    c = quality.cfg()["classifier_eval"]
    strata, size = await pool_strata(set())
    rng, out = random.Random(c["sample"]["seed"]), []
    for (name, group), cap in zip(strata.items(), (c["sample"][k] for k in ("flagged_funding", "flagged_other", "unflagged_cue", "unflagged_no_cue")), strict=True):
        drawn = rng.sample(group, min(len(group), cap))
        for r in drawn:
            out.append({"row": 0, "event_id": r["event_id"], "stratum": name, "weight": f"{len(group) / len(drawn):.3f}", "source": r["source"], "title": r["title"],
                        "content": " ".join(r["content"].split()), "url": r["url"], "suggested": "", "suggested_reason": "", "correct_label": "", "note": ""})
        print(f"  {name:30} pool {len(group):4}  drawn {len(drawn)}")
    rng.shuffle(out)  # the owner labels in random order, so the stratum cannot be read off the position
    for i, r in enumerate(out, 1):
        r["row"] = i
    with CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"Wrote {CSV}: {len(out)} events from a pool of {size} (gold-set events left out). Fill correct_label; see this script's header for the labels.")


VALIDATION = ROOT / "docs" / "classifier_labels_validation.csv"


async def sample_validation() -> None:
    """A second, fresh draw for checking errors hidden where the assistant and the classifier agree. It leaves out the 202 baseline events and the gold set,
    shuffles each stratum with a fixed seed and writes `draw` rows per stratum in that order. The assistant labels them in order and keeps the first
    `agree_quota` rows per stratum where its label and the classifier agree (plus any disagreement met on the way); the rest are discarded unused.
    Candidate rules are frozen first and are never edited after these rows are read."""
    if VALIDATION.exists():
        sys.exit(f"{VALIDATION} exists: it may hold labels. Move it away to draw a new sample.")
    v = quality.cfg()["classifier_eval"]["validation"]
    strata, size = await pool_strata({r["event_id"] for r in csv.DictReader(CSV.open())})
    rng, out = random.Random(v["seed"]), []
    for name, group in strata.items():
        k = name[0]
        if k not in v["draw"]:
            continue
        order = rng.sample(group, len(group))[: v["draw"][k]]
        out += [{"row": 0, "event_id": r["event_id"], "stratum": name, "weight": "1.000", "source": r["source"], "title": r["title"], "content": " ".join(r["content"].split()),
                 "url": r["url"], "suggested": "", "suggested_reason": "", "correct_label": "", "note": ""} for r in order]
        print(f"  {name:30} unsampled pool {len(group):4}  drawn {len(order)} (keep the first {v['agree_quota'][k]} agreeing)")
    for i, r in enumerate(out, 1):
        r["row"] = i
    with VALIDATION.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"Wrote {VALIDATION}: {len(out)} rows from {size} unsampled events. Rows are in draw order within each stratum; weight is 1 (raw counts only, not a pool estimate).")


def load(use_suggested: bool) -> tuple[list[dict], str]:
    """The owner's correct_label wins. With --use-suggested a blank cell falls back to the assistant's suggested label, and that row is
    marked label_source=suggested (preliminary, never verified by the owner). Without the flag, blank rows are skipped as before."""
    path = VALIDATION if "--validation" in sys.argv else CSV
    rows = list(csv.DictReader(path.open()))
    if not use_suggested and not any(r["correct_label"].strip() for r in rows):
        sys.exit(f"No rows have correct_label filled in yet ({len(rows)} rows in {path.name}).")
    for r in rows:
        r["label_source"] = "owner" if r["correct_label"].strip() else "suggested" if use_suggested and r["suggested"].strip() else "blank"
        if r["label_source"] == "suggested":
            r["correct_label"] = r["suggested"]
    return rows, "correct_label"


def provenance(rows: list[dict]) -> str:
    n = {k: sum(r["label_source"] == k for r in rows) for k in ("owner", "suggested", "blank")}
    if not n["suggested"]:
        return ""
    return (f"PRELIMINARY: {n['owner']} rows use your labels, {n['suggested']} rows fall back to the assistant's suggested label (not verified by you)"
            f"{'; ' + str(n['blank']) + ' blank rows skipped' if n['blank'] else ''}. Treat these numbers as provisional.\n")


def pct(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def delta(new, old) -> str:
    return "n/a" if new is None or old is None else f"{new - old:+.2f}"


def show(m: dict, rows: list[dict], label: str, verbose: bool) -> None:
    labels, _ = quality.classifier_labels()
    unsure = sum(r[label].strip().lower() == "unsure" for r in rows)
    print(f"{m['rows']} of {len(rows)} rows scored ({unsure} unsure, {len(rows) - m['rows'] - unsure} blank or unrecognised). " + ("Raw counts, weight 1 per row: a check on agreement rows, NOT a pool estimate." if "--validation" in sys.argv else "Numbers describe the whole pool (stratum weights)."))
    for lab in labels:
        v = m["labels"][lab]
        print(f"  {lab:8} precision {pct(v['precision'])}  recall {pct(v['recall'])}   (right {v['tp']:.1f}, false alarms {v['fp']:.1f}, missed {v['fn']:.1f})")
    print(f"  funding recall rests on {m['funding_rows_in_unflagged']} labelled funding rows the classifier did not flag.")
    if m["fp_cause"]:
        print("  Wrongly flagged as funding, by what the event really was (weighted): " + ", ".join(f"{k} {v:.1f}" for k, v in m["fp_cause"].items()))
    if m["fn_got"]:
        print("  Funding the classifier called something else (weighted): " + ", ".join(f"{k} {v:.1f}" for k, v in m["fn_got"].items()))
    if verbose:
        print("\n  Confusion (rows = label, columns = classifier; raw counts):")
        got = list(labels)
        print("  " + " " * 26 + "".join(f"{g:>9}" for g in got))
        for t in sorted({t for t, _ in m["raw"]}):
            print(f"  {t:26}" + "".join(f"{int(m['raw'].get((t, g), 0)):>9}" for g in got))
        src = {r["title"]: r.get("label_source") for r in rows}
        for w in m["wrong"]:
            print(f"  want {w['want']:24} got {w['got']:8} {w['title']}" + ("   [preliminary label]" if src.get(w["title"]) == "suggested" else ""))


def evaluate() -> None:
    rows, label = load("--use-suggested" in sys.argv)
    print(provenance(rows), end="")
    m = quality.classifier_metrics(rows, classify, label)
    show(m, rows, label, "-v" in sys.argv)
    if any(r[label].strip().lower() == "unsure" for r in rows):  # the answer must hold whichever way the unsure rows are decided
        for v in ("funding", "none"):
            alt = quality.classifier_metrics([{**r, label: v if r[label].strip().lower() == "unsure" else r[label]} for r in rows], classify, label)["labels"]["funding"]
            print(f"  funding if every unsure row is {v:7}: precision {pct(alt['precision'])}, recall {pct(alt['recall'])}")


def held_out(name: str) -> str:
    """The same candidate on the 74-row gold set, which the candidates were not written from (only the verbs announces and financing were first spotted there): funding precision and recall there."""
    gold = [r for r in csv.DictReader(GOLD.open()) if r["correct_label"].strip()]
    f = quality.gold_metrics(gold, quality.candidate_classifier(name))["labels"]["funding"]
    return f"gold set (held out) precision {pct(f['precision'])} recall {pct(f['recall'])}"


def candidates() -> None:
    rows, label = load("--use-suggested" in sys.argv)
    print(provenance(rows) + "Nothing below is applied to alerts.\n", end="")
    import hashlib
    import json
    print("Candidate rules fingerprint (sha256 of the candidates block in config/quality.yaml): "
          + hashlib.sha256(json.dumps(quality.cfg()["classifier_eval"]["candidates"], sort_keys=True).encode()).hexdigest()[:12] + (" [validation file]" if "--validation" in sys.argv else " [baseline file]"))
    base = quality.classifier_metrics(rows, quality.candidate_classifier("current"), label)
    print("This sample was drawn after the candidates were frozen: it is an independent check, but its rows are raw counts, not a pool estimate." if "--validation" in sys.argv
          else "The candidates were written after reading this sample, so its numbers flatter them; the gold-set column is the fair check.")
    print(f"{'current rules':32} funding precision {pct(base['labels']['funding']['precision'])} recall {pct(base['labels']['funding']['recall'])}   {held_out('current')}")
    b = base["labels"]["funding"]
    for name in quality.cfg()["classifier_eval"]["candidates"]:
        m = quality.classifier_metrics(rows, quality.candidate_classifier(name), label)
        f = m["labels"]["funding"]
        print(f"{name:32} funding precision {pct(f['precision'])} ({delta(f['precision'], b['precision'])}) recall {pct(f['recall'])} ({delta(f['recall'], b['recall'])}); rows now wrong {len(m['wrong'])} (was {len(base['wrong'])})\n{'':32} {held_out(name)}")
        if "-v" in sys.argv:
            was = {w["title"] for w in base["wrong"]}
            for w in m["wrong"]:
                if w["title"] not in was:
                    print(f"    newly wrong: want {w['want']:22} got {w['got']:8} {w['title']}")
            now = {w["title"] for w in m["wrong"]}
            for w in base["wrong"]:
                if w["title"] not in now:
                    print(f"    now right:   want {w['want']:22} {w['title']}")


if __name__ == "__main__":
    {"sample": lambda: asyncio.run(sample()), "sample-validation": lambda: asyncio.run(sample_validation()), "evaluate": evaluate, "candidates": candidates}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
