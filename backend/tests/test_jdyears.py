"""Years of experience from a job description (career-ops jd-years.mjs, simplified): which mentions count and which figure gates."""
import pytest

from app.services.jdyears import years_required as y


@pytest.mark.parametrize("text,years", [
    ("You have 5+ years of experience building backend systems.", 5),
    ("Minimum 3 years of professional experience in software engineering.", 3),
    ("At least five (5) years of relevant experience required.", 5),
    ("3-5 years of experience in data analysis", 3),
    ("<p>Requirements</p><ul><li>7 or more years of experience leading teams</li><li>2 years of experience with Kubernetes</li></ul>", 7),  # the skill figure does not count
    ("Requirements:\n- 4 years of experience in sales\n- 8 years of experience in sales leadership", 4),  # lowest of several required bars
    ("5+ years of experience preferred.", None),
    ("Nice to have:\n- 6 years of experience in fintech", None),  # a preferred section heading
    ("Qualifications:\n- 2 years of experience in QA", 2),
    ("A bachelor's degree or 5 years of equivalent experience.", None),  # the degree route means it never gates
    ("We have been building for 10 years. Our team vests over 4 years.", None),  # not about the candidate
    ("Must be 18 years old. Contract term of 2 years.", None),
    ("Experience: 4 years", 4),
    ("Join a fast team. No experience needed.", None), ("", None), (None, None)])
def test_years_required(text, years):
    assert y(text) == years


# ---- the adapters: Greenhouse deep reads once a day, remote boards carry the figure ----
import httpx

from app.connectors.api import AtsAdapter
from app.connectors.remote_boards import JobicyAdapter, RemotiveAdapter


async def test_greenhouse_reads_content_once_a_day_and_keeps_the_figure():
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        job = {"id": 1, "title": "Backend Engineer", "absolute_url": "https://x/1", "location": {"name": "Bengaluru"}, "updated_at": "2026-11-20T00:00:00Z"}
        if "content=true" in str(r.url):
            job["content"] = "&lt;p&gt;You have 6+ years of experience building services.&lt;/p&gt;"  # Greenhouse HTML-escapes it
        return httpx.Response(200, json={"jobs": [job]})

    a = AtsAdapter("greenhouse", "acme", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first = await a.fetch({})  # a real poll with no deep read on record: deep (a bare fetch(), as board verification does, stays light)
    assert "content=true" in seen[0] and (await a.normalize(first.items[0])).raw_metadata["years_min"] == 6
    plain = await a.fetch(first.cursor)  # inside the day: a plain poll, no figure, and the cursor still remembers the deep read
    assert "content=true" not in seen[1] and "years_min" not in (await a.normalize(plain.items[0])).raw_metadata
    assert plain.cursor["deep_at"] == first.cursor["deep_at"]
    old = await a.fetch({"deep_at": first.cursor["deep_at"] - 90000})
    assert "content=true" in seen[2] and old.cursor["deep_at"] > first.cursor["deep_at"] - 1


async def test_remote_boards_keep_only_the_number():
    jobicy = {"id": 7, "url": "https://jobicy.com/j/7", "jobTitle": "Support Engineer", "companyName": "Acme", "jobGeo": "Anywhere", "pubDate": "2026-11-20 10:00:00",
              "jobDescription": "<p>Requirements</p><ul><li>3+ years of experience in customer support</li></ul>"}
    ev = await JobicyAdapter("Jobicy").normalize(jobicy)
    assert ev.raw_metadata["years_min"] == 3 and "3+ years" not in str(ev.model_dump())
    remotive = {"id": 9, "url": "https://remotive.com/j/9", "title": "Writer", "company_name": "Acme", "candidate_required_location": "Worldwide",
                "publication_date": "2026-11-20T10:00:00", "description": "<p>Join us. No experience needed.</p>"}
    assert "years_min" not in (await RemotiveAdapter("Remotive").normalize(remotive)).raw_metadata
