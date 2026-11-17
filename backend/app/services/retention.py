"""Retention and deletion. Idempotent, safe to run any time (the worker runs it hourly; restore.sh runs it after a restore).

Event rows are evidence references and stay. Once an event passes its source's `retention_expires_at` its raw content and
personal fields are removed; identifiers, links, title, dates, derived fields and the `raw_metadata` keys later code reads
(region, ignored_reason, company_id, ts_fixed) are kept, and `raw_metadata.scrubbed` marks the row done. Operational tables are pruned by age.

Deletion ledger: `forget` appends a salted-free SHA-256 of the identifier to an append-only file OUTSIDE the database
(default ~/.config/startup-intel/deletions.jsonl, mode 600, copied into every encrypted backup bundle). Every purge replays
it, so a restore can never bring a forgotten person back: the next purge (restore.sh runs one) clears them again. Only hashes are
stored, so the ledger holds no names; a hash of a common name can still be guessed, which is the accepted limit.

CLI: python -m app.retention purge | forget "<author name or profile URL>"
"""
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

SCRUB_EVENTS = """UPDATE source_events SET content = NULL, author_name = NULL, author_url = NULL,
    raw_metadata = jsonb_build_object('scrubbed', true) || coalesce((SELECT jsonb_object_agg(k, v) FROM jsonb_each(raw_metadata) AS x(k, v)
                                                                   WHERE k IN ('region', 'ignored_reason', 'company_id', 'ts_fixed')), '{}')
    WHERE retention_expires_at < now() AND NOT (raw_metadata ? 'scrubbed')"""
# Model output is a quoted slice of the event's text, so it expires with it.
SCRUB_LLM = """UPDATE llm_attempts SET raw_output = NULL WHERE raw_output IS NOT NULL AND created_at < now() - interval '90 days'"""
PRUNE = {
    "job_queue_done": "DELETE FROM job_queue WHERE status = 'done' AND finished_at < now() - interval '30 days'",
    "job_queue_dead": "DELETE FROM job_queue WHERE status = 'dead' AND finished_at < now() - interval '90 days'",
    "worker_heartbeats": "DELETE FROM worker_heartbeats WHERE last_seen_at < now() - interval '7 days'",
    "source_runs": "DELETE FROM source_runs WHERE started_at < now() - interval '90 days'",
}


def ledger_path() -> Path:
    from app.core.config import get_settings

    return Path(get_settings().deletion_ledger).expanduser()


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def ledger_hashes() -> list[str]:
    f = ledger_path()
    return [json.loads(ln)["h"] for ln in f.read_text().splitlines() if ln.strip()] if f.is_file() else []


def record_deletion(who: str) -> bool:
    """Append a deletion request. Returns False if it was already recorded."""
    h = _hash(who)
    if h in ledger_hashes():
        return False
    f = ledger_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps({"h": h, "at": datetime.now(UTC).isoformat(timespec="seconds")}) + "\n")
    return True


H = "encode(sha256(convert_to(lower(btrim({c})), 'UTF8')), 'hex')"
APPLY_LEDGER = f"""UPDATE source_events SET author_name = NULL, author_url = NULL
    WHERE (author_name IS NOT NULL OR author_url IS NOT NULL)
      AND ({H.format(c='author_name')} = ANY(:hs) OR {H.format(c='author_url')} = ANY(:hs))"""


async def apply_ledger(s) -> int:
    """Clear every person on the ledger from stored events. Idempotent; this is what makes deletions survive a restore."""
    hs = ledger_hashes()
    return (await s.execute(text(APPLY_LEDGER), {"hs": hs})).rowcount if hs else 0


async def purge(s) -> dict[str, int]:
    out = {"events_scrubbed": (await s.execute(text(SCRUB_EVENTS))).rowcount,
           "llm_output_scrubbed": (await s.execute(text(SCRUB_LLM))).rowcount,
           "ledger_cleared": await apply_ledger(s)}
    for name, sql in PRUNE.items():
        out[name] = (await s.execute(text(sql))).rowcount
    return out


async def forget(s, who: str) -> int:
    """Deletion request for a person: record it on the ledger, then clear their name and profile link from every stored event.
    Exact match, case-insensitive. Recorded first, so a crash after this line still leaves the request on the ledger."""
    record_deletion(who)
    return (await s.execute(text(APPLY_LEDGER), {"hs": [_hash(who)]})).rowcount


async def _cli(argv: list[str]) -> int:
    from app.db.session import get_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as s:
            if argv[:1] == ["purge"]:
                print(await purge(s))
            elif len(argv) == 2 and argv[0] == "forget":
                print({"events_cleared": await forget(s, argv[1])})
            else:
                print(__doc__)
                return 2
            await s.commit()
        return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli(sys.argv[1:])))
