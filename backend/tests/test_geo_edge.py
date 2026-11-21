import pytest

from app.services.geo import classify_geo


# Edge cases from career-ops' location tests (whitespace and dash folding, hybrid and onsite in the text, exclusions), kept as one table.
@pytest.mark.parametrize("loc,expected", [
    ("Remote (India)", (True, True)), ("Remote – India", (True, True)), ("Remote India", (True, True)),
    ("Remote Worldwide except India", (False, False)), ("Remote - India excluded", (False, False)), ("Remote (excluding India)", (False, False)),
    ("Remote, outside India", (False, False)), ("Remote - Not India", (False, False)), ("Remote Worldwide, except US", (False, False)),
    ("Hybrid Bengaluru (remote Friday)", (True, False)), ("Bengaluru (Hybrid)", (True, False)), ("On-site Bengaluru", (True, False)),
    ("Non‑Remote, Bengaluru", (True, False)), ("Remote, India or US", (True, True)), ("Anywhere in the world", (False, True)),
    ("Remote - US only", (False, False)), ("Remote Europe — UK only", (False, False)), ("Remote CET working hours", (False, False)),
    ("Remote in MO", (False, False)), ("Pune, India or Remote", (True, True))])
def test_geo_edge_cases(loc, expected):
    assert classify_geo(loc) == expected
