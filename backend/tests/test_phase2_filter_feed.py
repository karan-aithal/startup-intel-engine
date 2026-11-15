import httpx
import pytest

from app.connectors.feed import FeedAdapter
from app.services.prefilter import classify

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Acme raises $5 million in Seed round led by X</title><link>https://e.com/a</link><guid>g1</guid>
<pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate><description>d</description></item></channel></rss>"""


def test_funding_detected_with_amount():
    c = classify("Acme raises $5 million in Seed round led by X")
    assert c["label"] == "funding" and c["amount"].startswith("$5")


def test_rumour_is_not_funding():
    assert classify("Monzo in talks to raise up to $500M Series C")["label"] == "rumour"


def test_spam_and_noise():
    assert classify("Pay to apply: registration fee job, we're hiring")["label"] == "spam"
    assert classify("Quarterly profit rises 12%")["label"] == "none"


def test_hiring():
    assert classify("Startup X is now hiring founding engineer")["label"] == "hiring"


@pytest.mark.asyncio
async def test_feed_fetch_normalize_and_304():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == "abc":
            return httpx.Response(304)
        return httpx.Response(200, content=RSS, headers={"etag": "abc"})

    a = FeedAdapter("T", "https://e.com/feed", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await a.fetch()
    ev = await a.normalize(r.items[0])
    assert ev.source_event_id == "g1" and ev.published_at.year == 2026 and ev.detected_at >= ev.published_at
    r2 = await a.fetch(r.cursor)
    assert r2.items == [] and calls[-1] == "abc"


def test_euro_amount_counts():
    c = classify("Sumary lands €22 million to automate accounting")
    assert c["label"] == "funding" and c["amount"].startswith("€22")


def test_summary_recap_of_past_round_is_not_funding():
    # funding keywords come from the title only
    c = classify("ShareChat parent crosses Rs 1,000 Cr revenue", "It raised $200 million in a Series B last year.")
    assert c["label"] == "none"


def test_amount_taken_from_summary():
    c = classify("Acme closes funding round", "The company said it brought in $5 million.")
    assert c["label"] == "funding" and c["amount"] == "$5 million"


def test_raise_verb_and_valuation_headline():
    assert classify("Co-Founders Raise $6M For Mental Health Startup")["label"] == "funding"
    assert classify("Fal doubles its valuation to $16B as demand surges")["label"] == "funding"
    assert classify("Startup plans to raise $50M next year")["label"] == "rumour"  # rumour still wins
