"""Phase 12 step 2: classifier evaluation is offline, weighted, honest about causes, and cannot change the live classifier."""
import csv
from pathlib import Path

from app.services import prefilter, quality

LABELS = Path(__file__).resolve().parents[2] / "docs" / "classifier_labels.csv"


def row(title, label, weight=1, content=""):
    return {"title": title, "content": content, "correct_label": label, "weight": str(weight), "stratum": "t"}


def test_taxonomy_is_the_planned_one():
    labels, negatives = quality.classifier_labels()
    assert labels == ("funding", "rumour", "hiring", "spam", "none")
    assert negatives == ("fund_close", "debt_or_project_finance", "sponsorship_or_naming", "acquisition", "grant", "earnings_or_revenue", "ipo_or_listing", "layoffs_or_shutdown")


def test_metrics_are_weighted_score_negatives_as_none_and_name_the_cause():
    live = quality.candidate_classifier("current")
    rows = [row("Acme raises $5M Series A", "funding", 1),                   # flagged, right
            row("Beta Capital raises $200M for its new fund", "fund_close", 3),  # flagged funding, really a fund close: weighted false alarm with that cause
            row("Gamma lands $9M seed round", "funding", 1),                  # flagged, right
            row("Delta Labs announces $5 Billion Series C funding", "funding", 4),  # not flagged: a miss that stands for 4 pool events
            row("Weather today", "none", 10),
            row("Odd one", "unsure", 5), row("Blank", "", 5)]                 # skipped
    m = quality.classifier_metrics(rows, live)
    f = m["labels"]["funding"]
    assert (m["rows"], m["skipped"]) == (5, 2)
    assert (f["tp"], f["fp"], f["fn"]) == (2, 3, 4)  # 2 right; the fund close (weight 3) wrongly flagged; the miss (weight 4)
    assert f["precision"] == 2 / 5 and f["recall"] == 2 / 6
    assert m["fp_cause"] == {"fund_close": 3} and m["fn_got"] == {"none": 4} and m["funding_rows_in_unflagged"] == 1


def test_candidates_change_verdicts_offline_and_the_live_classifier_is_untouched():
    cfg_fn = prefilter._cfg
    before = prefilter.classify("Delta Labs announces $5 Billion Series C funding")["label"], prefilter.classify("Norwest closes $4 Bn global fund")["label"]
    assert before == ("none", "funding")
    verbs = quality.candidate_classifier("more_verbs_and_stages")("Delta Labs announces $5 Billion Series C funding")
    neg = quality.candidate_classifier("not_startup_funding_negatives")("Norwest closes $4 Bn global fund")
    assert verbs["label"] == "funding" and neg["label"] == "none" and neg["negative"] == "fund_close"
    assert quality.candidate_classifier("current")("Norwest closes $4 Bn global fund")["label"] == "funding"
    assert prefilter._cfg is cfg_fn  # patched only inside the call, then restored
    assert (prefilter.classify("Delta Labs announces $5 Billion Series C funding")["label"], prefilter.classify("Norwest closes $4 Bn global fund")["label"]) == before
    both = quality.candidate_classifier("all_three")  # combines: verbs + rumour phrases + negatives
    assert both("Delta Labs announces $5 Billion Series C funding")["label"] == "funding" and both("Norwest closes $4 Bn global fund")["label"] == "none"


def test_label_file_is_well_formed():
    labels, negatives = quality.classifier_labels()
    rows = list(csv.DictReader(LABELS.open()))
    assert len({r["event_id"] for r in rows}) == len(rows) and [int(r["row"]) for r in rows] == list(range(1, len(rows) + 1))
    assert {r["suggested"] for r in rows} <= set(labels) | set(negatives) | {"unsure"}
    assert all(float(r["weight"]) >= 1 and r["suggested_reason"] for r in rows)
