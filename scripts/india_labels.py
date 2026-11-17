"""India relevance: draw a labelling sample, then score the current rule and candidate rules against the labels. Read-only for alerts.
Usage: cd backend && uv run python ../scripts/india_labels.py sample              write docs/india_labels.csv (refuses to overwrite it)
       cd backend && uv run python ../scripts/india_labels.py evaluate [-v]       score the rules against the owner's labels
       ... evaluate --use-suggested                                               score against the assistant's PRELIMINARY labels

Definition used for labelling (the default; change it and say so): an event is India-relevant when the company is incorporated or
headquartered in India, or the round is led by an India-based investor or aimed at India. Labels: y, n, or unsure (skipped).

The sample is stratified so precision and recall can be estimated for the whole pool of funding and rumour events:
  A  every event the current rule flags as India
  B  every event it does not flag that still contains India words or cues
  C  a random draw of the remaining events
`weight` is pool size / sample size for the stratum, so evaluate reports estimates for the pool, not for the sample."""
import asyncio
import csv
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services import quality

CSV = ROOT / "docs" / "india_labels.csv"
COLS = ["event_id", "stratum", "weight", "source", "title", "summary", "url", "region", "location", "flagged_by_current_rule",
        "suggested", "suggested_reason", "is_india_relevant", "note"]
POOL = """SELECT e.id::text AS event_id, r.name AS source, e.title, left(coalesce(e.content, ''), 600) AS summary, e.source_url AS url,
    coalesce(e.raw_metadata->>'region', '') AS region, coalesce(e.location, '') AS location
    FROM source_events e JOIN source_registry r ON r.id = e.source_id
    WHERE e.processing_status IN ('funding', 'rumour') AND e.event_type <> 'job' ORDER BY e.id"""
TARGET, SEED = 50, 12


async def sample() -> None:
    if CSV.exists():
        sys.exit(f"{CSV} exists: it may hold labels. Move it away to draw a new sample.")
    async with get_sessionmaker()() as s:
        pool = [dict(r) for r in (await s.execute(text(POOL))).mappings()]
    await get_engine().dispose()
    rules = quality.india_rules()
    current = rules["current (source region OR location text)"]
    cues = rules["candidate: current OR India words and cues (Indian, Rs, crore, rupee sign)"]
    a = [r for r in pool if current(r)]
    b = [r for r in pool if not current(r) and cues(r)]
    rest = [r for r in pool if not current(r) and not cues(r)]
    c = random.Random(SEED).sample(rest, min(len(rest), max(12, TARGET - len(a) - len(b))))
    out = []
    for name, group, size in (("A flagged", a, len(a)), ("B unflagged, India words", b, len(b)), ("C unflagged, no India words", c, len(rest))):
        for r in group:
            out.append({**r, "stratum": name, "weight": f"{size / len(group):.3f}", "flagged_by_current_rule": "y" if current(r) else "n",
                        "suggested": "", "suggested_reason": "", "is_india_relevant": "", "note": ""})
    with CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"Wrote {CSV}: {len(out)} events (A {len(a)}, B {len(b)}, C {len(c)} of {len(rest)}) from a pool of {len(pool)}.")
    print("Fill is_india_relevant with y, n or unsure for each row, using the definition in this script's header.")


def evaluate() -> None:
    use_suggested = "--use-suggested" in sys.argv
    rows = list(csv.DictReader(CSV.open()))
    label = "suggested" if use_suggested else "is_india_relevant"
    done = [r for r in rows if r[label].strip().lower() in ("y", "n")]
    if not done:
        sys.exit(f"No rows have {label} filled in yet ({len(rows)} rows in {CSV.name}).")
    unsure = sum(r[label].strip().lower() == "unsure" for r in rows)
    print(("PRELIMINARY: scored against the assistant's suggested labels, not yours.\n" if use_suggested else "")
          + f"{len(done)} of {len(rows)} rows labelled y or n ({unsure} unsure, {len(rows) - len(done) - unsure} blank). "
          "Estimates are for the whole pool (stratum weights).")
    res = quality.india_metrics(rows, label)
    print(f"India-relevant events in the pool, estimated: {res[0]['positives']:.1f} of {sum(float(r['weight']) for r in done):.0f}\n")
    pct = lambda v: "n/a" if v is None else f"{v:.2f}"
    for m in res:
        print(f"  {m['rule'][:78]:78} precision {pct(m['precision'])}  recall {pct(m['recall'])}   (right {m['tp']:.1f}, false alarms {m['fp']:.1f}, missed {m['fn']:.1f})")
    if unsure:  # the answer must hold whichever way the unsure rows are decided
        lo_hi = [quality.india_metrics([{**r, label: v if r[label].strip().lower() == "unsure" else r[label]} for r in rows], label)[0] for v in ("n", "y")]
        print(f"\n  current rule if every unsure row is n / y: precision {pct(lo_hi[0]['precision'])} / {pct(lo_hi[1]['precision'])}, "
              f"recall {pct(lo_hi[0]['recall'])} / {pct(lo_hi[1]['recall'])}")
    print("\nErrors of the current rule (rows as labelled, unweighted):")
    for kind, title in res[0]["wrong"]:
        print(f"  {kind:11} {title}")
    if "-v" in sys.argv:
        for m in res[1:]:
            print(f"\n{m['rule']}:")
            for kind, title in m["wrong"]:
                print(f"  {kind:11} {title}")


if __name__ == "__main__":
    {"sample": lambda: asyncio.run(sample()), "evaluate": evaluate}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
