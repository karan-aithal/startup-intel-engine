"""One-time import (J7, decision 111): your `save` and `applied` feedback marks become Pipeline jobs (saved, applied).
Dry run by default: prints what it would do and writes nothing. `--apply` does it; running it again imports nothing new.
Usage: cd backend && uv run python ../scripts/pipeline_import.py [--apply]"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import get_engine, get_sessionmaker
from app.services import tracked


async def main(apply: bool) -> None:
    async with get_sessionmaker()() as s:
        out = await tracked.import_marks(s, dry_run=not apply)
        await s.commit()
    await get_engine().dispose()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
