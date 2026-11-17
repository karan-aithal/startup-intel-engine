"""Precision/recall of the keyword pre-filter against the human-corrected gold set.
Usage: cd backend && uv run python ../scripts/gold_report.py [-v]
Calls the same classify(title, summary) the pipeline calls. Only rows with a filled correct_label count.
Rows with an empty summary are scored on the title alone, so they understate summary-driven behaviour.
The scoring itself is app.services.quality.gold_metrics (also used by quality_report.py)."""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.prefilter import classify
from app.services.quality import gold_metrics


def report(verbose: bool = False) -> str:
    rows = [r for r in csv.DictReader((ROOT / "docs/gold_set_candidates.csv").open()) if r["correct_label"].strip()]
    if not rows:
        return "No rows have correct_label filled in yet. Fill docs/gold_set_candidates.csv first."
    m = gold_metrics(rows, classify)
    out = [f"{m['rows']} labelled rows = {m['events']:g} events, {sum(bool(r.get('summary')) for r in rows)} with a summary"]
    for lab, v in m["labels"].items():
        p = f"{v['precision']:.2f}" if v["precision"] is not None else "n/a"
        rc = f"{v['recall']:.2f}" if v["recall"] is not None else "n/a"
        out.append(f"{lab:8} precision {p}  recall {rc}  (tp {v['tp']:g}, fp {v['fp']:g}, fn {v['fn']:g}, support {v['support']:g})")
    if verbose:
        out += [f"  want {want:8} got {got:8} {title}" for want, got, title in m["wrong"]]
    return "\n".join(out)


if __name__ == "__main__":
    print(report("-v" in sys.argv))
