"""Benchmark the local model on the labelled gold set, next to the keyword filter.
Usage: cd backend && uv run python ../scripts/llm_benchmark.py [--model qwen3.5-4b] [-n 20] [-v]
Prints per-label precision/recall, rejected outputs, and latency. Rows sharing an event_group count once."""
import asyncio
import csv
import statistics
import sys
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings
from app.services.llm import extract
from app.services.prefilter import classify


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


async def main():
    cfg, model, limit = get_settings(), arg("--model", get_settings().llm_model), int(arg("-n", 0))
    rows = [r for r in csv.DictReader((ROOT / "docs/gold_set_candidates.csv").open()) if r["correct_label"].strip()]
    seen, uniq = set(), []
    for r in rows:  # one row per event group
        if r.get("event_group") and r["event_group"] in seen:
            continue
        seen.add(r.get("event_group"))
        uniq.append(r)
    rows = uniq[:limit] if limit else uniq
    tp, fp, fn, bad, lat, wrong = Counter(), Counter(), Counter(), 0, [], []
    kw_ok = 0
    fields, funding_n = Counter(), 0
    async with httpx.AsyncClient() as c:
        for i, r in enumerate(rows, 1):
            want = r["correct_label"].strip().lower()
            o = await extract(c, cfg.llm_base_url, model, "funding", f"{r['title']}\n{r.get('summary') or ''}")
            lat.append(sum(a["latency_ms"] or 0 for a in o["attempts"]))
            got = o["result"].get("label", "invalid") if o["valid"] else "invalid"
            bad += not o["valid"]
            if o["valid"] and got == "funding":
                funding_n += 1
                for k, st in o["result"]["field_status"].items():
                    fields[k + ":" + (st[0] if isinstance(st, list) and st else st).split(":")[0]] += 1
            kw_ok += classify(r["title"], r.get("summary") or None)["label"] == want
            if got == want:
                tp[want] += 1
            else:
                fp[got] += 1
                fn[want] += 1
                wrong.append((want, got, r["title"][:80]))
            print(f"\r{i}/{len(rows)}", end="", flush=True)
    print(f"\n{model}: {len(rows)} events, accuracy {sum(tp.values()) / len(rows):.2f} (keyword filter {kw_ok / len(rows):.2f}), "
          f"rejected {bad}, median {statistics.median(lat) / 1000:.1f}s, p95 {sorted(lat)[int(len(lat) * .95) - 1] / 1000:.1f}s")
    print(f"field grounding on {funding_n} funding events: " + ", ".join(f"{k} {v}" for k, v in sorted(fields.items())))
    print("GATE (accuracy >= 0.95):", "PASS" if sum(tp.values()) / len(rows) >= 0.95 else "FAIL")
    for lab in ("funding", "rumour", "hiring", "none"):
        n = tp[lab] + fn[lab]
        p = f"{tp[lab] / (tp[lab] + fp[lab]):.2f}" if tp[lab] + fp[lab] else "n/a"
        print(f"{lab:8} precision {p}  recall {tp[lab] / n:.2f}  support {n}" if n else f"{lab:8} no rows")
    if "-v" in sys.argv:
        for w in wrong:
            print("  want %-8s got %-8s %s" % w)


asyncio.run(main())
