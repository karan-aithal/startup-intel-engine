"""Architecture revision: normalization, claim staging/merge, logical dedup, caps and promotion, persistence, outbox."""
from decimal import Decimal

import httpx
from sqlalchemy import text

from app.connectors.api import AtsAdapter
from app.connectors.runner import run_once
from app.services import ats, claims, outbox, registry
from app.services.normalize import parse_amounts, parse_stage, validate_field
from app.services.pipeline import format_alert, process_event


def test_amount_parser_handles_currencies_and_units():
    got = parse_amounts("$5M, ₹40 Cr, Rs 10 lakh, 5 million dollars, €2.5m, US$ 1.2 billion")
    assert [(int(v), c) for v, c in got] == [(5000000, "USD"), (400000000, "INR"), (1000000, "INR"), (2500000, "EUR"), (1200000000, "USD"), (5000000, "USD")]


def test_validation_supports_normalized_values_and_rejects_the_rest():
    src = "Zypto Labs Pvt Ltd raises Rs 40 crore in a Series A round"
    assert validate_field("company", "Zypto", "Zypto Labs Pvt Ltd raises", src) == ("Zypto", "supported")
    assert validate_field("company", "Zypto Labs Pvt Ltd", "Zypto Labs Private Limited raises", "Zypto Labs Private Limited raises")[1] == "normalized"
    assert validate_field("amount", "400000000", "Rs 40 crore", src, "INR")[0]["currency"] == "INR"
    assert validate_field("amount", "400000000", "Rs 40 crore", src, "USD") == (None, "rejected:currency_mismatch")
    assert validate_field("stage", "series a", "Series A round", src)[0] == "series a"
    assert validate_field("company", "Zypto", "text that is not in the article", src) == (None, "rejected:quote_not_in_source")
    assert parse_stage("pre-seed funding") == "pre-seed"


def test_merge_agree_conflict_and_union():
    rx = {"company": "Zypto", "stage": "seed", "amount_value": Decimal(5000000), "currency": "USD", "amount_text": "$5M",
          "investors": [("Peak XV", "lead")], "founders": ["Asha Rao"]}
    llm = {"company": "Zypto", "stage": "series a", "amount_value": "5000000", "currency": "USD", "investors": ["Peak XV", "Accel"], "founders": []}
    m = claims.merge(rx, llm)
    assert m["origins"] == {"company": "merged", "stage": "llm", "amount": "merged"} and m["alternatives"] == {"stage": "seed"}
    assert {k: v["origin"] for k, v in m["people"]["investors"].items()} == {"peak xv": "merged", "accel": "llm"}
    assert m["people"]["investors"]["peak xv"]["role"] == "lead" and list(m["people"]["founders"]) == ["asha rao"]


async def add_event(sm, title, source="News", content="", status="new"):
    async with sm() as s:
        sid = await registry.register_source(s, source, "feed")
        eid = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content, processing_status)
            VALUES (:s, :t, 'article', :title, :c, :st) RETURNING id"""), {"s": sid, "t": title, "title": title, "c": content, "st": status})).scalar_one()
        await s.commit()
    return str(eid)


async def run(sm, eid):
    async with sm() as s:
        label = await process_event(s, eid)
        await s.commit()
    return label


async def test_confirmed_duplicate_alert_is_suppressed_and_linked(sm):
    e1 = await add_event(sm, "Zypto raises $5 million in seed round", "TechCrunch")
    e2 = await add_event(sm, "Zypto secures $5M seed funding led by Accel", "Entrackr")
    assert await run(sm, e1) == await run(sm, e2) == "funding"
    await run(sm, e1)  # a retried job must not alert again
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM alert_deliveries"))).scalar_one() == 1
        rel = (await s.execute(text("SELECT event_id, related_event_id, kind, method FROM event_relationships"))).one()
        assert (str(rel.event_id), str(rel.related_event_id), rel.kind, rel.method) == (e2, e1, "same_event", "sync_rule")
        assert (await s.execute(text("SELECT count(*) FROM funding_round_claims"))).scalar_one() == 2  # both articles stay as claims


async def test_rumour_then_announcement_is_a_follow_up_not_a_duplicate(sm):
    e1 = await add_event(sm, "Zypto in talks to raise $5 million", "A")
    e2 = await add_event(sm, "Zypto raises $5 million in seed round", "B")
    assert await run(sm, e1) == "rumour" and await run(sm, e2) == "funding"
    async with sm() as s:
        assert (await s.execute(text("SELECT count(*) FROM alert_deliveries"))).scalar_one() == 2
        assert (await s.execute(text("SELECT kind FROM event_relationships"))).scalar_one() == "follow_up"


async def test_outbox_resend_after_crash_is_flagged(sm):
    async with sm() as s:
        await outbox.add_alert(s, "k1", {"label": "funding", "title": "T", "source": "S", "detected_at": "2026-11-16T00:00:00+00:00"}, topic="Global Funding")
        first = await outbox.claim_pending(s, lease_s=-1)  # worker "dies": lease already expired
        await s.commit()
    assert first[0]["possible_duplicate"] is False
    async with sm() as s:
        again = await outbox.claim_pending(s)
        await s.commit()
    assert again[0]["possible_duplicate"] is True
    assert "(resent)" in format_alert("Global Funding", again[0]["payload"], resent=True)
    assert "(resent)" not in format_alert("Global Funding", again[0]["payload"])


def ats_web(tokens):
    def h(req: httpx.Request):
        for t in tokens:
            if str(req.url) == f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs":
                return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Eng", "absolute_url": "https://x/1", "location": {"name": "Remote"}}]})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


async def mk_company(s, name, hot=False, india=False):
    return str((await s.execute(text("""INSERT INTO companies (name, canonical_name, is_india_linked, hot_until)
        VALUES (:n, :n, :i, CASE WHEN :h THEN now() + interval '30 days' END) RETURNING id"""), {"n": name, "i": india, "h": hot})).scalar_one())


async def test_board_cap_and_company_cap_are_independent_and_promotion_ranks(sm):
    c = ats_web(["a", "b", "c"])
    async with sm() as s:
        ids = {n: await mk_company(s, n, hot=(n == "b"), india=(n == "c")) for n in "abc"}
        for n in "abc":  # board cap 1: all three wait, none polled
            assert await ats.register_board(s, ids[n], "greenhouse", n, "t", 200, 0, c) == "candidate"
        assert await ats.promote_candidates(s, 200, 1, c) == 1
        await s.commit()
    async with sm() as s:
        rows = dict((await s.execute(text("SELECT board_token, status FROM ats_boards"))).all())
        assert rows == {"a": "candidate", "b": "active", "c": "candidate"}  # the hot company wins the single slot
        assert (await s.execute(text("SELECT promotion_reason FROM ats_boards WHERE board_token='b'"))).scalar_one() == "hot company"
        assert (await s.execute(text("SELECT enabled FROM source_registry WHERE name='greenhouse:b'"))).scalar_one()
        assert not (await s.execute(text("SELECT enabled FROM source_registry WHERE name='greenhouse:a'"))).scalar_one()
        # raising the board cap while the company cap is 1 (b already uses it): c is blocked by the company limit, not the board limit
        assert await ats.promote_candidates(s, 1, 10, c) == 0
        assert await ats.promote_candidates(s, 2, 10, c) == 1  # india-linked c next
        assert (await s.execute(text("SELECT status FROM ats_boards WHERE board_token='c'"))).scalar_one() == "active"


async def test_demotion_frees_the_slot(sm):
    c = ats_web(["a"])
    async with sm() as s:
        cid = await mk_company(s, "a")
        assert await ats.register_board(s, cid, "greenhouse", "a", "t", 200, 300, c) == "registered"
        await s.execute(text("UPDATE source_registry SET consecutive_failures = 3 WHERE name = 'greenhouse:a'"))
        assert await ats.demote_stale(s) == 1
        await s.commit()
    async with sm() as s:
        assert (await s.execute(text("SELECT status FROM ats_boards"))).scalar_one() == "candidate"
        assert not (await s.execute(text("SELECT watched FROM companies"))).scalar_one()
        assert await ats.active_boards(s) == 0


async def test_polls_persist_board_and_feed_status(sm):
    c = ats_web(["a"])
    async with sm() as s:
        cid = await mk_company(s, "a")
        await ats.register_board(s, cid, "greenhouse", "a", "careers page link", 200, 300, c, {"found_on": "https://a.com/careers"})
        await s.execute(text("""INSERT INTO company_sources (company_id, source_id, kind, url) SELECT :c, id, 'careers', 'https://a.com/careers'
            FROM source_registry WHERE name = 'greenhouse:a'"""), {"c": cid})
        await s.commit()
    assert (await run_once(sm, AtsAdapter("greenhouse", "a", c)))["status"] == "ok"
    async with sm() as s:
        b = (await s.execute(text("SELECT last_polled_at, last_success_at, next_poll_at, last_job_count, verification_status, evidence FROM ats_boards"))).one()
        assert b.last_polled_at and b.last_success_at and b.next_poll_at > b.last_polled_at
        assert (b.last_job_count, b.verification_status, b.evidence["found_on"]) == (1, "verified", "https://a.com/careers")
        cs = (await s.execute(text("SELECT verified_at, last_polled_at, next_poll_at FROM company_sources"))).one()
        assert cs.verified_at and cs.last_polled_at and cs.next_poll_at


async def test_candidate_per_event_is_idempotent(sm):
    from app.services.companies import add_candidate
    e = await add_event(sm, "Zypto raises $5 million")
    async with sm() as s:
        await add_candidate(s, "Zypto", e)
        await s.execute(text("UPDATE company_discovery_candidates SET status = 'accepted'"))
        await add_candidate(s, "Zypto", e)  # retried job after the first candidate was already accepted
        await s.commit()
        assert (await s.execute(text("SELECT count(*) FROM company_discovery_candidates"))).scalar_one() == 1


def test_validator_tolerates_model_formatting_but_not_wrong_numbers():
    src = "Cosmoserve raised $6 million in a seed round. Arka raises Rs 525 Cr"
    assert validate_field("amount", "{6000000}", "raised $6 million", src)[0]["value"] == 6000000
    assert validate_field("amount", "{52500000000}", "Rs 525 Cr", src) == (None, "rejected:amount_not_in_quote")  # 10x off: still rejected
    assert validate_field("stage", "null", "null", src) == (None, "absent")
