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
