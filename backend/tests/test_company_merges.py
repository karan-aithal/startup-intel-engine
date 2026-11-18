"""Phase 13: approved identity changes (merge, rename, not-a-company) are audited, reversible, never delete, and collapse duplicate rounds."""
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import company_merges as cm
from app.services import registry
from app.services.companies import canonical, match_company
from app.services.verify import verify_company

MAP = Path(__file__).resolve().parents[2] / "docs" / "quality" / "identity_merge_map.csv"
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
_n = itertools.count()


def test_parse_the_owners_real_approve_cells():
    p = cm.parse_decision
    assert p("yes: merge into Dreamteam. When implemented, dedupe the duplicate $53M round") == ("merge", "Dreamteam")
    assert p("yes: rename to Numa (changed from review_name). Keep its Series C round") == ("rename", "Numa")
    assert p("yes: rename to Commit Biologics. Keep its round") == ("rename", "Commit Biologics")
    assert p("yes: not_a_company, a university (endowment donations, not a funding round)") == ("exclude", "a university")
    assert p("no: keep as a company. Keep its expired round untouched") is None and p("") is None and p("   ") is None
    with pytest.raises(ValueError):
        p("yes: do something else")
    with pytest.raises(ValueError):
        p("yes: merge into")


def test_the_approved_map_reads_as_fourteen_decisions():
    d = cm.read_decisions(MAP)
    kinds = sorted(x["action"] for x in d)
    assert (kinds.count("merge"), kinds.count("rename"), kinds.count("exclude")) == (3, 3, 8) and len(d) == 14
    assert "Nexedge Capital" not in {x["company"] for x in d}  # approve = no: left alone


async def company(s, name):
    return str((await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :c) RETURNING id"), {"n": name, "c": canonical(name)})).scalar_one())


async def claim(s, cid, source, amount=5_000_000, stage="seed"):
    sid = await registry.register_source(s, source, "feed")
    n = next(_n)
    eid = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content_hash, published_at, detected_at)
        VALUES (:s, :n, 'article', :t, :h, :p, :p) RETURNING id"""), {"s": sid, "n": f"e{n}", "t": f"Acme raises {n}", "h": None, "p": NOW - timedelta(days=1)})).scalar_one()
    await s.execute(text("""INSERT INTO funding_round_claims (source_event_id, company_id, stage, amount_value, currency, origin, status)
        VALUES (:e, :c, :st, :a, 'USD', 'regex', 'unverified')"""), {"e": eid, "c": cid, "st": stage, "a": amount})
    return str(eid)


async def one(s, sql, **p):
    return (await s.execute(text(sql), p)).scalar_one()


def decision(action, src, arg, name="X"):
    return {"row": 2, "company_id": src, "company": name, "action": action, "arg": arg, "decision": f"yes: {action}"}


async def test_merge_moves_rows_collapses_the_duplicate_round_hides_the_source_and_undo_restores(sm):
    async with sm() as s:
        tgt, src = await company(s, "Dreamteam"), await company(s, "AI-native CRM Dreamteam")
        await claim(s, tgt, "Inc42")
        await claim(s, src, "Entrackr")  # the same $5M seed reported under the long name
        await verify_company(s, tgt)
        await verify_company(s, src)
        assert await one(s, "SELECT count(*) FROM funding_rounds") == 2  # two companies, one real round
        await s.commit()
    async with sm() as s:
        d = decision("merge", src, "Dreamteam", "AI-native CRM Dreamteam")
        assert await cm.resolve(s, [d]) == [] and d["target_id"] == tgt
        rep = await cm.apply_one(s, d)
        await s.commit()
    assert rep["moved"]["funding_round_claims"] == 1 and rep["moved"]["funding_rounds"] == 1 and len(rep["rounds_after"]) == 1
    async with sm() as s:
        assert await one(s, "SELECT count(*) FROM funding_rounds WHERE company_id = CAST(:t AS uuid)", t=tgt) == 1  # collapsed, not lost
        assert await one(s, "SELECT count(*) FROM funding_round_claims WHERE company_id = CAST(:t AS uuid)", t=tgt) == 2  # every claim kept
        assert await one(s, "SELECT excluded_reason FROM companies WHERE id = CAST(:i AS uuid)", i=src) == "merged into Dreamteam"
        assert await one(s, "SELECT count(*) FROM companies") == 2  # nothing deleted
        assert await match_company(s, "AI-native CRM Dreamteam") == (tgt, 100)  # a later mention resolves to the survivor, not the hidden row
        mid = await one(s, "SELECT id::text FROM company_merges")
        out = await cm.undo(s, mid)
        await s.commit()
    assert out["restored"]["funding_round_claims"] == "1 of 1"
    async with sm() as s:
        assert await one(s, "SELECT count(*) FROM funding_round_claims WHERE company_id = CAST(:t AS uuid)", t=src) == 1
        assert await one(s, "SELECT excluded_reason IS NULL FROM companies WHERE id = CAST(:i AS uuid)", i=src)
        assert await one(s, "SELECT count(*) FROM company_aliases") == 0
        assert await one(s, "SELECT undone_at IS NOT NULL FROM company_merges")
        assert await one(s, "SELECT count(*) FROM funding_rounds") == 2  # each company's round is rebuilt from its own claim
        with pytest.raises(ValueError, match="already undone"):
            await cm.undo(s, mid)


async def test_a_unique_key_clash_leaves_that_row_behind_and_the_merge_still_completes(sm):
    async with sm() as s:
        tgt, src = await company(s, "Genesis AI"), await company(s, "French robot maker Genesis")
        for c in (tgt, src):
            await s.execute(text("INSERT INTO founders (company_id, name) VALUES (CAST(:c AS uuid), 'Ada Lovelace')"), {"c": c})
        await s.execute(text("INSERT INTO founders (company_id, name) VALUES (CAST(:c AS uuid), 'Grace Hopper')"), {"c": src})
        d = decision("merge", src, "Genesis AI")
        d["target_id"] = tgt
        rep = await cm.apply_one(s, d)
        await s.commit()
    assert rep["moved"]["founders"] == 1 and rep["left_behind"]["founders"] == 1
    async with sm() as s:
        assert await one(s, "SELECT count(*) FROM founders WHERE company_id = CAST(:t AS uuid)", t=tgt) == 2
        assert await one(s, "SELECT count(*) FROM founders WHERE company_id = CAST(:t AS uuid)", t=src) == 1


async def test_rename_keeps_the_old_name_as_an_alias_and_stops_on_a_collision_and_undo_restores(sm):
    async with sm() as s:
        cid, other = await company(s, "Profitable French InsurTech Previsia"), await company(s, "Numa")
        assert await cm.resolve(s, [decision("rename", cid, "Previsia", "Profitable French InsurTech Previsia")]) == []
        errs = await cm.resolve(s, [decision("rename", cid, "Numa", "Profitable French InsurTech Previsia")])
        assert len(errs) == 1 and "collides" in errs[0] and other
        await cm.apply_one(s, decision("rename", cid, "Previsia", "Profitable French InsurTech Previsia"))
        await s.commit()
    async with sm() as s:
        assert await one(s, "SELECT name FROM companies WHERE id = CAST(:i AS uuid)", i=cid) == "Previsia"
        assert await match_company(s, "Profitable French InsurTech Previsia") == (cid, 100)  # old name still resolves
        assert await match_company(s, "Previsia") == (cid, 100)
        await cm.undo(s, await one(s, "SELECT id::text FROM company_merges"))
        await s.commit()
    async with sm() as s:
        assert await one(s, "SELECT name FROM companies WHERE id = CAST(:i AS uuid)", i=cid) == "Profitable French InsurTech Previsia"
        assert await one(s, "SELECT count(*) FROM company_aliases") == 0


async def test_not_a_company_hides_but_keeps_rounds_and_evidence_and_undo_unhides(sm):
    async with sm() as s:
        cid = await company(s, "Canonicus Capital")
        await claim(s, cid, "Entrackr")
        await verify_company(s, cid)
        await s.commit()
    async with sm() as s:
        d = decision("exclude", cid, "an investment fund", "Canonicus Capital")
        assert await cm.resolve(s, [d]) == []
        await cm.apply_one(s, d)
        await s.commit()
    async with sm() as s:
        assert await one(s, "SELECT excluded_reason FROM companies") == "not a company: an investment fund"
        assert await one(s, "SELECT count(*) FROM funding_rounds") == 1  # kept
        assert await match_company(s, "Canonicus Capital") == (cid, 100)  # still resolves to the hidden row, so no fresh duplicate appears
        assert await cm.resolve(s, [d]) == [] and d.get("skip") == "already applied"  # applying twice is a no-op
        await cm.undo(s, await one(s, "SELECT id::text FROM company_merges"))
        await s.commit()
    async with sm() as s:
        assert await one(s, "SELECT excluded_reason IS NULL FROM companies")


async def test_stale_or_unmatched_maps_stop_before_anything_is_written_and_undo_refuses_out_of_order(sm):
    async with sm() as s:
        cid = await company(s, "Real Name")
        stale = await cm.resolve(s, [decision("exclude", cid, "", "Old Name"), decision("merge", cid, "Nobody Inc", "Real Name")])
        assert any("now 'Real Name'" in e for e in stale)
        assert await cm.resolve(s, [decision("merge", cid, "Nobody Inc", "Real Name")]) != []
        a = decision("exclude", cid, "x", "Real Name")
        await cm.apply_one(s, a)
        await s.execute(text("UPDATE company_merges SET created_at = created_at - interval '1 hour'"))
        await cm.apply_one(s, {**a, "action": "rename", "arg": "Newer Name"})  # a later change on the same company
        first = await one(s, "SELECT id::text FROM company_merges WHERE action = 'exclude'")
        with pytest.raises(ValueError, match="later change"):
            await cm.undo(s, first)


async def test_two_rounds_becoming_one_can_take_the_key_the_duplicate_still_holds(sm):
    """Regression (found while building merges): the surviving round is the lowest id, the duplicate holds the identity key."""
    async with sm() as s:
        cid = await company(s, "Dreamteam")
        a, b = await claim(s, cid, "Inc42"), await claim(s, cid, "Entrackr")
        low, high = "00000000-0000-0000-0000-000000000001", "ffffffff-ffff-ffff-ffff-ffffffffffff"
        for rid, key in ((low, None), (high, f"{cid}|USD|5000000.00|2026-11")):
            await s.execute(text("INSERT INTO funding_rounds (id, company_id, identity_key) VALUES (CAST(:r AS uuid), CAST(:c AS uuid), :k)"), {"r": rid, "c": cid, "k": key})
        for ev, rid in ((a, low), (b, high)):
            await s.execute(text("UPDATE funding_round_claims SET funding_round_id = CAST(:r AS uuid) WHERE source_event_id = CAST(:e AS uuid)"), {"r": rid, "e": ev})
        await verify_company(s, cid)
        assert await one(s, "SELECT count(*) FROM funding_rounds") == 1 and await one(s, "SELECT id::text FROM funding_rounds") == low
        assert await one(s, "SELECT identity_key FROM funding_rounds") == f"{cid}|USD|5000000.00|2026-11"


async def test_an_excluded_company_leaves_dashboard_lists_and_rankings_but_its_data_stays(sm, monkeypatch):
    from app.api import intel
    from app.services import quality

    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    async with sm() as s:
        ids = {n: await company(s, n) for n in ("Real Startup", "Some Fund")}
        for cid in ids.values():
            await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, announced_at, verification_status, confidence, score_global, score_india)
                VALUES (CAST(:c AS uuid), 'seed', :t, 'VERIFIED', 90, 70, 70)"""), {"c": cid, "t": NOW})
        await s.commit()
    names = lambda r: {i["company"] for i in r["items"]}
    assert names(await intel.funding("all", "", "", "recent", 50, 0)) == {"Real Startup", "Some Fund"}
    async with sm() as s:
        await cm.apply_one(s, decision("exclude", ids["Some Fund"], "an investment fund", "Some Fund"))
        await s.commit()
    assert names(await intel.funding("all", "", "", "recent", 50, 0)) == {"Real Startup"}
    o = await intel.overview("all")
    assert {r["name"] for r in o["top_funding"]} == {"Real Startup"} and o["stats"]["verified_rounds"] == 1
    assert {i["name"] for i in (await intel.companies("", "all", "name", 50, 0))["items"]} == {"Real Startup"}
    async with sm() as s:
        assert {r["company"] for r in (await quality.ranking(s))["global"]} == {"Real Startup"}
        assert await one(s, "SELECT count(*) FROM funding_rounds") == 2  # hidden, not deleted
