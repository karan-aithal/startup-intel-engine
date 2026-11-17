"""Phase 9: dashboard endpoints. India is derived at read time with the alert rule, filters partition the data, company pages are complete."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api import intel
from app.services import registry
from app.services.pipeline import INDIA

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def use_test_db(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)


async def mk_company(s, name):
    return (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :n) RETURNING id"), {"n": name})).scalar_one()


async def mk_round(s, cid, source, location, status, stage="seed", region=None, days=2):
    sid = await registry.register_source(s, source, "feed")
    e = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, raw_metadata, published_at, detected_at)
        VALUES (:s, :i, 'article', 'raises', :l, CAST(:m AS jsonb), :p, :p) RETURNING id"""),
        {"s": sid, "i": f"{source}-{cid}", "l": location, "m": '{"region": "india"}' if region else "{}", "p": NOW - timedelta(days=days)})).scalar_one()
    f = (await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, amount_original, currency, amount_usd, announced_at, first_detected_at, verification_status,
        confidence, score_global, score_india) VALUES (:c, :st, 5000000, 'USD', 5000000, :p, :p, :v, 80, 60, 70) RETURNING id"""),
        {"c": cid, "st": stage, "p": NOW - timedelta(days=days), "v": status})).scalar_one()
    await s.execute(text("""INSERT INTO evidence (company_id, funding_round_id, source_event_id, evidence_type, weight, url, created_at)
        VALUES (:c, :f, :e, 'reputable_publication', 90, 'https://example.com/a', :p)"""), {"c": cid, "f": f, "e": e, "p": NOW - timedelta(days=days)})
    return f


async def mk_jobs(s, cid, token, locations):
    await s.execute(text("INSERT INTO ats_boards (company_id, provider, board_token, board_url, status) VALUES (:c, 'greenhouse', :t, 'https://example.com/jobs', 'active')"),
                    {"c": cid, "t": token})
    sid = await registry.register_source(s, f"greenhouse:{token}", "api")
    for i, loc in enumerate(locations):
        await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, published_at, detected_at)
            VALUES (:s, :i, 'job', :t, :l, :p, :p)"""), {"s": sid, "i": f"{token}{i}", "t": f"Engineer {token}{i}", "l": loc, "p": NOW - timedelta(days=1)})


@pytest.fixture
async def seeded(sm):
    async with sm() as s:
        ids = {n: await mk_company(s, n) for n in ("Indico", "Globex", "Delhico", "Hirely", "Tagged")}
        await mk_round(s, ids["Indico"], "Entrackr", "Bengaluru, India", "VERIFIED", "series a")
        await mk_round(s, ids["Globex"], "TechCrunch All", "London", "PARTIALLY_VERIFIED", "seed")
        await mk_round(s, ids["Tagged"], "Inc42", None, "PARTIALLY_VERIFIED", "seed", region=True)  # no location, region metadata says india
        await mk_jobs(s, ids["Indico"], "indico", ["Bengaluru, India", "Remote, US"])
        await mk_jobs(s, ids["Delhico"], "delhico", ["New Delhi"])
        await mk_jobs(s, ids["Hirely"], "hirely", ["Remote, US"])
        await s.commit()
    return {k: str(v) for k, v in ids.items()}


def names(d, key="name"):
    return {x[key] for x in d["items"]}


def test_sql_word_boundary_rule_matches_the_alert_rule():
    # the endpoint hands Postgres a \y version of pipeline.INDIA: "Indiana" must not count as India in either engine
    assert "\\y" in intel.RX and "\\b" not in intel.RX
    assert INDIA.search("Pune, India") and not INDIA.search("Indiana")


async def test_region_partitions_funding_and_companies(sm, seeded):
    f = {r: await intel.funding(r, "", "", "recent", 50, 0) for r in ("all", "india", "global")}
    assert names(f["india"], "company") == {"Indico", "Tagged"}  # location rule and region metadata rule
    assert names(f["global"], "company") == {"Globex"}
    assert f["all"]["total"] == f["india"]["total"] + f["global"]["total"] == 3
    c = {r: await intel.companies("", r, "name", 50, 0) for r in ("all", "india", "global")}
    assert names(c["india"]) == {"Indico", "Tagged", "Delhico"}  # Delhico is India through its job location only
    assert names(c["global"]) == {"Globex", "Hirely"}
    assert c["all"]["total"] == 5
    o = await intel.overview("india")
    assert o["stats"]["companies"] == 3 and o["stats"]["rounds"] == 2


async def test_funding_status_stage_and_score_filters(sm, seeded):
    assert names(await intel.funding("all", "verified", "", "recent", 50, 0), "company") == {"Indico"}
    assert names(await intel.funding("all", "", "SEED", "recent", 50, 0), "company") == {"Globex", "Tagged"}  # stage match ignores case
    india = await intel.funding("india", "", "", "score", 50, 0)
    assert india["items"][0]["score_india"] == 70 and all(i["india"] for i in india["items"])
    assert (await intel.funding("all", "", "", "recent", 1, 1))["total"] == 3  # paging keeps the full total


async def test_hiring_filters_by_region_text_and_company(sm, seeded):
    h = await intel.hiring("india", "", "", 50, 0)
    assert h["total"] == 2 and {j["location"] for j in h["items"]} == {"Bengaluru, India", "New Delhi"}
    assert (await intel.hiring("global", "", "", 50, 0))["total"] == 2
    assert (await intel.hiring("all", "delhico", "", 50, 0))["total"] == 1
    one = await intel.hiring("all", "", seeded["Hirely"], 50, 0)
    assert one["total"] == 1 and one["items"][0]["company"] == "Hirely"
    assert {t["company"] for t in one["top_companies"]} == {"Hirely"}


async def test_company_page_shows_funding_jobs_sources_and_evidence(sm, seeded):
    d = await intel.company(seeded["Indico"])
    assert d["company"]["india"] is True
    assert len(d["rounds"]) == 1 and d["rounds"][0]["verification_status"] == "VERIFIED" and d["rounds"][0]["india"] is True
    assert d["jobs_total"] == 2 and len(d["jobs"]) == 2
    assert [b["board_token"] for b in d["ats_boards"]] == ["indico"]
    assert len(d["evidence"]) == 1 and d["evidence"][0]["source"] == "Entrackr"
    kinds = [t["kind"] for t in d["timeline"]]
    assert "evidence" in kinds and "round" in kinds
    ats = [t["at"] for t in d["timeline"] if t["at"]]
    assert ats == sorted(ats, reverse=True)
    for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        with pytest.raises(HTTPException) as e:
            await intel.company(bad)
        assert e.value.status_code == 404


async def test_search_escapes_wildcards_and_respects_region(sm, seeded):
    assert (await intel.search("%%", "all"))["events"] == []  # a literal "%%", not "match everything"
    r = await intel.search("indico", "india")
    assert [c["name"] for c in r["companies"]] == ["Indico"]
    assert (await intel.search("indico", "global"))["companies"] == []
    assert any(e["event_type"] == "job" for e in (await intel.search("engineer", "india"))["events"])


async def test_source_health_and_discovery_return_rows(sm, seeded):
    s = await intel.source_health()
    assert {x["name"] for x in s} >= {"Entrackr", "greenhouse:indico"}
    d = await intel.discovery()
    assert [b["company"] for b in d["boards"] if b["board_token"] == "indico"] == ["Indico"]
