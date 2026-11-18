"""Phase 13: apply the owner's approved identity decisions (docs/quality/identity_merge_map.csv). Dry run by default.
Usage: cd backend && uv run python ../scripts/company_merges.py [--apply] [--undo ID] [--list]
  (no flag)  run every approved change inside one transaction, print what it would move and how rounds collapse, then ROLL BACK
  --apply    the same, committed. Refuses unless a database backup from the last 2 hours exists (run scripts/backup.sh first)
  --undo ID  reverse one audited change (newest first when changes share a company)
  --list     show the audit trail
Nothing is deleted: merged-away and not-a-company rows are only hidden. No alert, score or classifier setting is touched."""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services import company_merges as cm

MAP = ROOT / "docs" / "quality" / "identity_merge_map.csv"


def fresh_backup(hours: float = 2) -> Path | None:
    d = Path(os.environ.get("BACKUP_DIR", Path.home() / "backups" / "startup-intel")) / "daily"
    newest = max(d.glob("*.dump"), key=lambda p: p.stat().st_mtime, default=None)
    return newest if newest and time.time() - newest.stat().st_mtime < hours * 3600 else None


def show(r: dict) -> None:
    what = {"merge": f"merge into {r['arg']}", "rename": f"rename to {r['arg']}", "exclude": f"not a company ({r['arg'] or 'no detail'})"}[r["action"]]
    print(f"\n  row {r['row']:>2}  {r['company']}  ->  {what}")
    if r["action"] == "merge":
        print(f"      moved {r['moved'] or '{}'}; left behind {r['left_behind'] or 'none'}")
        print(f"      rounds before: source {r['rounds_before']['source'] or '-'} | survivor {r['rounds_before']['survivor'] or '-'}")
        print(f"      rounds after:  survivor {r['rounds_after'] or '-'}   (verification: {r['verification']})")


async def main() -> int:
    sm = get_sessionmaker()
    try:
        async with sm() as s:
            if "--list" in sys.argv:
                for m in (await s.execute(text("SELECT id, action, old_name, new_name, created_at, undone_at FROM company_merges ORDER BY created_at"))).all():
                    print(m.id, m.action, m.old_name, "->", m.new_name or "", f"{m.created_at:%Y-%m-%d %H:%M}", "UNDONE" if m.undone_at else "")
                return 0
            if "--undo" in sys.argv:
                print(await cm.undo(s, sys.argv[sys.argv.index("--undo") + 1]))
                await s.commit()
                return 0
            apply = "--apply" in sys.argv
            if apply and not (b := fresh_backup()):
                print("Refused: no database backup from the last 2 hours. Run scripts/backup.sh first.")
                return 1
            decisions = cm.read_decisions(MAP)
            errors = await cm.resolve(s, decisions)
            if errors:
                print("Stopped before writing anything:\n  " + "\n  ".join(errors))
                return 1
            todo = [d for d in decisions if not d.get("skip")]
            print(f"{len(decisions)} approved decisions, {len(decisions) - len(todo)} already applied, {len(todo)} to run ({'APPLY, backup ' + b.name if apply else 'DRY RUN, nothing is saved'})")
            for d in todo:
                show(await cm.apply_one(s, d))
            if apply:
                await s.commit()
                print("\nCommitted. Undo any change with --undo <id> (see --list).")
            else:
                await s.rollback()
                print("\nRolled back. Review the above, then re-run with --apply.")
        return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
