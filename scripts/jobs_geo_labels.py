"""Job geo accuracy (J5a): draw a labelling sample of open jobs, then score services/geo.py's India and remote flags against the owner's labels.
Usage: cd backend && uv run python ../scripts/jobs_geo_labels.py sample              write docs/jobs_geo_labels.csv (refuses to overwrite it)
       cd backend && uv run python ../scripts/jobs_geo_labels.py evaluate [-v]       score against the owner's labels (columns india, remote)
       ... evaluate --use-suggested                                                  score against the assistant's PRELIMINARY labels
Method follows scripts/india_labels.py (decision 84): stratified, `weight` = stratum size / sample size, so the numbers estimate the whole open-job pool.
Definitions used for labelling: india = y when the job is located in India; remote = y when the job is remote and a person in India can take it
(not restricted to other countries). Labels are y or n; blank rows are skipped."""
import asyncio
import csv
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services.geo import REMOTE, classify_geo

CSV = ROOT / "docs" / "jobs_geo_labels.csv"
COLS = ["job_id", "stratum", "weight", "source", "title", "location", "workplace", "flag_india", "flag_remote",
        "suggested_india", "suggested_remote", "india", "remote", "note"]
POOL = """SELECT j.id::text AS job_id, r.name AS source, j.title, coalesce(j.location, '') AS location, coalesce(j.workplace, '') AS workplace
    FROM job_postings j JOIN source_registry r ON r.id = j.source_id WHERE j.closed_at IS NULL ORDER BY j.id"""
PER_STRATUM, SEED = 17, 12
STRATA = ("A hybrid or onsite", "B ambiguous remote (no place named)", "C remote with a place named, flagged neither", "D clear India (no remote word)",
          "E remote flagged open to India", "F elsewhere (no remote word)")


def stratum(r: dict, india: bool, remote: bool) -> str:
    loc = r["location"]
    if r["workplace"] in ("hybrid", "onsite") or "hybrid" in loc.lower():
        return STRATA[0]
    if REMOTE.search(loc) and not india and remote:
        return STRATA[1] if len(loc.split()) <= 3 else STRATA[4]
    if REMOTE.search(loc) and not india and not remote:
        return STRATA[2]
    if india and not REMOTE.search(loc):
        return STRATA[3]
    if remote:
        return STRATA[4]
    return STRATA[5]


async def sample() -> None:
    if CSV.exists():
        sys.exit(f"{CSV} exists: it may hold labels. Move it away to draw a new sample.")
    async with get_sessionmaker()() as s:
        pool = [dict(r) for r in (await s.execute(text(POOL))).mappings()]
    await get_engine().dispose()
    groups: dict[str, list[dict]] = {k: [] for k in STRATA}
    for r in pool:
        i, m = classify_geo(r["location"], r["title"], r["workplace"] or None)
        groups[stratum(r, i, m)].append({**r, "flag_india": "y" if i else "n", "flag_remote": "y" if m else "n"})
    out = []
    for name, g in groups.items():
        pick = random.Random(SEED).sample(g, min(len(g), PER_STRATUM))
        for r in pick:
            out.append({**r, "stratum": name, "weight": f"{len(g) / len(pick):.3f}", "suggested_india": "", "suggested_remote": "", "india": "", "remote": "", "note": ""})
    with CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"Wrote {CSV}: {len(out)} jobs from a pool of {len(pool)} open jobs; " + ", ".join(f"{k[0]} {len(v)}" for k, v in groups.items()))


def evaluate(use_suggested: bool, verbose: bool) -> None:
    ic, rc = ("suggested_india", "suggested_remote") if use_suggested else ("india", "remote")
    rows = [r for r in csv.DictReader(CSV.open()) if r[ic] in ("y", "n") and r[rc] in ("y", "n")]
    if not rows:
        sys.exit(f"No rows have {ic}/{rc} labels yet.")
    print(f"{len(rows)} labelled jobs ({'assistant PRELIMINARY labels' if use_suggested else 'owner labels'}); weighted to the open-job pool.")
    for flag, col, name in (("flag_india", ic, "India"), ("flag_remote", rc, "Remote open to India")):
        tp = fp = fn = tn = 0.0
        for r in rows:
            w, pred, truth = float(r["weight"]), r[flag] == "y", r[col] == "y"
            tp, fp, fn, tn = tp + w * (pred and truth), fp + w * (pred and not truth), fn + w * (truth and not pred), tn + w * (not pred and not truth)
            if verbose and pred != truth:
                print(f"  {name} {'FALSE POSITIVE' if pred else 'MISSED'}: {r['location']!r} | {r['title'][:50]} | workplace={r['workplace']}")
        p, rec = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn) if tp + fn else 0
        print(f"{name}: precision {p:.1%}, recall {rec:.1%} (pool estimate: TP {tp:.0f}, FP {fp:.0f}, FN {fn:.0f}, TN {tn:.0f})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sample":
        asyncio.run(sample())
    elif len(sys.argv) > 1 and sys.argv[1] == "evaluate":
        evaluate("--use-suggested" in sys.argv, "-v" in sys.argv)
    else:
        sys.exit(__doc__)
