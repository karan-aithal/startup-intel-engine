"""Phase 11 quality report. Read-only: it never changes alerts, scores or data.
Usage: cd backend && uv run python ../scripts/quality_report.py [--save NAME] [--compare NAME] [-v]
  --save NAME     store today's top-25 funding rankings (with their timestamp basis) as docs/quality/ranking_NAME.json (the "before")
  --compare NAME  show how today's rankings moved against that file (the "after")
  --merge-map     write docs/quality/identity_merge_map.csv: proposed company merges, renames and removals with what each would move
Sections: classifier precision/recall on the gold set, latency, source contribution, India reasons, duplicate rates, rankings."""
import asyncio
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import gold_report

from app.db.session import get_engine, get_sessionmaker
from app.services import feedback, quality

Q = ROOT / "docs" / "quality"


def secs(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v:.0f}s" if v < 120 else f"{v / 60:.0f}m" if v < 7200 else f"{v / 3600:.1f}h"


def table(rows: list[list], head: list[str]) -> str:
    w = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    return "\n".join("  " + "  ".join(str(x).ljust(w[i]) for i, x in enumerate(r)) for r in [head, *rows])


def arg(name: str) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def moved(f: dict) -> dict:
    """A footprint grouped for the owner: what a merge or removal would have to move."""
    g = {"rounds": f.get("funding_rounds", 0), "evidence": f.get("evidence", 0), "claims": sum(f.get(t, 0) for t in ("funding_round_claims", "company_investor_claims", "company_founder_claims")),
         "events": f.get("source_events", 0), "boards": f.get("ats_boards", 0), "signals": f.get("company_signals", 0), "alerts": f.get("alert_deliveries", 0)}
    g["other"] = sum(f.values()) - sum(g.values())
    return g


def print_identity(d: dict) -> None:
    items = d["items"]
    print(f"\n== Company identity ({d['companies']} companies, {d['rounds']} rounds) ==")
    by = Counter(i["action"] for i in items)
    print(f"  flagged {len(items)}: merge into an existing company {by['merge']}, rename {by['rename']}, not a company {by['not_a_company']}, review {by['review_name']}")
    light = sum(1 for i in items if not moved(i["footprint"])["rounds"] and not moved(i["footprint"])["evidence"])
    print(f"  of those, {light} own no round and no evidence (only one extracted claim each): a merge moves a claim, not a round")
    print(table([[i["action"], i["confidence"], i["company"][:36], (i["survivor"] or i["cleaned_name"] or "-")[:22], i["reason"][:40], moved(i["footprint"])["rounds"],
                  moved(i["footprint"])["claims"], moved(i["footprint"])["evidence"], moved(i["footprint"])["alerts"] + i["alerts_by_name"]] for i in items],
                ["action", "conf", "company", "survivor / cleaned", "why", "rounds", "claims", "evidence", "alerts"]))
    print(f"\n  round pairs that look like one round (stage and amount compatible, currencies converted roughly): {len(d['round_pairs'])} of {d['rounds']} rounds")
    for a, b in d["round_pairs"]:
        print(f"    {a['company']}: {a['stage'] or '-'} {a['amount_original'] or '-'} {a['currency'] or ''} ({a['announced_at']:%m-%d}) + {b['stage'] or '-'} {b['amount_original'] or '-'} {b['currency'] or ''} ({b['announced_at']:%m-%d})")
        print(f"       {a['sources']}\n       {b['sources']}")


def write_merge_map(d: dict) -> None:
    import csv

    Q.mkdir(parents=True, exist_ok=True)
    f = Q / "identity_merge_map.csv"
    if n := quality.approvals_in(f):  # decisions are the owner's work: never regenerate over them
        sys.exit(f"Not written: {f} already holds {n} approval decision(s) in its approve column, and regenerating would erase them. "
                 "The file is unchanged. Move or rename it yourself if you want a fresh map.")
    cols = ["action", "confidence", "company", "company_id", "survivor", "survivor_id", "cleaned_name", "reason", "rounds", "evidence", "claims", "events", "boards", "signals",
            "alerts", "other", "alerts_by_name", "survivor_rounds", "approve"]
    with f.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for i in d["items"]:
            w.writerow({**{k: i[k] for k in ("action", "confidence", "company", "company_id", "survivor", "survivor_id", "cleaned_name", "reason", "alerts_by_name")},
                        **moved(i["footprint"]), "survivor_rounds": moved(i["survivor_footprint"])["rounds"] if i["survivor_id"] else "", "approve": ""})
    print(f"\n  Wrote {f} ({len(d['items'])} rows). Fill the approve column (yes/no) per row; nothing is changed by this report.")


async def main() -> None:
    verbose = "-v" in sys.argv
    print("== Classifier vs gold set (prefilter) ==")
    print(gold_report.report(verbose))
    async with get_sessionmaker()() as s:
        lat = await quality.latency(s)
        print("\n== Alert latency (seconds from our detection / the source's publication to Telegram) ==")
        print(table([[r["topic"], r["n"], secs(r["detect_to_sent_p50"]), secs(r["detect_to_sent_p90"]), secs(r["publish_to_sent_p50"])] for r in lat["alerts"]],
                    ["topic", "alerts", "detect>sent p50", "p90", "publish>sent p50"]))
        print("\n== Detection latency per source (publication to detection, items published in the last 7 days, last 14 days) ==")
        print(table([[r["source"], r["n"], secs(r["p50"]), secs(r["p90"])] for r in lat["detection"]], ["source", "items", "p50", "p90"]))
        print("\n== Source contribution ==")
        print(table([[r["source"], r["route"], r["events"], r["relevant"], r["alerts"], r["first_for_rounds"]] for r in (await quality.source_contribution(s))[:25]],
                    ["source", "route", "events", "relevant", "alerts", "first to report a round"]))
        print("\n== Funding/rumour events by why they count as India ==")
        print(table([[r["reason"], r["n"]] for r in await quality.india_reasons(s)], ["reason", "events"]))
        d = await quality.duplicates(s)
        print(f"\n== Duplicate rates (target under {d['target_rate']:.0%}) ==")
        for k in ("companies", "rounds", "funding_alerts"):
            v = d[k]
            print(f"  {k:15} {v['duplicates']} of {v['total']} = {v['rate']:.1%}  {'OK' if v['ok'] else 'ABOVE TARGET'}")
            for p in v["pairs"][:5] if verbose else []:
                print("     ", {x: (str(y) if y is not None else None) for x, y in p.items() if x != "dup_id"})
        ident = await quality.identity(s)
        fb = await feedback.stats(s)
        ranks = await quality.ranking(s)
        basis = await quality.timestamp_basis(s)
    await get_engine().dispose()
    print(f"\nTimestamp basis of this run: {basis}")

    print_identity(ident)
    print(f"\n== Alert feedback (precision only from {fb['min_ratings']} drawn ratings per group) ==")
    if not fb["rated"]:
        print(f"  no ratings yet. Unrated alerts: {fb['unrated']}. Rate with scripts/feedback.py (next, add).")
    for g in fb["by_topic"]:
        print(f"  {g['group']:15} drawn {g['drawn']:3} chosen {g['chosen']:3}  " + (f"{g['precision']:.0%} useful ({g['low']:.0%} to {g['high']:.0%})" if g["status"] == "measured" else "insufficient sample: no estimate"))
    if "--merge-map" in sys.argv:
        write_merge_map(ident)

    print("\n== Rankings (top 10 of 25 saved) ==")
    for col, rows in ranks.items():
        print(f" by score_{col}:")
        print(table([[i + 1, r["company"], r["stage"] or "-", r["status"], r[f"score_{col}"]] for i, r in enumerate(rows[:10])], ["#", "company", "stage", "status", "score"]))
    if name := arg("--save"):
        Q.mkdir(parents=True, exist_ok=True)
        (Q / f"ranking_{name}.json").write_text(json.dumps({"_meta": {"basis": basis, "saved": date.today().isoformat()}, **ranks}, indent=1))
        print(f"\nSaved {Q / f'ranking_{name}.json'}")
    if name := arg("--compare"):
        before = json.loads((Q / f"ranking_{name}.json").read_text())
        print(f"\n== Ranking change since '{name}' ==")
        was = before.get("_meta", {}).get("basis", quality.LEGACY_BASIS)
        print(f"  basis then: {was}; now: {basis}" + ("" if was == basis else "\n  NOTE: the bases differ, so some movement below is the timestamp correction, not a real change."))
        for col in ("global", "india"):
            c = quality.compare_rankings(before[col], ranks[col])
            shift = "n/a" if c["mean_rank_shift"] is None else f"{c['mean_rank_shift']:.2f}"
            print(f"  {col}: {'identical' if c['unchanged'] else 'changed'}; top-25 overlap {c['overlap']:.0%}, mean rank shift {shift}, "
                  f"entered {c['entered'] or '-'}, left {c['left'] or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
