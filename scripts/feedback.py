"""Rate the alerts you receive. Records your opinion in the `feedback` table and nothing else: alerts, events, rounds, scores,
verification status, ranking and company watch/mute flags are never changed. Local only, no network.

  cd backend && uv run python ../scripts/feedback.py recent [--topic "India Funding"] [--n 20]   alerts with their short reference
  ... next [--topic T] [--more]                random unrated alert, spread evenly across the four topics (daily budget in config)
  ... add ACTION REF [--why CODE] [--of REF] [--text "..."] [--drawn]
  ... undo REF [--action ACTION]               remove one feedback row (default: the rating); the alert itself is untouched
  ... stats                                    ratings so far; precision only from 30 drawn ratings per topic or source

ACTION: useful, irrelevant, incorrect, duplicate (a rating; a new one replaces the old), or watch, mute, save, applied (marks only).
incorrect needs --why: wrong_label, wrong_company, wrong_amount, wrong_stage, not_startup_funding, not_india, other. duplicate needs --of REF.
Add --drawn when you are rating the alert `next` gave you; leave it off for alerts you picked yourself (those are kept apart in stats)."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.services import feedback


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recent")
    r.add_argument("--topic")
    r.add_argument("--n", type=int, default=20)
    n = sub.add_parser("next")
    n.add_argument("--topic")
    n.add_argument("--more", action="store_true", help="draw beyond today's budget")
    a = sub.add_parser("add")
    a.add_argument("action")
    a.add_argument("ref")
    a.add_argument("--why")
    a.add_argument("--of")
    a.add_argument("--text")
    a.add_argument("--drawn", action="store_true")
    u = sub.add_parser("undo")
    u.add_argument("ref")
    u.add_argument("--action")
    sub.add_parser("stats")
    return p


def pct(g: dict) -> str:
    if g["status"] == "insufficient":
        return f"insufficient sample ({g['drawn']} of {feedback.cfg()['min_ratings']} drawn ratings): no estimate"
    return f"{g['precision']:.0%} useful, 95% interval {g['low']:.0%} to {g['high']:.0%} (from {g['drawn']} drawn ratings)"


async def run(args) -> int:
    tz = get_settings().display_timezone
    try:
        async with get_sessionmaker()() as s:
            if args.cmd == "recent":
                for r in await feedback.recent(s, args.topic, args.n):
                    print(f"{r['ref']}  {r['created_at']:%m-%d %H:%M}  {r['topic']:15} {r['source'] or '':22.22} {r['title'] or ''}" + (f"   [{r['marks']}]" if r["marks"] else ""))
            elif args.cmd == "next":
                d = await feedback.draw(s, tz, args.topic, args.more)
                done = sum((await feedback.drawn_today(s, tz)).values())
                if d is None:
                    print(f"Nothing to draw: today's {feedback.cfg()['daily_budget']} are done ({done} rated), or no unrated alert is left. --more draws beyond the budget.")
                else:
                    print(f"{d['ref']}  {d['topic']}  {d['source']}\n  {d['title']}\n  ({done} of {feedback.cfg()['daily_budget']} drawn today)\n"
                          f"  rate it:  feedback.py add useful {d['ref']} --drawn     (or irrelevant, incorrect --why CODE, duplicate --of REF)")
            elif args.cmd == "add":
                print(await feedback.add(s, args.action, args.ref, why=args.why, of=args.of, note=args.text, drawn=args.drawn))
            elif args.cmd == "undo":
                print(await feedback.undo(s, args.ref, args.action))
            else:
                st = await feedback.stats(s)
                print(f"{st['rated']} alerts rated. Precision is reported only from {st['min_ratings']} DRAWN ratings per group; chosen ratings are counted but not used.")
                for title, groups in (("By topic", st["by_topic"]), ("By source", st["by_source"])):
                    print(f"\n{title}:")
                    for g in groups:
                        print(f"  {g['group']:26} chosen {g['chosen']:3}  {pct(g)}")
                    if not groups:
                        print("  no ratings yet")
                print(f"\nUnrated alerts (the pool `next` draws from): {st['unrated']}")
                if st["why"] or st["duplicates"] or st["marks"]:
                    print(f"Incorrect, by reason: {st['why']}; duplicates flagged: {st['duplicates']}; marks: {st['marks']}")
            await s.commit()
        return 0
    except feedback.FeedbackError as e:
        print(f"Not done: {e}", file=sys.stderr)
        return 2
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parser().parse_args())))
