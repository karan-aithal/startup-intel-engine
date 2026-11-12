"""Precision/recall of the keyword pre-filter against the human-corrected gold set.
Usage: cd backend && uv run python ../scripts/gold_report.py
Reads docs/gold_set_candidates.csv; only rows with a filled correct_label count."""
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
tp, fp, fn = Counter(), Counter(), Counter()
for r in rows:
    got, want = classify(r["title"])["label"], r["correct_label"].strip().lower()
    if got == want:
        tp[want] += 1
    else:
        fp[got] += 1
        fn[want] += 1
print(f"{len(rows)} labelled rows")
for lab in ("funding", "rumour", "hiring"):
    p = tp[lab] / (tp[lab] + fp[lab]) if tp[lab] + fp[lab] else float("nan")
    r = tp[lab] / (tp[lab] + fn[lab]) if tp[lab] + fn[lab] else float("nan")
    print(f"{lab:8} precision {p:.2f}  recall {r:.2f}  (tp {tp[lab]}, fp {fp[lab]}, fn {fn[lab]})")
