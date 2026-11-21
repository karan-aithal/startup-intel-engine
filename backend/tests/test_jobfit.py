"""Seniority rules ported from career-ops classify-tier.mjs (its own --test cases and its guards), mapped to this app's levels:
career-ops senior -> senior, or above for chief / VP / director / principal / staff / head of; entry -> junior; its default "mid" is None here."""
import pytest

from app.services import jobfit


@pytest.mark.parametrize("title,level", [
    ("Software Engineer Intern", "intern"), ("Junior Software Engineer", "junior"), ("Software Engineer I", "junior"), ("Software Engineer II", "mid"),
    ("Software Engineer III", "senior"), ("Senior Software Engineer", "senior"), ("Sr. Software Engineer", "senior"), ("Staff Engineer", "above"),
    ("Principal Engineer", "above"), ("VP of Engineering", "above"), ("Head of Data", "above"), ("Engineering Intern Program", "intern"),
    ("Software Engineer", None), ("Senior Intern Coordinator", "senior"), ("Graduate Engineer", None), ("Graduate Engineer Program", "intern"),
    ("A.I. Researcher", None), ("I.T. Specialist II", "mid"), ("Engineer L1", "junior"), ("Engineer L5", "mid"), ("Mid-level Backend Engineer", "mid"),
    ("Lead Product Designer", "senior"), ("Lead Generation Specialist", None), ("Senior Living Advisor", None), ("Management Trainee", "intern"),
    ("Associate Software Engineer", "junior"), ("Associate Attorney", "junior"), ("Associate Manager, Ops", "junior"),  # associate is the junior variant ...
    ("Associate Director, Product", "above"), ("Associate Vice President", "above"), ("Associate Professor of Physics", "above"),  # ... unless a senior noun follows
    ("Intern, Associate Dean of Student Life", "intern"),  # a junior marker that leads still decides it
    ("Summer Intern, Director of Product", "intern"), ("Senior Staff Software Engineer", "above"), ("Senior Director, Sales", "above"),
    ("Intern Program Director", "above"), ("Junior Staff Accountant", "junior"),
    ("Fresher - QA", "junior"), ("New Grad Software Engineer", "junior"), (None, None)])
def test_seniority(title, level):
    assert jobfit.seniority(title) == level


def test_stem_keywords_match_word_starts():
    assert jobfit.role_category("Engineering Manager") == "engineering"  # stem:engineer
    assert jobfit.role_category("Marketing Lead") == "marketing" and jobfit.role_category("Reengineer Basket") == "other"  # not mid-word
