"""Pull live items from approved feeds and pre-label them with the rule filter, for human correction.
Usage: cd backend && uv run python ../scripts/gold_candidates.py [N]  -> docs/gold_set_candidates.csv"""
import asyncio, csv, sys
from pathlib import Path

import yaml

from app.connectors.feed import FeedAdapter
from app.services.prefilter import classify

ROOT = Path(__file__).resolve().parents[1]


async def main(n: int) -> None:
    feeds = yaml.safe_load((ROOT / "config/sources.yaml").read_text())["feeds"]
    rows = []
    for f in feeds:
        a = FeedAdapter(f["name"], f["url"], f["region"])
        try:
            res = await a.fetch()
        except Exception as e:  # noqa: BLE001
            print("skip", f["name"], e); continue
        for item in res.items[:15]:
            ev = await a.normalize(item)
            if ev:
                c = classify(ev.title, ev.content)
                rows.append([f["name"], f["region"], ev.title, ev.source_url, c["label"], c["amount"] or "", ""])
        await asyncio.sleep(1)
    # favour a balanced sample: all non-"none" first, then fill with "none"
    rows.sort(key=lambda r: r[4] == "none")
    keep = [r for r in rows if r[4] != "none"][: n * 2 // 3] + [r for r in rows if r[4] == "none"][: n // 3]
    out = ROOT / "docs/gold_set_candidates.csv"
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "region", "title", "url", "auto_label", "amount", "correct_label"])
        w.writerows(keep)
    print(f"{len(keep)} rows -> {out}")


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 80))
