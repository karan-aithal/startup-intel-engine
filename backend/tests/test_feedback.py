"""Feedback capture (route A): resolution, ratings, undo, the daily draw, honest statistics, and no side effects on anything else."""
import json
import random
import uuid

import pytest
from sqlalchemy import text

from app.services import feedback, registry

TOPICS = ["Global Funding", "India Funding", "Global Hiring", "India Hiring"]
COUNTER = iter(range(10_000))


async def mk_alert(s, topic="India Funding", source="Entrackr", alert_id=None, events=1, company="Acme"):
    """An alert and the event(s) it was sent for. events=2 makes the alert ambiguous (an identity problem)."""
    n = next(COUNTER)
    sid = await registry.register_source(s, source, "feed")
    for i in range(events):
        await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content_hash, source_url, detected_at)
            VALUES (:s, :k, 'article', :t, :h, :u, now())"""), {"s": sid, "k": f"{n}-{i}", "t": f"{company} raises #{n}", "h": f"hash{n}", "u": f"https://e.com/{n}"})
    aid = alert_id or str(uuid.uuid4())
    payload = json.dumps({"source": source, "url": f"https://e.com/{n}", "company": company, "title": f"{company} raises #{n}", "label": "funding"})
    await s.execute(text("INSERT INTO alert_deliveries (id, topic, payload, dedup_key) VALUES (:i, :t, CAST(:p AS jsonb), :k)"),
                    {"i": aid, "t": topic, "p": payload, "k": f"event:hash{n}"})
    return aid[:8], aid


async def feedback_rows(s):
    return (await s.execute(text("SELECT action, note FROM feedback ORDER BY created_at, id"))).all()


# ---- references and identity ----

async def test_reference_must_name_exactly_one_alert_and_one_event(sm):
    async with sm() as s:
        await mk_alert(s, alert_id="aaaaaaaa-0000-4000-8000-000000000001")
        await mk_alert(s, alert_id="aaaaaaaa-0000-4000-8000-000000000002")
        ops, _ = await mk_alert(s, topic="Ops")
        amb, _ = await mk_alert(s, events=2)
        for bad, why in (("xyz", "hex"), ("aaaa", "hex"), ("aaaaaaaa", "matches 2"), ("12345678", "No alert"), (ops, "No alert"), (amb, "identity problem")):
            with pytest.raises(feedback.FeedbackError, match=why):
                await feedback.add(s, "useful", bad)
        assert await feedback_rows(s) == []  # a refusal writes nothing
        await feedback.add(s, "useful", "aaaaaaaa-0000-4000-8000-000000000001")  # the full id (dashes allowed) disambiguates
        assert len(await feedback_rows(s)) == 1


async def test_row_shape_links_and_json_note(sm):
    async with sm() as s:
        ref, aid = await mk_alert(s)
        ev = (await s.execute(text("SELECT id FROM source_events"))).scalar_one()
        co = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES ('Acme', 'acme') RETURNING id"))).scalar_one()
        rd = (await s.execute(text("INSERT INTO funding_rounds (company_id, verification_status) VALUES (:c, 'VERIFIED') RETURNING id"), {"c": co})).scalar_one()
        await s.execute(text("INSERT INTO funding_round_claims (source_event_id, company_id, origin) VALUES (:e, :c, 'regex')"), {"e": ev, "c": co})
        await s.execute(text("INSERT INTO evidence (company_id, funding_round_id, source_event_id, evidence_type, weight) VALUES (:c, :r, :e, 'reputable_publication', 90)"), {"c": co, "r": rd, "e": ev})
        await feedback.add(s, "incorrect", ref, why="wrong_amount", note="amount is in rupees", drawn=True)
        row = (await s.execute(text("SELECT action, source_event_id, company_id, funding_round_id, note FROM feedback"))).one()
    assert (row.action, row.source_event_id, row.company_id, row.funding_round_id) == ("incorrect", ev, co, rd)
    assert json.loads(row.note) == {"alert": aid, "topic": "India Funding", "source": "Entrackr", "company": "Acme", "via": "drawn", "why": "wrong_amount", "text": "amount is in rupees"}


# ---- the taxonomy and its rules ----

def test_taxonomy_matches_the_design_and_the_table_constraint():
    assert feedback.ACTIONS == ("useful", "irrelevant", "duplicate", "incorrect", "watch", "mute", "save", "applied")
    assert feedback.WHY == ("wrong_label", "wrong_company", "wrong_amount", "wrong_stage", "not_startup_funding", "not_india", "other")


async def test_validation(sm):
    async with sm() as s:
        a, _ = await mk_alert(s)
        b, _ = await mk_alert(s)
        for args, kw, why in ((("like", a), {}, "Unknown action"), (("incorrect", a), {}, "needs --why"), (("incorrect", a), {"why": "bad"}, "needs --why"),
                              (("useful", a), {"why": "other"}, "only goes with"), (("useful", a), {"of": b}, "only goes with"), (("duplicate", a), {}, "needs --of"),
                              (("duplicate", a), {"of": a}, "itself"), (("duplicate", a), {"of": "ffffffff"}, "No alert"), (("useful", a), {"note": "x" * 501}, "500"),
                              (("useful", a), {"note": "line\nbreak"}, "one line")):
            with pytest.raises(feedback.FeedbackError, match=why):
                await feedback.add(s, *args, **kw)
        assert await feedback_rows(s) == []
        await feedback.add(s, "duplicate", b, of=a)  # the valid form
        assert json.loads((await feedback_rows(s))[0].note)["of"].startswith(a)


async def test_one_rating_per_alert_revisions_replace_and_repeats_are_refused(sm):
    async with sm() as s:
        a, _ = await mk_alert(s)
        await feedback.add(s, "useful", a)
        with pytest.raises(feedback.FeedbackError, match="Already recorded"):
            await feedback.add(s, "useful", a)
        msg = await feedback.add(s, "irrelevant", a)  # a revision: replaces, not a second rating
        assert "replaced your earlier 'useful'" in msg
        await feedback.add(s, "incorrect", a, why="not_india")
        with pytest.raises(feedback.FeedbackError, match="Already recorded"):
            await feedback.add(s, "incorrect", a, why="not_india")
        await feedback.add(s, "incorrect", a, why="wrong_label")  # a different reason is a revision
        await feedback.add(s, "save", a)  # marks are independent of the rating
        with pytest.raises(feedback.FeedbackError, match="Already recorded"):
            await feedback.add(s, "save", a)
        assert sorted(r.action for r in await feedback_rows(s)) == ["incorrect", "save"]


async def test_undo_removes_only_the_chosen_feedback_row(sm):
    async with sm() as s:
        a, _ = await mk_alert(s)
        other, _ = await mk_alert(s)
        for act in ("useful", "save", "watch"):
            await feedback.add(s, act, a)
        await feedback.add(s, "irrelevant", other)
        before = [(await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() for t in ("alert_deliveries", "source_events")]
        assert "removed useful" in await feedback.undo(s, a)  # default: the rating
        assert sorted(r.action for r in await feedback_rows(s)) == ["irrelevant", "save", "watch"]
        assert "removed watch" in await feedback.undo(s, a, "watch")
        with pytest.raises(feedback.FeedbackError, match="No rating to remove.*It has: save.*--action"):
            await feedback.undo(s, a)
        with pytest.raises(feedback.FeedbackError, match="No mute"):
            await feedback.undo(s, a, "mute")
        assert sorted(r.action for r in await feedback_rows(s)) == ["irrelevant", "save"]  # the other alert's rating is untouched
        assert [(await s.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() for t in ("alert_deliveries", "source_events")] == before


# ---- guardrails: feedback touches nothing but its own table ----

async def test_feedback_changes_nothing_else(sm):
    async with sm() as s:
        a, _ = await mk_alert(s)
        b, _ = await mk_alert(s)
        co = (await s.execute(text("INSERT INTO companies (name, canonical_name, watched, muted) VALUES ('Acme', 'acme', false, false) RETURNING id"))).scalar_one()
        ev = (await s.execute(text("SELECT id FROM source_events LIMIT 1"))).scalar_one()
        await s.execute(text("INSERT INTO funding_rounds (company_id, verification_status, confidence, score_global) VALUES (:c, 'PARTIALLY_VERIFIED', 70, 60)"), {"c": co})
        await s.execute(text("INSERT INTO funding_round_claims (source_event_id, company_id, origin) VALUES (:e, :c, 'regex')"), {"e": ev, "c": co})
        snap = """SELECT md5(string_agg(x::text, ',' ORDER BY x::text)) FROM (
            SELECT row(a.*) AS x FROM alert_deliveries a UNION ALL SELECT row(e.*) FROM source_events e UNION ALL SELECT row(f.*) FROM funding_rounds f
            UNION ALL SELECT row(c.*) FROM companies c UNION ALL SELECT row(k.*) FROM funding_round_claims k UNION ALL SELECT row(v.*) FROM evidence v) t"""
        before = (await s.execute(text(snap))).scalar_one()
        for act in ("useful", "irrelevant", "watch", "mute", "save", "applied"):
            await feedback.add(s, act, a)
        await feedback.add(s, "incorrect", b, why="other")
        await feedback.add(s, "duplicate", a, of=b)
        await feedback.undo(s, a, "mute")
        assert (await s.execute(text(snap))).scalar_one() == before  # alerts, events, rounds (status, confidence, scores), companies (watched, muted), claims, evidence
        assert (await s.execute(text("SELECT watched, muted FROM companies"))).one() == (False, False)


# ---- the daily draw ----

async def test_draw_spreads_across_topics_and_stops_at_the_budget(sm):
    async with sm() as s:
        for t in TOPICS:
            for _ in range(8):
                await mk_alert(s, topic=t)
        rng, seen = random.Random(3), []
        for _ in range(4):  # the first four draws visit all four topics
            d = await feedback.draw(s, "UTC", rng=rng)
            seen.append(d["topic"])
            await feedback.add(s, "useful", d["ref"], drawn=True)
        assert sorted(seen) == sorted(TOPICS)
        d = await feedback.draw(s, "UTC", rng=rng)  # the fifth is the 5th of the day
        await feedback.add(s, "irrelevant", d["ref"], drawn=True)
        assert sum((await feedback.drawn_today(s, "UTC")).values()) == 5
        assert await feedback.draw(s, "UTC", rng=rng) is None  # the budget of 5 is used up
        assert (await feedback.draw(s, "UTC", more=True, rng=rng)) is not None
        with pytest.raises(feedback.FeedbackError, match="Unknown topic"):  # ops alerts are never drawn
            await feedback.draw(s, "UTC", topic="Ops", more=True)


async def test_draw_skips_an_empty_topic_and_returns_none_when_all_are_empty(sm):
    async with sm() as s:
        assert await feedback.draw(s, "UTC") is None  # nothing at all
        await mk_alert(s, topic="India Hiring")
        for _ in range(3):  # three empty topics never block the one that has an alert
            assert (await feedback.draw(s, "UTC", more=True))["topic"] == "India Hiring"
        assert await feedback.draw(s, "UTC", topic="Global Funding") is None


async def test_revision_keeps_drawn_or_chosen_and_undo_returns_the_alert_to_the_pool(sm):
    async with sm() as s:
        d, _ = await mk_alert(s)
        c, _ = await mk_alert(s)
        await feedback.add(s, "useful", d, drawn=True)
        await feedback.add(s, "irrelevant", d)  # revised without --drawn: still a drawn rating
        await feedback.add(s, "useful", c)
        await feedback.add(s, "irrelevant", c, drawn=True)  # revised with --drawn: still a chosen rating, cannot be promoted
        via = {json.loads(r.note)["alert"][:8]: json.loads(r.note)["via"] for r in await feedback_rows(s)}
        assert via == {d: "drawn", c: "chosen"}
        assert await feedback.drawn_today(s, "UTC") == {"India Funding": 1}
        await feedback.undo(s, d)  # undo erases that rating, its drawn status and its place in today's count: the alert is unrated again
        assert await feedback.drawn_today(s, "UTC") == {}
        assert (await feedback.draw(s, "UTC", rng=random.Random(1)))["ref"] == d


async def test_draw_never_returns_a_rated_alert_and_chosen_ratings_do_not_use_the_budget(sm):
    async with sm() as s:
        refs = [(await mk_alert(s, topic="India Funding"))[0] for _ in range(3)]
        for r in refs[:2]:
            await feedback.add(s, "useful", r)  # chosen, not drawn
        assert await feedback.drawn_today(s, "UTC") == {}
        assert (await feedback.draw(s, "UTC", rng=random.Random(1)))["ref"] == refs[2]  # the only unrated one


# ---- statistics that do not overstate ----

def test_wilson_interval():
    lo, hi = feedback.wilson(27, 30)
    assert round(lo, 2) == 0.74 and round(hi, 2) == 0.97
    lo, hi = feedback.wilson(30, 30)
    assert round(lo, 2) == 0.89 and hi > 0.99


async def test_stats_reports_no_estimate_below_the_minimum_and_ignores_chosen_ratings(sm):
    async with sm() as s:
        for i in range(30):  # 30 drawn ratings on one topic and source: 27 useful
            ref, _ = await mk_alert(s, topic="Global Funding", source="Big")
            await feedback.add(s, "useful" if i < 27 else "irrelevant", ref, drawn=True)
        for _ in range(10):  # 10 drawn ratings elsewhere: too few
            ref, _ = await mk_alert(s, topic="India Hiring", source="Small")
            await feedback.add(s, "useful", ref, drawn=True)
        for _ in range(40):  # 40 chosen ratings: never used for precision
            ref, _ = await mk_alert(s, topic="India Hiring", source="Small")
            await feedback.add(s, "irrelevant", ref)
        ref, _ = await mk_alert(s, topic="Global Hiring")
        await feedback.add(s, "incorrect", ref, why="not_india", drawn=True)
        st = await feedback.stats(s)
    by = {g["group"]: g for g in st["by_topic"]}
    big = by["Global Funding"]
    assert big["status"] == "measured" and big["drawn"] == 30 and big["useful"] == 27 and round(big["low"], 2) == 0.74 and round(big["high"], 2) == 0.97
    small = by["India Hiring"]
    assert small["status"] == "insufficient" and "precision" not in small and small["drawn"] == 10 and small["chosen"] == 40
    assert by["Global Hiring"]["status"] == "insufficient"
    assert {g["group"]: g["status"] for g in st["by_source"]}["Big"] == "measured" and st["why"] == {"not_india": 1}
    assert st["rated"] == 81 and st["unrated"] == {}


async def test_precision_is_useful_over_all_four_ratings_and_marks_never_count(sm):
    async with sm() as s:
        orig, _ = await mk_alert(s, topic="Global Funding", source="S")
        for i in range(30):  # 20 useful, 4 irrelevant, 3 incorrect, 3 duplicate: all of the last three count as "not useful", all 30 are the denominator
            ref, _ = await mk_alert(s, topic="Global Funding", source="S")
            kind = "useful" if i < 20 else "irrelevant" if i < 24 else "incorrect" if i < 27 else "duplicate"
            await feedback.add(s, kind, ref, drawn=True, why="other" if kind == "incorrect" else None, of=orig if kind == "duplicate" else None)
            await feedback.add(s, "save", ref, drawn=True)  # marks are not ratings: they change neither numerator nor denominator
        g = {g["group"]: g for g in (await feedback.stats(s))["by_topic"]}["Global Funding"]
    assert (g["drawn"], g["useful"], g["status"]) == (30, 20, "measured") and g["precision"] == pytest.approx(20 / 30)
    assert round(g["low"], 2) == 0.49 and round(g["high"], 2) == 0.81


async def test_stats_with_nothing_rated(sm):
    async with sm() as s:
        await mk_alert(s)
        st = await feedback.stats(s)
    assert st["rated"] == 0 and st["by_topic"] == [] and st["unrated"] == {"India Funding": 1}
