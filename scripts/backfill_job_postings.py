"""Jobs track J1: create a job record (job_postings) for every existing job event. Dry run by default.
Usage: cd backend && uv run python ../scripts/backfill_job_postings.py [--apply]
  (no flag)  inside one transaction: audit, backfill, audit again, print both, then ROLL BACK (nothing is saved)
  --apply    the same, committed. Refuses unless a database backup from the last 2 hours exists (run scripts/backup.sh first)
Idempotent (existing records are kept). Fails the run when the records written do not match the audit's expected count, or an event is left
without a record. Needs migration 0011. Closing is not done here: the next poll of each ATS board closes the jobs it no longer lists."""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.session import get_engine, get_sessionmaker
from app.services import jobs


def fresh_backup(hours: float = 2) -> Path | None:
    d = Path(os.environ.get("BACKUP_DIR", Path.home() / "backups" / "startup-intel")) / "daily"
    newest = max(d.glob("*.dump"), key=lambda p: p.stat().st_mtime, default=None)
    return newest if newest and time.time() - newest.stat().st_mtime < hours * 3600 else None


async def main(apply: bool) -> int:
    backup = fresh_backup()
    if apply and not backup:
        print("Refused: no database backup from the last 2 hours. Run scripts/backup.sh first.")
        return 1
    async with get_sessionmaker()() as s:
        before = await jobs.audit(s)
        written = await jobs.backfill(s)
        after = await jobs.audit(s)
        print(f"{'APPLY, backup ' + backup.name if apply else 'DRY RUN, nothing is saved'}")
        print(f"before: {before}")
        print(f"records to write: {written}")
        print(f"after:  {after}")
        ok = after["records"] == after["expected_records"] and after["events_without_record"] == 0
        print(f"audit: {'OK, every job event has a record and the counts match' if ok else 'MISMATCH, not committed'}")
        if apply and ok:
            await s.commit()
            print("committed")
        else:
            await s.rollback()
    await get_engine().dispose()
    return 0 if ok else 2


sys.exit(asyncio.run(main("--apply" in sys.argv)))
