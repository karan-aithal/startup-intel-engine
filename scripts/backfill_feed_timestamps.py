"""Corrects the old feed timestamps. Dry run by default: it shows what would change and writes nothing.
Usage: cd backend && uv run python ../scripts/backfill_feed_timestamps.py [--offset-seconds 19800] [-v] [--apply]
--apply: refuses to run if the safety check fails, takes a database backup first (and stops if that fails), moves every unfixed feed
event in one transaction and marks it ts_fixed (so a second run finds nothing), then waits for the verification loop to recompute the
funding rounds and checks the result. Run it with the worker up: new events are already marked and are never touched.

Background: until the Phase 12 fix, feed.py stored every feed `published_at` 5.5 h early (this host is IST). Events written since the
fix carry raw_metadata.ts_fixed and are correct. The affected rows are feed-route events with a published_at and no ts_fixed marker.

What the script checks before anyone is asked to approve an apply:
  1. which events would move, per source, with examples;
  2. that the shift is safe: no event would land in the future of its own detection time (the evidence the offset is real);
  3. which funding rounds' announced_at would move (verification recomputes them from claims every 5 minutes);
  4. the edge cases a uniform shift can still disturb: a round whose month changes (that month is part of the round's identity
     key) and claim pairs that cross the cluster window (a shifted feed claim next to an unshifted one from an API source)."""
import asyncio
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services import scoring

OFFSET = int(sys.argv[sys.argv.index("--offset-seconds") + 1]) if "--offset-seconds" in sys.argv else 19800
VERBOSE = "-v" in sys.argv
APPLY = "--apply" in sys.argv

ELIGIBLE = "r.connector_type = 'feed' AND e.published_at IS NOT NULL AND NOT (e.raw_metadata ? 'ts_fixed')"

# Each claim's "seen" time (verify.py uses coalesce(published_at, detected_at)) before and after the shift.
SEEN = f"""SELECT c.id AS claim_id, c.company_id, c.funding_round_id, coalesce(e.published_at, e.detected_at) AS old_seen,
    CASE WHEN {ELIGIBLE} THEN e.published_at + make_interval(secs => :off) ELSE coalesce(e.published_at, e.detected_at) END AS new_seen,
    ({ELIGIBLE}) AS shifted
    FROM funding_round_claims c JOIN source_events e ON e.id = c.source_event_id JOIN source_registry r ON r.id = e.source_id
    WHERE c.status IN ('unverified', 'verified')"""


async def main() -> None:
    p = {"off": OFFSET}
    async with get_sessionmaker()() as s:
        q = lambda sql, **kw: s.execute(text(sql), {**p, **kw})
        print(("APPLY" if APPLY else "DRY RUN. Nothing is written.") + f" Offset: {OFFSET} s ({OFFSET / 3600:g} h)\n")

        print("== 1. Events that would move ==")
        rows = (await q(f"""SELECT r.name, count(*) AS n, min(e.published_at) AS first_pub, max(e.published_at) AS last_pub
            FROM source_events e JOIN source_registry r ON r.id = e.source_id WHERE {ELIGIBLE} GROUP BY 1 ORDER BY 2 DESC""")).all()
        for r in rows:
            print(f"  {r.name:24} {r.n:5}  published {r.first_pub:%Y-%m-%d %H:%M} .. {r.last_pub:%Y-%m-%d %H:%M} UTC")
        total = sum(r.n for r in rows)
        fixed = (await q("SELECT count(*) FROM source_events e JOIN source_registry r ON r.id = e.source_id WHERE r.connector_type = 'feed' AND e.raw_metadata ? 'ts_fixed'")).scalar_one()
        print(f"  total to move: {total}; feed events already correct (ts_fixed): {fixed}")
        if VERBOSE:
            for r in (await q(f"""SELECT r.name, e.title, e.published_at AS old, e.published_at + make_interval(secs => :off) AS new
                FROM source_events e JOIN source_registry r ON r.id = e.source_id WHERE {ELIGIBLE} ORDER BY e.detected_at DESC LIMIT 5""")).all():
                print(f"    {r.name}: {r.old:%m-%d %H:%M} -> {r.new:%m-%d %H:%M}  {r.title[:70]}")

        print("\n== 2. Safety: would any moved event land after its own detection time? ==")
        bad = (await q(f"""SELECT count(*) FROM source_events e JOIN source_registry r ON r.id = e.source_id
            WHERE {ELIGIBLE} AND e.published_at + make_interval(secs => :off) > e.detected_at""")).scalar_one()
        late = (await q(f"""SELECT count(*) FROM source_events e JOIN source_registry r ON r.id = e.source_id
            WHERE {ELIGIBLE} AND e.published_at + make_interval(secs => :off) > e.detected_at + interval '1 hour'""")).scalar_one()
        print(f"  after the shift in the future: {bad} (more than 1 h in the future: {late}) of {total}")
        print("  " + ("OK: the offset is consistent with the data." if bad == 0 else
                      "CHECK: some events were seen before their corrected time. They are listed below, and the offset is not safe to apply until they are explained."))
        if bad:
            for r in (await q(f"""SELECT r.name, e.title, e.published_at + make_interval(secs => :off) AS new, e.detected_at
                FROM source_events e JOIN source_registry r ON r.id = e.source_id
                WHERE {ELIGIBLE} AND e.published_at + make_interval(secs => :off) > e.detected_at
                ORDER BY e.published_at + make_interval(secs => :off) - e.detected_at DESC LIMIT 10""")).all():
                print(f"    {r.name}: corrected {r.new:%m-%d %H:%M} > detected {r.detected_at:%m-%d %H:%M}  {r.title[:60]}")

        print("\n== 3. Funding rounds whose announced_at would move ==")
        rounds = (await q(f"""WITH seen AS ({SEEN})
            SELECT f.id, co.name, f.announced_at, min(seen.old_seen) AS old_min, min(seen.new_seen) AS new_min,
                   to_char(min(seen.old_seen) AT TIME ZONE 'UTC', 'YYYY-MM') AS old_month, to_char(min(seen.new_seen) AT TIME ZONE 'UTC', 'YYYY-MM') AS new_month
            FROM seen JOIN funding_rounds f ON f.id = seen.funding_round_id JOIN companies co ON co.id = f.company_id
            GROUP BY f.id, co.name, f.announced_at ORDER BY co.name""")).all()
        moved = [r for r in rounds if r.new_min != r.old_min]
        print(f"  rounds with claims: {len(rounds)}; announced_at would move for {len(moved)}; unchanged (earliest claim is not a feed item): {len(rounds) - len(moved)}")
        mism = [r for r in rounds if r.announced_at != r.old_min]
        print(f"  sanity: announced_at equals the earliest claim time for {len(rounds) - len(mism)} of {len(rounds)} rounds"
              + ("" if not mism else f" (differs for {len(mism)}: they use another rule, listed with -v)"))
        for r in (mism if VERBOSE else []):
            print(f"    differs: {r.name[:34]:34} announced {r.announced_at:%m-%d %H:%M} vs earliest claim {r.old_min:%m-%d %H:%M}")
        for r in (moved if VERBOSE else moved[:5]):
            print(f"    {r.name[:34]:34} {r.old_min:%m-%d %H:%M} -> {r.new_min:%m-%d %H:%M}")
        if not VERBOSE and len(moved) > 5:
            print(f"    ... {len(moved) - 5} more (-v lists all)")
        month = [r for r in moved if r.old_month != r.new_month]
        print(f"  round-identity month changes: {len(month)}" + ("" if not month else "  <- the key is company|currency|amount|YYYY-MM, so these could split into a new round"))
        for r in month:
            print(f"    {r.name}: {r.old_month} -> {r.new_month}")

        print("\n== 4. Cluster-window edge cases ==")
        win = scoring.cfg()["verification"]["cluster_window_days"]
        pairs = (await q(f"""WITH seen AS ({SEEN})
            SELECT co.name, abs(extract(epoch from a.old_seen - b.old_seen)) AS d_old, abs(extract(epoch from a.new_seen - b.new_seen)) AS d_new
            FROM seen a JOIN seen b ON a.company_id = b.company_id AND a.claim_id < b.claim_id JOIN companies co ON co.id = a.company_id
            WHERE a.company_id IS NOT NULL AND a.shifted <> b.shifted""")).all()
        w = win * 86400
        flips = [r for r in pairs if (r.d_old <= w) != (r.d_new <= w)]
        print(f"  claim pairs of one company mixing a moved and an unmoved claim: {len(pairs)}; crossing the {win}-day cluster window: {len(flips)}")
        for r in flips:
            print(f"    {r.name}: gap {r.d_old / 86400:.2f} d -> {r.d_new / 86400:.2f} d")
        print("  (pairs of two feed claims keep their gap: they shift together.)")
        print("\nNot checked here: company_signals timing for funding_then_hiring (90-day window, a 5.5 h edge) and already-sent alert text,")
        print("which keeps its original 'Published' line by design.")
        if APPLY:
            if bad or not total:
                sys.exit(f"\nNot applying: {'the safety check failed' if bad else 'nothing to move'}.")
            await apply(s, total)
    await get_engine().dispose()


UPDATE_SQL = f"""UPDATE source_events e SET published_at = e.published_at + make_interval(secs => :off),
    raw_metadata = e.raw_metadata || '{{"ts_fixed": true}}'::jsonb FROM source_registry r WHERE r.id = e.source_id AND {ELIGIBLE}"""

ROUNDS = """SELECT f.id::text AS id, f.verification_status AS status, f.announced_at,
    (SELECT min(coalesce(e.published_at, e.detected_at)) FROM funding_round_claims c JOIN source_events e ON e.id = c.source_event_id
     WHERE c.funding_round_id = f.id AND c.status IN ('unverified', 'verified')) AS earliest
    FROM funding_rounds f"""


async def apply(s, total: int) -> None:
    before = {r.id: r for r in (await s.execute(text(ROUNDS))).all()}
    print("\n== APPLY ==\n  1. backup")
    env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:{os.environ['PATH']}"}
    await asyncio.to_thread(subprocess.run, [str(ROOT / "scripts" / "backup.sh")], check=True, env=env)  # a failure here stops everything
    res = await s.execute(text(UPDATE_SQL), {"off": OFFSET})
    if res.rowcount != total:
        await s.rollback()
        sys.exit(f"Rolled back: moved {res.rowcount} rows, expected {total}.")
    await s.commit()
    print(f"  2. moved {res.rowcount} events (one transaction, marked ts_fixed)")
    print("  3. waiting for verification to recompute the rounds (it runs every 5 minutes) ...")
    deadline = time.monotonic() + 15 * 60
    while True:
        after = {r.id: r for r in (await s.execute(text(ROUNDS))).all()}
        await s.rollback()  # end the read transaction so the next poll sees the worker's commits
        stale = [r for r in after.values() if r.earliest and r.announced_at != r.earliest]
        if not stale or time.monotonic() > deadline:
            break
        await asyncio.sleep(20)
    print(f"  4. rounds: {len(before)} before, {len(after)} after; announced_at now matches the corrected claim times for {len(after) - len(stale)} of {len(after)}"
          + ("" if not stale else f"  <- {len(stale)} NOT yet recomputed (worker down?)"))
    moved = sum(1 for k, r in before.items() if k in after and after[k].announced_at != r.announced_at)
    changed = [(k, before[k].status, after[k].status) for k in before if k in after and before[k].status != after[k].status]
    print(f"     announced_at changed on {moved} rounds; status counts before {dict(Counter(r.status for r in before.values()))}, after {dict(Counter(r.status for r in after.values()))}")
    print(f"     rounds whose status changed: {len(changed)}" + ("" if not changed else f" {changed[:5]} (new evidence may also change a status; review them)"))
    print("  Next: scripts/quality_report.py --save baseline-ts-corrected --compare baseline-2026-11-17")


if __name__ == "__main__":
    asyncio.run(main())
