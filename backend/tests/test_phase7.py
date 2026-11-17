"""Phase 7: evidence weights, syndication, status lifecycle, contradiction, promotion, correlation, scores."""
import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.services import registry, scoring
from app.services.correlate import correlate_company
from app.services.verify import verify_company

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
_n = itertools.count()


def ev(type_, group="g", supports=True):
    return {"type": type_, "weight": scoring.weight(type_), "group": group, "supports": supports}


def test_assess_rules():
    a = scoring.assess([ev("community")], rumour_only=False)
    assert a["status"] == "UNVERIFIED" and "community_only" in a["flags"]
    o = scoring.assess([ev("press_wire")], rumour_only=False)
    assert o["status"] == "VERIFIED" and o["confidence"] == 90
    assert scoring.assess([ev("reputable_publication")], False)["status"] == "PARTIALLY_VERIFIED"
    # three copies in one syndication group are one voice
    copies = scoring.assess([ev("reputable_publication", "x")] * 3, False)
    assert copies["status"] == "PARTIALLY_VERIFIED" and copies["independent"] == 1 and "single_independent_source" in copies["flags"]
    two = scoring.assess([ev("reputable_publication", "x"), ev("reputable_publication", "y")], False)
    assert two["status"] == "VERIFIED" and two["confidence"] == 95
    r = scoring.assess([ev("press_wire", "x"), ev("reputable_publication", "y")], rumour_only=True)
    assert r["status"] == "UNVERIFIED" and r["confidence"] == 40


def test_scores_global_ignores_india_and_flags_subtract():
    a = scoring.funding_scores("seed", 90, False, NOW, 90, [], NOW)
    b = scoring.funding_scores("seed", 90, True, NOW, 90, [], NOW)
    assert a[1] == b[1] and b[0] - a[0] == 20  # the India term moves only the India score
    assert scoring.funding_scores("seed", 90, True, NOW, 90, ["community_only"], NOW)[0] == b[0] - 20
    assert scoring.freshness(NOW - timedelta(days=7), NOW) == 50


async def company(sm, name="Zypto", india=False):
    async with sm() as s:
        cid = (await s.execute(text("INSERT INTO companies (name, canonical_name, is_india_linked) VALUES (:n, :n, :i) RETURNING id"),
                               {"n": name, "i": india})).scalar_one()
        await s.commit()
    return str(cid)


async def claim(sm, cid, source, amount=5_000_000, h=None, rumour=False, days_ago=1, stage="seed", status="unverified"):
    async with sm() as s:
        sid = await registry.register_source(s, source, "feed")
        t = NOW - timedelta(days=days_ago)
        n = next(_n)
        eid = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content_hash, published_at, detected_at)
            VALUES (:s, :n, 'article', :t, :h, :p, :p) RETURNING id"""), {"s": sid, "n": f"e{n}", "t": f"Zypto raises {n}", "h": h, "p": t})).scalar_one()
        await s.execute(text("""INSERT INTO funding_round_claims (source_event_id, company_id, stage, amount_value, currency, is_rumour, origin, status)
            VALUES (:e, :c, :st, :a, 'USD', :r, 'regex', :s)"""), {"e": eid, "c": cid, "st": stage, "a": amount, "r": rumour, "s": status})
        await s.commit()
    return str(eid)


async def verify(sm, cid, now=NOW):
    async with sm() as s:
        out = await verify_company(s, cid, now)
        await s.commit()
    return out


async def rounds(sm):
    async with sm() as s:
        return (await s.execute(text("SELECT verification_status, confidence, risk_flags, amount_original FROM funding_rounds ORDER BY amount_original"))).all()


async def test_community_claim_stays_unverified_and_official_evidence_raises_confidence(sm):
    cid = await company(sm)
    await claim(sm, cid, "Hacker News RSS")  # unlisted source = community (25)
    assert await verify(sm, cid) == {"UNVERIFIED": 1}
    assert await rounds(sm) == []  # no authoritative row for an unverified claim
    async with sm() as s:
        assert (await s.execute(text("SELECT status FROM funding_round_claims"))).scalar_one() == "unverified"
    await claim(sm, cid, "PR Newswire VC")  # company press release (90)
    assert await verify(sm, cid) == {"VERIFIED": 1}
    [r] = await rounds(sm)
    assert r.verification_status == "VERIFIED" and r.confidence == 95  # 90 plus one independent corroborating voice
    async with sm() as s:
        assert {x for x in (await s.execute(text("SELECT status FROM funding_round_claims"))).scalars()} == {"verified"}
        assert (await s.execute(text("SELECT count(*) FROM evidence WHERE funding_round_id IS NOT NULL"))).scalar_one() == 2


async def test_syndicated_copies_do_not_corroborate_each_other(sm):
    cid = await company(sm)
    await claim(sm, cid, "Entrackr", h="same")
    await claim(sm, cid, "Inc42", h="same")  # identical content from another outlet: one voice
    await claim(sm, cid, "Entrackr", h="other")  # second article from the same outlet: still one voice
    await verify(sm, cid)
    [r] = await rounds(sm)
    assert r.verification_status == "PARTIALLY_VERIFIED" and "single_independent_source" in r.risk_flags
    async with sm() as s:
        assert (await s.execute(text("SELECT count(DISTINCT syndication_group) FROM evidence"))).scalar_one() == 1
    await claim(sm, cid, "Sifted", h="third")  # genuinely separate outlet
    await verify(sm, cid)
    [r] = await rounds(sm)
    assert r.verification_status == "VERIFIED" and r.confidence == 95 and "single_independent_source" not in r.risk_flags


async def test_rumour_only_never_verifies_and_rerun_is_idempotent(sm):
    cid = await company(sm)
    await claim(sm, cid, "PR Newswire VC", rumour=True)
    await verify(sm, cid)
    assert await rounds(sm) == []
    await claim(sm, cid, "Entrackr")
    for _ in range(3):
        await verify(sm, cid)
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM funding_rounds"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM evidence"))).scalar_one() == 2
        assert (await s.execute(text("SELECT count(*) FROM funding_round_claims WHERE funding_round_id IS NOT NULL"))).scalar_one() == 2


async def test_conflicting_amounts_contradict_the_weaker_round(sm):
    cid = await company(sm)
    await claim(sm, cid, "PR Newswire VC", 5_000_000)
    await claim(sm, cid, "Sifted", 5_000_000)
    await claim(sm, cid, "Inc42", 5_000_000)  # three independent voices
    await claim(sm, cid, "Entrackr", 9_000_000)  # a single outlet disagrees
    await verify(sm, cid)
    five, nine = await rounds(sm)
    assert nine.verification_status == "CONTRADICTED" and "conflicting_amount" in nine.risk_flags
    assert five.verification_status == "VERIFIED" and "conflicting_amount" in five.risk_flags
    async with sm() as s:  # the winner keeps the loser's article as contradicting evidence
        assert (await s.execute(text("SELECT count(*) FROM evidence WHERE supports = false"))).scalar_one() == 1


async def test_no_clear_winner_caps_both_at_partially_verified(sm):
    cid = await company(sm)
    await claim(sm, cid, "Entrackr", 5_000_000)
    await claim(sm, cid, "Inc42", 9_000_000)
    await verify(sm, cid)
    assert {r.verification_status for r in await rounds(sm)} == {"PARTIALLY_VERIFIED"}


async def test_rejected_claim_unsupports_its_round_and_old_partial_rounds_expire(sm):
    cid = await company(sm)
    eid = await claim(sm, cid, "Entrackr", days_ago=90)
    await verify(sm, cid)
    assert (await rounds(sm))[0].verification_status == "EXPIRED"  # 90 days with nothing newer
    async with sm() as s:
        await s.execute(text("UPDATE funding_round_claims SET status = 'rejected' WHERE source_event_id = :e"), {"e": eid})
        await s.commit()
    await verify(sm, cid)
    r = (await rounds(sm))[0]
    assert r.verification_status == "UNVERIFIED" and r.confidence == 0 and r.risk_flags == ["all_claims_rejected"]


async def test_verified_round_promotes_investors_and_founders_with_evidence(sm):
    cid = await company(sm)
    e = await claim(sm, cid, "PR Newswire VC")
    async with sm() as s:
        await s.execute(text("""INSERT INTO company_investor_claims (source_event_id, company_id, name, canonical_name, origin, role)
            VALUES (:e, :c, 'Accel', 'accel', 'regex', 'lead')"""), {"e": e, "c": cid})
        await s.execute(text("INSERT INTO company_founder_claims (source_event_id, company_id, name, canonical_name, origin) VALUES (:e, :c, 'Asha Rao', 'asha rao', 'regex')"),
                        {"e": e, "c": cid})
        await s.commit()
    await verify(sm, cid)
    async with sm() as s:
        row = (await s.execute(text("""SELECT i.name, ci.role, ci.evidence_id IS NOT NULL AS proof FROM company_investors ci
            JOIN investors i ON i.id = ci.investor_id"""))).one()
        assert (row.name, row.role, row.proof) == ("Accel", "lead", True)
        assert (await s.execute(text("SELECT name FROM founders"))).scalar_one() == "Asha Rao"


async def test_funding_followed_by_hiring_is_detected_and_scores_are_stored(sm):
    cid = await company(sm, india=True)
    await claim(sm, cid, "PR Newswire VC", days_ago=20)
    await verify(sm, cid)
    async with sm() as s:
        await s.execute(text("INSERT INTO ats_boards (company_id, provider, board_token) VALUES (:c, 'greenhouse', 'zypto')"), {"c": cid})
        sid = await registry.register_source(s, "greenhouse:zypto", "api")
        for i, days in enumerate([40, 10, 9, 8, 7, 6]):  # one job predates the round; five follow it
            await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, published_at, detected_at)
                VALUES (:s, :i, 'job', 'Engineer', :p, :p)"""), {"s": sid, "i": f"j{i}", "p": NOW - timedelta(days=days)})
        await s.commit()
    for _ in range(2):  # idempotent
        async with sm() as s:
            await correlate_company(s, cid, NOW)
            await s.commit()
    async with sm() as s:
        sig = {r.signal_type: r.data for r in (await s.execute(text("SELECT signal_type, data FROM company_signals"))).all()}
        assert sig["funding_then_hiring"]["jobs"] == 5 and "no causal claim" in sig["funding_then_hiring"]["note"]
        assert sig["hiring_surge"]["jobs"] == 5
        assert (await s.execute(text("SELECT count(*) FROM company_signals"))).scalar_one() == 2
        f = (await s.execute(text("SELECT score_india, score_global FROM funding_rounds"))).one()
        assert f.score_india > f.score_global  # India-linked company: the India term adds to the India score only
        e = (await s.execute(text("SELECT emerging_india, emerging_global FROM companies"))).one()
        assert e.emerging_india is not None and e.emerging_global is not None


async def test_different_stages_in_the_same_window_are_both_kept_and_flagged(sm):
    cid = await company(sm)
    await claim(sm, cid, "Entrackr", 2_000_000, stage="seed", days_ago=5)  # seed extension
    await claim(sm, cid, "Inc42", 9_000_000, stage="series a", days_ago=3)  # a separate, legitimate round
    await verify(sm, cid)
    two, nine = await rounds(sm)
    assert two.verification_status == nine.verification_status == "PARTIALLY_VERIFIED"  # nobody is CONTRADICTED
    assert two.confidence == nine.confidence == 90  # and nobody pays the contradiction penalty
    for r in (two, nine):
        assert "possible_conflicting_amount" in r.risk_flags and "conflicting_amount" not in r.risk_flags
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM evidence WHERE supports = false"))).scalar_one() == 0


async def test_unknown_stage_is_also_only_flagged(sm):
    cid = await company(sm)
    await claim(sm, cid, "Entrackr", 2_000_000, stage=None)
    await claim(sm, cid, "Inc42", 9_000_000, stage="series a")
    await verify(sm, cid)
    assert {r.verification_status for r in await rounds(sm)} == {"PARTIALLY_VERIFIED"}


async def test_promotions_are_removed_when_the_round_loses_verification_but_manual_rows_stay(sm):
    cid = await company(sm)
    wire = await claim(sm, cid, "PR Newswire VC", days_ago=2)
    await claim(sm, cid, "Entrackr", days_ago=1)
    async with sm() as s:
        await s.execute(text("""INSERT INTO company_investor_claims (source_event_id, company_id, name, canonical_name, origin, role)
            VALUES (:e, :c, 'Accel', 'accel', 'regex', 'lead')"""), {"e": wire, "c": cid})
        await s.execute(text("INSERT INTO company_founder_claims (source_event_id, company_id, name, canonical_name, origin) VALUES (:e, :c, 'Asha Rao', 'asha rao', 'regex')"),
                        {"e": wire, "c": cid})
        manual = (await s.execute(text("INSERT INTO investors (name, canonical_name) VALUES ('Sequoia', 'sequoia') RETURNING id"))).scalar_one()
        await s.execute(text("INSERT INTO company_investors (company_id, investor_id, role) VALUES (:c, :i, 'existing')"), {"c": cid, "i": manual})
        await s.execute(text("INSERT INTO founders (company_id, name) VALUES (:c, 'Manual Founder')"), {"c": cid})
        await s.commit()
    await verify(sm, cid)
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM company_investors"))).scalar_one() == 2
        assert (await s.execute(text("SELECT count(*) FROM founders"))).scalar_one() == 2
        await s.execute(text("UPDATE funding_round_claims SET status = 'rejected' WHERE source_event_id = :e"), {"e": wire})  # the wire claim is withdrawn
        await s.commit()
    await verify(sm, cid)
    assert (await rounds(sm))[0].verification_status == "PARTIALLY_VERIFIED"  # one outlet is left
    async with sm() as s:
        assert [r[0] for r in (await s.execute(text("SELECT i.name FROM company_investors ci JOIN investors i ON i.id = ci.investor_id"))).all()] == ["Sequoia"]
        assert (await s.execute(text("SELECT name FROM founders"))).scalar_one() == "Manual Founder"
        assert {x for x in (await s.execute(text("SELECT status FROM company_investor_claims"))).scalars()} == {"unverified"}
    await claim(sm, cid, "Sifted", days_ago=1)  # corroboration returns: the round is VERIFIED again and the promotion is restored
    await verify(sm, cid)
    assert (await rounds(sm))[0].verification_status == "VERIFIED"


async def test_signal_data_is_refreshed_not_frozen(sm):
    cid = await company(sm)
    async with sm() as s:
        await s.execute(text("INSERT INTO ats_boards (company_id, provider, board_token) VALUES (:c, 'greenhouse', 'zypto')"), {"c": cid})
        sid = await registry.register_source(s, "greenhouse:zypto", "api")
        await s.commit()

    async def add_jobs(n, start):
        async with sm() as s:
            for i in range(start, start + n):
                await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, published_at, detected_at)
                    VALUES (:s, :i, 'job', 'Engineer', :p, :p)"""), {"s": sid, "i": f"s{i}", "p": NOW - timedelta(days=2)})
            await s.commit()

    async def run(now):
        async with sm() as s:
            await correlate_company(s, cid, now)
            await s.commit()

    await add_jobs(5, 0)
    await run(NOW)
    await add_jobs(10, 5)
    await run(NOW + timedelta(hours=3))
    async with sm() as s:
        rows = (await s.execute(text("SELECT data, occurred_at FROM company_signals WHERE signal_type = 'hiring_surge'"))).all()
    assert len(rows) == 1 and rows[0].data["jobs"] == 15  # same identity, fresh count
    assert rows[0].data["refreshed_at"].startswith("2026-11-16T15") and rows[0].occurred_at == NOW  # first observation time is kept
