"""Phase 12 step 1: feed timestamps are read as UTC whatever the host timezone."""
import time
from datetime import UTC, datetime

import feedparser
import pytest
from sqlalchemy import text

from app.connectors.feed import FeedAdapter
from app.services import quality, registry, retention

RSS = """<rss version="2.0"><channel><item><title>Acme raises $5M</title><link>https://e.com/a</link>
<pubDate>Tue, 17 Nov 2026 08:30:00 +0000</pubDate></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Acme raises $5M</title><id>x</id><link href="https://e.com/b"/>
<updated>2026-11-17T10:00:00+05:30</updated></entry></feed>"""


@pytest.fixture
def host_tz(monkeypatch):
    """Run a test under a given host timezone (the bug only shows when the host is not UTC)."""
    def use(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/Los_Angeles", "Pacific/Auckland"])
async def test_feed_published_at_is_utc_on_any_host(host_tz, tz):
    host_tz(tz)
    a = FeedAdapter("T", "https://e.com/feed")
    ev = await a.normalize(feedparser.parse(RSS).entries[0])
    assert ev.published_at == datetime(2026, 11, 17, 8, 30, tzinfo=UTC)
    atom = await a.normalize(feedparser.parse(ATOM).entries[0])  # the updated_parsed fallback goes through the same conversion
    assert atom.published_at == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)
    assert ev.raw_metadata["ts_fixed"] is True


async def test_ts_fixed_marker_survives_retention(sm):
    async with sm() as s:
        sid = await registry.register_source(s, "Feed", "feed")
        await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, content, raw_metadata, detected_at, retention_expires_at)
            VALUES (:s, 'a', 'article', 't', 'body', CAST('{"region": "global", "tags": ["x"], "ts_fixed": true}' AS jsonb), now(), now() - interval '1 day')"""), {"s": sid})
        await retention.purge(s)
        meta = (await s.execute(text("SELECT raw_metadata FROM source_events"))).scalar_one()
    assert meta == {"scrubbed": True, "region": "global", "ts_fixed": True}  # tags dropped, marker kept so the backfill never re-shifts it


async def test_backfill_moves_only_unfixed_feed_events_and_only_once(sm):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("backfill", Path(__file__).resolve().parents[2] / "scripts" / "backfill_feed_timestamps.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # defines the SQL; main() only runs from the command line
    t0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
    async with sm() as s:
        feed, api = await registry.register_source(s, "Feed", "feed"), await registry.register_source(s, "Api", "api")
        rows = [("old", feed, t0, "{}"), ("fixed", feed, t0, '{"ts_fixed": true}'), ("nopub", feed, None, "{}"), ("api", api, t0, "{}")]
        for key, sid, pub, meta in rows:
            await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, published_at, raw_metadata, detected_at)
                VALUES (:s, :k, 'article', 't', :p, CAST(:m AS jsonb), now())"""), {"s": sid, "k": key, "p": pub, "m": meta})
        assert (await s.execute(text(mod.UPDATE_SQL), {"off": 19800})).rowcount == 1
        got = {r[0]: (r[1], r[2]) for r in (await s.execute(text("SELECT source_event_id, published_at, raw_metadata FROM source_events"))).all()}
        assert got["old"][0] == datetime(2026, 11, 10, 8, 30, tzinfo=UTC) and got["old"][1]["ts_fixed"] is True
        assert got["fixed"][0] == t0 and got["api"][0] == t0 and got["nopub"][0] is None  # untouched
        assert (await s.execute(text(mod.UPDATE_SQL), {"off": 19800})).rowcount == 0  # a second run finds nothing


# ---- step 3: company identity report (read-only) ----

@pytest.mark.parametrize("name,kinds,cleaned", [
    ("ai native crm dreamteam", ["description_in_name"], "dreamteam"),
    ("french robot maker genesis", ["description_in_name"], "genesis"),
    ("denmark s commit biologics", ["description_in_name"], "commit biologics"),
    ("profitable french insurtech previsia", ["description_in_name"], "previsia"),
    ("freshworks alums", ["description_in_name"], None),  # nothing after the descriptor: needs a human
    ("numa triples arr", ["headline_fragment"], None),
    ("canonicus capital", ["investor_or_institution"], None),
    ("bits pilani", ["investor_or_institution"], None),
    ("genesis ai", [], None), ("remedy robotics", [], None), ("pro climbing league", [], None), ("hundo pizza", [], None), ("meridian robotics systems", [], None)])
def test_classify_name(name, kinds, cleaned):
    from app.services.quality import classify_name

    assert classify_name(name) == {"kinds": kinds, "cleaned": cleaned}


def test_same_round_sees_one_round_reported_in_two_currencies():
    from app.services.quality import same_round

    d = datetime(2026, 11, 12, tzinfo=UTC)
    inr = {"stage": "series c", "amount_original": 580_000_000, "currency": "INR", "announced_at": d, "first_detected_at": d}
    usd = {"stage": None, "amount_original": 6_000_000, "currency": "USD", "announced_at": datetime(2026, 11, 16, tzinfo=UTC), "first_detected_at": d}
    assert same_round(inr, usd, 14)
    assert not same_round(inr, {**usd, "amount_original": 20_000_000}, 14)  # amounts too far apart
    assert not same_round(inr, {**usd, "stage": "seed"}, 14)  # different stage
    assert not same_round(inr, {**usd, "announced_at": datetime(2026, 12, 20, tzinfo=UTC)}, 14)  # too far apart in time
    assert not same_round({**inr, "currency": "USD", "amount_original": 5_000_000}, {**usd, "amount_original": 5_100_000}, 14)  # same currency: 0.5% tolerance


async def test_identity_report_proposes_but_changes_nothing(sm):
    from app.services import quality

    async def company(s, name):
        from app.services.companies import canonical

        return (await s.execute(text("INSERT INTO companies (name, canonical_name) VALUES (:n, :c) RETURNING id"), {"n": name, "c": canonical(name)})).scalar_one()

    async with sm() as s:
        brand, dirty, *_ = [await company(s, n) for n in ("Dreamteam", "AI-native CRM Dreamteam", "Canonicus Capital", "Freshworks alums", "Orakl Oncology")]
        sid = await registry.register_source(s, "Feed", "feed")
        ev = (await s.execute(text("""INSERT INTO source_events (source_id, source_event_id, event_type, title, detected_at) VALUES (:s, 'e', 'article', 't', now()) RETURNING id"""), {"s": sid})).scalar_one()
        await s.execute(text("INSERT INTO funding_round_claims (source_event_id, company_id, origin) VALUES (:e, :c, 'regex')"), {"e": ev, "c": dirty})
        await s.execute(text("INSERT INTO alert_deliveries (topic, payload, dedup_key) VALUES ('India Funding', CAST(:p AS jsonb), 'k')"), {"p": '{"company": "AI-native CRM Dreamteam"}'})
        await s.commit()
        counts = lambda: s.execute(text("SELECT (SELECT count(*) FROM companies), (SELECT count(*) FROM funding_round_claims), (SELECT count(*) FROM alert_deliveries)"))
        before = (await counts()).one()
        d = await quality.identity(s)
        assert (await counts()).one() == before  # read-only
    got = {i["company"]: i for i in d["items"]}
    assert set(got) == {"AI-native CRM Dreamteam", "Canonicus Capital", "Freshworks alums"}  # the clean names are not flagged
    m = got["AI-native CRM Dreamteam"]
    assert (m["action"], m["confidence"], m["survivor"], m["survivor_id"]) == ("merge", "high", "Dreamteam", str(brand))
    assert m["footprint"] == {"funding_round_claims": 1} and m["alerts_by_name"] == 1  # what a merge would move, and the alert text that cannot change
    assert got["Canonicus Capital"]["action"] == "not_a_company" and got["Freshworks alums"]["action"] == "review_name"
    assert d["companies"] == 5 and d["round_pairs"] == []


def test_india_metrics_weights_and_skips_unsure():
    from app.services import quality

    row = lambda label, **kw: {"title": "x", "summary": "", "location": "", "region": "", "is_india_relevant": label, "weight": "1", **kw}
    rows = [row("y", region="india"),  # flagged and right
            row("n", region="india"),  # flagged and wrong
            row("y", title="Indian chip startup"),  # not flagged by the current rule, found by India words
            row("n", weight="4"),  # a stratum standing for 4 events
            row("unsure", region="india")]  # skipped
    by = {m["rule"]: m for m in quality.india_metrics(rows)}
    cur = by["current (source region OR location text)"]
    assert (cur["n"], cur["tp"], cur["fp"], cur["fn"], cur["tn"]) == (4, 1, 1, 1, 4)  # d counts 4 times
    assert cur["precision"] == 0.5 and cur["recall"] == 0.5
    words = by["candidate: India words in title or summary"]
    assert words["tp"] == 0 and words["fn"] == 2  # "Indian" is not one of the city words, so only the cues rule finds row c
    assert by["candidate: current OR India words and cues (Indian, Rs, crore, rupee sign)"]["recall"] == 1.0


# ---- the merge map never overwrites the owner's decisions ----

def _merge_map_module(tmp_path, monkeypatch):
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    qr = importlib.import_module("quality_report")
    monkeypatch.setattr(qr, "Q", tmp_path)
    return qr


def _identity(company="Acme Labs"):
    item = {"action": "rename", "confidence": "medium", "company": company, "company_id": "1", "survivor": None, "survivor_id": None, "cleaned_name": "Acme",
            "reason": "description_in_name", "alerts_by_name": 0, "footprint": {"funding_rounds": 1}, "survivor_footprint": {}}
    return {"items": [item]}


def test_merge_map_is_written_when_missing_or_without_approvals(tmp_path, monkeypatch, capsys):
    qr = _merge_map_module(tmp_path, monkeypatch)
    f = tmp_path / "identity_merge_map.csv"
    qr.write_merge_map(_identity())  # file missing: written
    assert f.exists() and quality.approvals_in(f) == 0
    qr.write_merge_map(_identity("Other Co"))  # file exists but nothing approved yet: regenerated
    assert "Other Co" in f.read_text()


def test_merge_map_refuses_to_overwrite_approvals_and_leaves_the_file_untouched(tmp_path, monkeypatch):
    import csv

    qr = _merge_map_module(tmp_path, monkeypatch)
    f = tmp_path / "identity_merge_map.csv"
    qr.write_merge_map(_identity())
    rows = list(csv.DictReader(f.open()))
    rows[0]["approve"] = "yes: rename to Acme"
    with f.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    before = f.read_bytes()
    assert quality.approvals_in(f) == 1
    with pytest.raises(SystemExit) as e:
        qr.write_merge_map(_identity("Different Co"))
    assert "already holds 1 approval" in str(e.value) and "erase" in str(e.value) and "unchanged" in str(e.value)
    assert f.read_bytes() == before  # byte for byte


@pytest.mark.parametrize("content", [b"action,company,decision\nmerge,Acme,yes\n", b"\xff\xfe\x00bad,bytes\n\x80\n"])
def test_merge_map_refuses_a_file_it_cannot_inspect_and_leaves_it_untouched(tmp_path, monkeypatch, content):
    """No `approve` column (hand-edited header) or not UTF-8: decisions could be hiding there, so never overwrite."""
    qr = _merge_map_module(tmp_path, monkeypatch)
    f = tmp_path / "identity_merge_map.csv"
    f.write_bytes(content)
    with pytest.raises(SystemExit) as e:
        qr.write_merge_map(_identity())
    assert "Not written" in str(e.value) and "unchanged" in str(e.value)
    assert f.read_bytes() == content


def test_merge_map_unreadable_file_is_refused_and_an_empty_file_is_regenerated(tmp_path, monkeypatch):
    qr = _merge_map_module(tmp_path, monkeypatch)
    f = tmp_path / "identity_merge_map.csv"
    f.write_text("")  # zero bytes: nothing to lose
    qr.write_merge_map(_identity())
    assert "approve" in f.read_text().splitlines()[0]
    real_open = quality.Path.open

    def deny_reads(self, mode="r", *a, **k):  # unreadable, works even as root
        if mode == "r":
            raise PermissionError("denied")
        return real_open(self, mode, *a, **k)

    before = f.read_bytes()
    with monkeypatch.context() as m:
        m.setattr(quality.Path, "open", deny_reads)
        with pytest.raises(SystemExit) as e:
            qr.write_merge_map(_identity("Other Co"))
    assert "Not written" in str(e.value) and f.read_bytes() == before
