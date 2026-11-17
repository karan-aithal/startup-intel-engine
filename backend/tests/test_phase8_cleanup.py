"""An ignored GitHub repo event stays in the database for audit but can never feed a signal again."""
from datetime import UTC, datetime

from sqlalchemy import text

from app.services.correlate import correlate_company
from app.services.pipeline import process_event
from app.services.registry import register_source


async def repo_event(s, sid, cid, title, meta=None, n=1):
    meta = {"company_id": str(cid), **(meta or {})}
    return str((await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, published_at, raw_metadata)
        VALUES (:s, :n, 'repo', :t, now() - interval '2 days', CAST(:m AS jsonb)) RETURNING id"""),
        {"s": sid, "n": f"repo:{n}", "t": title, "m": __import__("json").dumps(meta)})).scalar_one())


async def test_profile_repos_and_hand_marked_events_are_ignored_and_never_signal(sm):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme', 'acme') RETURNING id"))).scalar_one()
        sid = await register_source(s, "GitHub Orgs", "api")
        profile = await repo_event(s, sid, cid, "acme/.github", n=1)
        manual = await repo_event(s, sid, cid, "acme/not-a-launch", {"ignored_reason": "marked by the owner"}, n=2)
        assert [await process_event(s, profile), await process_event(s, manual)] == ["ignored", "ignored"]
        await correlate_company(s, str(cid), datetime.now(UTC))
        assert (await s.execute(text("SELECT count(*) FROM company_signals WHERE signal_type = 'new_repo'"))).scalar_one() == 0
        assert (await s.execute(text("SELECT count(*) FROM source_events WHERE event_type = 'repo'"))).scalar_one() == 2  # raw events kept
        assert (await s.execute(text("SELECT count(*) FROM alert_deliveries"))).scalar_one() == 0
        genuine = await repo_event(s, sid, cid, "acme/new-tool", n=3)
        assert await process_event(s, genuine) == "none"  # a real repo still counts
        await correlate_company(s, str(cid), datetime.now(UTC))
        assert (await s.execute(text("SELECT data->>'repos' FROM company_signals WHERE signal_type = 'new_repo'"))).scalar_one() == "1"
        assert await process_event(s, profile) == "ignored"  # a retried job keeps the status


async def test_removing_a_signal_and_ignoring_its_event_survives_the_next_pass(sm):
    """The cleanup sequence used on the real database: delete the signal, mark the event, run the correlation pass again."""
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme', 'acme') RETURNING id"))).scalar_one()
        sid = await register_source(s, "GitHub Orgs", "api")
        eid = await repo_event(s, sid, cid, "acme/.github")
        await s.execute(text("UPDATE source_events SET processing_status = 'none' WHERE id = :e"), {"e": eid})  # how it was stored before the fix
        await correlate_company(s, str(cid), datetime.now(UTC))
        assert (await s.execute(text("SELECT count(*) FROM company_signals"))).scalar_one() == 1  # the incorrect signal
        await s.execute(text("DELETE FROM company_signals WHERE signal_type = 'new_repo'"))
        await s.execute(text("""UPDATE source_events SET processing_status = 'ignored',
            raw_metadata = raw_metadata || '{"ignored_reason": "organisation profile repository, not a launch"}' WHERE id = :e"""), {"e": eid})
        await correlate_company(s, str(cid), datetime.now(UTC))
        assert (await s.execute(text("SELECT count(*) FROM company_signals"))).scalar_one() == 0
