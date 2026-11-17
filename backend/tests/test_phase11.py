"""Phase 11: measurement is read-only and correct; thresholds moved to config keep today's behaviour."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.services import companies, quality, registry

NOW = datetime.now(UTC)


def test_thresholds_in_config_equal_the_old_hard_coded_values():
    c = quality.cfg()
    assert c["entity_resolution"]["similarity"] == 0.85 and c["dedup"]["window_days"] == 7


def test_gold_metrics_counts_events_once_and_reports_errors():
    rows = [{"title": "a", "correct_label": "funding", "event_group": "g1"}, {"title": "b", "correct_label": "funding", "event_group": "g1"},
            {"title": "c", "correct_label": "none", "event_group": ""}, {"title": "d", "correct_label": "funding", "event_group": ""}]
    got = {"a": "funding", "b": "funding", "c": "funding", "d": "none"}
    m = quality.gold_metrics(rows, lambda t, s: {"label": got[t]})
    f = m["labels"]["funding"]
    assert m["rows"] == 4 and m["events"] == 3  # g1 is one event
    assert (f["tp"], f["fp"], f["fn"]) == (1, 1, 1) and f["precision"] == 0.5 and f["recall"] == 0.5
    assert m["labels"]["hiring"]["precision"] is None
    assert sorted(m["wrong"]) == [("funding", "none", "d"), ("none", "funding", "c")]


def test_compare_rankings_measures_movement():
    a = [{"id": "1", "company": "A"}, {"id": "2", "company": "B"}, {"id": "3", "company": "C"}]
    assert quality.compare_rankings(a, a)["unchanged"]
    b = [{"id": "2", "company": "B"}, {"id": "1", "company": "A"}, {"id": "4", "company": "D"}]
    c = quality.compare_rankings(a, b)
    assert not c["unchanged"] and c["entered"] == ["D"] and c["left"] == ["C"]
    assert abs(c["overlap"] - 2 / 3) < 1e-9 and c["mean_rank_shift"] == 1.0


async def mk_company(s, name):
    cid = (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :c) RETURNING id"), {"n": name, "c": companies.canonical(name)})).scalar_one()
    return cid


async def mk_round(s, cid, stage, amount, days=0):
    await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, amount_usd, announced_at, first_detected_at, verification_status, score_global)
        VALUES (:c, :s, :a, :t, :t, 'VERIFIED', 50)"""), {"c": cid, "s": stage, "a": amount, "t": NOW - timedelta(days=days)})


async def test_duplicate_audit_finds_names_with_a_description_left_in(sm):
    async with sm() as s:
        a = await mk_company(s, "Dreamteam")
        await mk_company(s, "AI-native CRM Dreamteam")  # extraction left a description in the name
        await mk_company(s, "Orakl Oncology")
        await mk_company(s, "Orakl Biotech")  # similar words, different company: below both thresholds
        await mk_company(s, "Acme Robotics Systems")
        await mk_company(s, "Acme Robotics System")  # one letter apart
        await mk_round(s, a, "seed", 5_000_000)
        await mk_round(s, a, None, 5_000_000, days=3)  # same round seen again with the stage missing
        await mk_round(s, a, "series a", 9_000_000, days=2)  # a different round: stage and amount both differ
        for i, (co, amt) in enumerate((("Dreamteam", "$5M"), ("dreamteam ", "$5M"), ("Dreamteam", "$9M"))):  # only the 2nd repeats the 1st
            await s.execute(text("""INSERT INTO alert_deliveries (topic, payload, dedup_key, created_at) VALUES ('India Funding', CAST(:p AS jsonb), :k, :t)"""),
                            {"p": f'{{"company": "{co}", "amount": "{amt}"}}', "k": f"e{i}", "t": NOW + timedelta(hours=i)})
        await s.commit()
        d = await quality.duplicates(s)
    names = {frozenset((p["a"], p["b"])) for p in d["companies"]["pairs"]}
    assert frozenset(("Dreamteam", "AI-native CRM Dreamteam")) in names
    assert frozenset(("Acme Robotics Systems", "Acme Robotics System")) in names
    assert not [n for n in names if "Orakl Biotech" in n]
    assert d["companies"]["duplicates"] == 2 and d["companies"]["total"] == 6 and not d["companies"]["ok"]  # 33% is above the 5% target
    assert d["rounds"]["duplicates"] == 1 and d["rounds"]["total"] == 3
    assert d["funding_alerts"]["duplicates"] == 1 and d["funding_alerts"]["total"] == 3


async def test_detection_latency_ignores_the_first_poll_backfill(sm):
    async with sm() as s:
        sid = await registry.register_source(s, "Feed", "feed")
        await s.execute(text("INSERT INTO source_runs (source_id, started_at) VALUES (:i, :t)"), {"i": sid, "t": NOW - timedelta(days=2)})
        rows = [("backfill", NOW - timedelta(days=2, hours=-0.2), NOW - timedelta(days=2, hours=30)),  # seen in the first minutes: 30 h "late"
                ("live", NOW - timedelta(hours=3, minutes=50), NOW - timedelta(hours=4))]  # 10 minutes after publication
        for key, detected, published in rows:
            await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, published_at, detected_at)
                VALUES (:s, :k, 'article', 't', :p, :d)"""), {"s": sid, "k": key, "p": published, "d": detected})
        await s.commit()
        r = (await quality.latency(s))["detection"]
    assert len(r) == 1 and r[0]["n"] == 1 and 500 < float(r[0]["p50"]) < 700


async def test_entity_resolution_threshold_comes_from_config(sm, monkeypatch):
    async with sm() as s:
        await mk_company(s, "Meridian Robotics Systems")
        assert (await companies.match_company(s, "Meridian Robotics System"))[0] is not None  # similar enough at 0.85
        monkeypatch.setattr(companies, "quality_cfg", lambda: {"entity_resolution": {"similarity": 0.999}})
        assert (await companies.match_company(s, "Meridian Robotics System"))[0] is None
