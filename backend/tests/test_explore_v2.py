"""J5 Explore v2: fit / role / seniority rules, the funding badge, open or closed from the job record, sorts, and that filters still compose."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.api import intel
from app.services import jobfit, registry

NOW = datetime.now(UTC)


@pytest.mark.parametrize("title,level,role", [
    ("Senior Software Engineer (Infra)", "senior", "engineering"), ("Senior Staff Software Engineer", "above", "engineering"),
    ("Data Engineer Intern", "intern", "data"), ("Junior Product Manager", "junior", "product"), ("Software Engineer II", "mid", "engineering"),
    ("Account Executive, Commercial", None, "sales"), ("Director of Marketing", "above", "marketing"), ("Solutions Architect", None, "customer"),
    ("Underwater Basket Weaver", None, "other"), (None, None, "other")])
def test_seniority_and_role_rules(title, level, role):
    assert (jobfit.seniority(title), jobfit.role_category(title)) == (level, role)


def test_fit_hint_follows_the_targets():
    assert jobfit.fit("Senior Backend Engineer", "Bengaluru, India", False) == "strong"  # target level at a preferred place
    assert jobfit.fit("Senior Backend Engineer", "Remote (worldwide)", True) == "strong"  # or remote
    assert jobfit.fit("Senior Backend Engineer", "Pune", False) == "related"  # target level elsewhere in India
    assert jobfit.fit("Backend Engineer", "Bengaluru", False) == "related"  # no level named
    assert jobfit.fit("Staff Engineer", "Bengaluru", False) == "weak"  # above the targets


async def job(s, source, eid, title, location, *, days=1, company="Acme", cid=None):
    sid = await registry.register_source(s, source, "api")
    await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, location, company_name, company_id, raw_metadata, published_at, detected_at)
        VALUES (:s, :i, 'job', :t, :l, :c, :cid, '{}', :p, :p)"""), {"s": sid, "i": eid, "t": title, "l": location, "c": company, "cid": cid, "p": NOW - timedelta(days=days)})
    return sid


@pytest.fixture
async def seeded(sm, monkeypatch):
    monkeypatch.setattr(intel, "get_sessionmaker", lambda: sm)
    async with sm() as s:
        rich, poor = [(await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :n) RETURNING id"), {"n": n})).scalar_one() for n in ("Fundo", "Poorco")]
        for status, stage, ago, rumour in (("VERIFIED", "Series A", 30, False), ("DISCOVERED", "Seed", 5, False), ("VERIFIED", "Seed", 400, False)):
            await s.execute(text("""INSERT INTO funding_rounds (company_id, stage, announced_at, verification_status, is_rumour)
                VALUES (:c, :s, :a, :v, :r)"""), {"c": rich, "s": stage, "a": NOW - timedelta(days=ago), "v": status, "r": rumour})
        await s.execute(text("INSERT INTO funding_rounds (company_id, stage, announced_at, verification_status, is_rumour) VALUES (:c, 'Seed', :a, 'VERIFIED', true)"),
                        {"c": poor, "a": NOW - timedelta(days=2)})  # a rumour never badges
        a = await job(s, "Himalayas", "1", "Senior Backend Engineer", "Remote (India)", days=3, company="Fundo", cid=rich)
        b = await job(s, "Jobicy", "2", "Staff Engineer", "Bengaluru", days=1, company="Poorco", cid=poor)
        c = await job(s, "lever:zed", "3", "Product Designer", "Pune", days=2, company="Alpha")
        d = await job(s, "lever:zed", "4", "Gone Role", "Pune", days=2, company="Alpha")
        for sid, ext, open_ in ((a, "1", True), (b, "2", True), (c, "3", True), (d, "4", False)):  # c has a record too; "Gone Role" is closed
            await s.execute(text("""INSERT INTO job_postings (source_id, external_job_id, title, status, closed_at)
                VALUES (:s, :e, 't', 'open', CASE WHEN :o THEN NULL ELSE now() END)"""), {"s": sid, "e": ext, "o": open_})
        await s.commit()


def call(**kw):
    return intel.explore(**{"mode": "all", "q": "", "exclude": "", "location": "", "source": "", "days": 7, "limit": 50, "offset": 0, "sort": "newest", "status": "open", **kw})


async def test_rows_carry_fit_role_funding_and_open(sm, seeded):
    by = {j["title"]: j for j in (await call())["items"]}
    assert "Gone Role" not in by  # closed in the job record: hidden by default
    s = by["Senior Backend Engineer"]
    assert (s["fit"], s["role"], s["seniority"], s["open"]) == ("strong", "engineering", "senior", True)
    assert s["funding"]["stage"] == "Series A" and s["funding"]["status"] == "VERIFIED"  # the verified round in the window, not the unverified or the 400-day-old one
    assert by["Staff Engineer"]["fit"] == "weak" and by["Staff Engineer"]["funding"] is None  # a rumour does not badge
    assert by["Product Designer"]["funding"] is None


async def test_status_all_shows_the_closed_job(sm, seeded):
    d = await call(status="all")
    assert {j["title"]: j["open"] for j in d["items"]}["Gone Role"] is False and d["counts"]["all"] == 4


@pytest.mark.parametrize("sort,first", [("newest", "Staff Engineer"), ("fit", "Senior Backend Engineer"), ("company", "Product Designer"), ("funded", "Senior Backend Engineer")])
async def test_sorts(sm, seeded, sort, first):
    d = await call(sort=sort)
    assert d["items"][0]["title"] == first
    assert d["items"][-1]["title"] != first


async def test_filters_still_compose_with_the_new_ones(sm, seeded):
    d = await call(q="engineer", exclude="staff", location="india", sort="fit", source="Himalayas")
    assert [j["title"] for j in d["items"]] == ["Senior Backend Engineer"] and d["total"] == 1


async def test_years_first_seen_and_reposts_on_explore_rows(sm, seeded):
    async with sm() as s:
        sid = (await s.execute(text("SELECT id FROM source_registry WHERE name = 'Himalayas'"))).scalar_one()
        await s.execute(text("UPDATE job_postings SET years_min = 12, first_seen_at = now() - interval '2 days' WHERE source_id = :s AND external_job_id = '1'"), {"s": sid})
        # the same role listed again under a new id 20 days later: a repost; a sibling listed the same day is not
        for ext, ago in (("1b", 20), ("1c", 2)):
            await s.execute(text("""INSERT INTO job_postings (source_id, external_job_id, title, company_name, status, first_seen_at)
                VALUES (:s, :e, :t, 'Alpha', 'open', now() - make_interval(days => :a))"""), {"s": sid, "e": ext, "t": "Product Designer", "a": ago})
        await s.commit()
    by = {j["title"]: j for j in (await call())["items"]}
    s = by["Senior Backend Engineer"]
    assert s["years_min"] == 12 and s["above_band"] and s["fit"] == "weak" and s["first_seen_at"] is not None  # asks more than max_years: above the band
    assert by["Product Designer"]["reposted"] and not s["reposted"]
