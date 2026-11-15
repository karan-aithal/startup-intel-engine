"""Precision/recall of the keyword pre-filter against the human-corrected gold set.
Usage: cd backend && uv run python ../scripts/gold_report.py [-v]
Calls the same classify(title, summary) the pipeline calls. Only rows with a filled correct_label count.
Rows with an empty summary are scored on the title alone, so they understate summary-driven behaviour."""
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.prefilter import classify

rows = [r for r in csv.DictReader((ROOT / "docs/gold_set_candidates.csv").open()) if r["correct_label"].strip()]
if not rows:
    sys.exit("No rows have correct_label filled in yet. Fill docs/gold_set_candidates.csv first.")
# Rows sharing an event_group are one event covered by several sources: each group counts once in total.
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
print(f"{len(rows)} labelled rows = {sum(1 / size[r['event_group']] if r.get('event_group') else 1 for r in rows):g} events, {sum(bool(r.get('summary')) for r in rows)} with a summary")
for lab in ("funding", "rumour", "hiring"):
    n = tp[lab] + fn[lab]
    p = f"{tp[lab] / (tp[lab] + fp[lab]):.2f}" if tp[lab] + fp[lab] else "n/a"
    rc = f"{tp[lab] / n:.2f}" if n else "n/a"
    print(f"{lab:8} precision {p}  recall {rc}  (tp {tp[lab]:g}, fp {fp[lab]:g}, fn {fn[lab]:g}, support {n:g})")
if "-v" in sys.argv:
    for want, got, title in wrong:
        print(f"  want {want:8} got {got:8} {title}")
