# Phase 12 plan: data correctness and measurement first

Status: step 1 done (fix and backfill applied 2026-11-17, ts-corrected baseline saved); step 3 done as a read-only report (merge map CSV awaiting the owner's approvals); step 4 sample drawn and preliminarily scored (awaiting the owner's labels and definition); step 5 built as route A (`scripts/feedback.py`, decision 86); step 2 not started. No new automated classification or ranking behaviour.
Standing rules: LLM_ENABLED=false, EU-Startups disabled, AUTO_SOURCE_CAP=0, Jooble off, deletion ledger outside PostgreSQL and in every encrypted backup, dead `llm_enrich` jobs kept (87 today: 81 at the Phase 11 report, 6 more exhausted retries before the model was switched off), no commit or push without approval. The quality report stays read-only and `docs/quality/ranking_baseline-2026-11-17.json` is never edited.

## Order and dependencies

| # | Work | Needs from the owner |
|---|---|---|
| 1 | UTC timestamp correction and backfill audit | go for the fix, then go for the backfill |
| 2 | Funding classifier evaluation and negative categories | about 120 labels |
| 3 | Company identity and duplicate-resolution report | nothing (read-only) |
| 4 | India relevance labelled evaluation | about 50 labels |
| 5 | Feedback capture design | choice of capture route |

1 and 3 can start immediately. 2 and 4 need the owner's labels (I pre-label, the owner corrects, as for the gold set). 5 is a design only.

## 1. UTC timestamp correction

Evidence (read-only audit, 2026-11-17). `connectors/feed.py:50` turns feedparser's UTC tuple into a datetime with `mktime`, which assumes local time. This host is IST (+5:30), so every feed `published_at` is 19,800 s too early.
- Minimum publication-to-detection time per feed is 19,810 to 21,171 s (the 5.5 h offset plus a few seconds of polling delay), for all 12 feeds.
- If the offset were not real, adding 5.5 h would push items into the future. It does so for 0 of 381 feed items (post-first-poll). The same test on Hacker News Algolia, which uses epoch seconds and is correct, flags 56 of 77, as expected.
- Reproduced: feed says 08:30 UTC, stored 03:00 UTC.

Blast radius:
- Affected: 764 feed events and all 53 funding rounds (every round has feed evidence, so `announced_at` is shifted too). Feed-derived values also appear in `company_signals` timing and in the "Published" line of already-sent alerts.
- Not affected: ATS and Hacker News Algolia (API timestamps), Bluesky, GitHub (ISO with zone). The 48 h job age limit applies to ATS jobs only, so alert eligibility does not change. No page-monitor, Telegram-preview or email events exist yet; those connectors parse naive or zone-less dates and should be checked before they are enabled.
- Scoring: freshness changes by 5.5 h on a 7-day half-life, at most about a 2 percent change in the freshness input, so expect at most a point of movement per round, mostly rounding and tie order.

Steps (on approval):
1. Code: `calendar.timegm` in `feed.py`, with a regression test that fails under any host timezone (set `TZ` in the test).
2. Backfill script, dry-run by default, `--apply` explicit: add 19,800 s to `published_at` of events from `feed` routes that have not been fixed, mark each with `raw_metadata.ts_fixed` (added to the retention keep-list so scrubbed rows stay marked and the script is idempotent), report counts before and after, and print the 53 rounds whose `announced_at` will move.
3. Take a database backup first, then apply. The verification loop recomputes rounds and scores on its own; the script waits for it and checks that the number of rounds and the statuses are unchanged.
4. Sent alerts keep their original text (they are a record of what was sent). Their stored payload `published_at` is left as is and the report says so.

Ranking comparison that separates timestamps from real improvements:
- `ranking_baseline-2026-11-17.json` stays as the historical baseline.
- After step 3, save `ranking_baseline-ts-corrected` and compare it to the 17 November file: every difference there is caused by the timestamp correction alone.
- Every later change (classifier, identity merges) is compared against the ts-corrected baseline, never against the 17 November file. Saved rankings gain a `basis` field naming their parent, and `--compare` refuses to compare across a timestamp-basis change without saying so.
- Latency and detection numbers after the fix will drop by about 5.5 h; the report notes the date of the correction so the step is not read as a real speed-up.

## 2. Funding classifier evaluation and negative categories

Today: gold set 74 rows, 24 funding positives, 1 rumour, 0 hiring. Funding precision and recall are both 0.88, which is too small a sample to tune on (one error moves recall by 4 points).
- Grow the labelled set to about 250 events: about 60 flagged funding (precision), about 60 unflagged with a funding cue such as a currency amount or "raise" (recall), about 60 flagged hiring or rumour, the rest random. I pre-label, the owner corrects.
- Negative taxonomy, as extra `correct_label` values that all mean "not a startup funding alert": `fund_close` (VC or PE fund raising), `debt_or_project_finance`, `sponsorship_or_naming`, `acquisition`, `grant`, `earnings_or_revenue`, `ipo_or_listing`, `layoffs_or_shutdown`, `other_none`. They give a confusion matrix, so a rule change shows what it moved.
- Report per category: precision, recall, and the false-positive cause. Offline harness runs a candidate keyword config against the labels and prints the delta; the live config never changes in this phase. Already measured offline: verbs "announces" and "financing" lift funding recall 0.88 to 0.92 with precision flat (a hint, not evidence).
- A "not startup funding" rule needs a new label path, because `not_funding` terms currently produce `rumour`. Design only, applied in a later phase on approval.

## 3. Company identity and duplicate-resolution report (read-only)

Extend `quality_report.py` and `services/quality.py`:
- Names carrying a description (current examples: AI-native CRM Dreamteam, Helsing backer Plural, VC Firm Plural, French robot maker Genesis, Sri Lankan firm): detect with a descriptor word list in `config/quality.yaml` plus the containment test already in the duplicate audit.
- Investors, schools and funds recorded as companies (Canonicus Capital, Gemspring Capital, Nexedge Capital, BITS Pilani): flag by suffix list and by appearing as an investor elsewhere.
- Proposed merge map with impact per pair: rounds, evidence, alerts, jobs and signals that would move, and which name would survive. Output as a CSV for the owner to approve line by line.
- No merge tool and no migration in this phase. A merge needs an audit table (`company_merges`, migration 0009) and an undo path; that is proposed for the next phase once the map is approved.
- Round duplicates: list the Olyv round (series c with no amount, and an unstaged round with 6 M) with its evidence, and recommend.

## 4. India relevance labelled evaluation

Finding: all 33 funding events counted as India are India only through the source's configured region; none through location text.
- Sample about 50 funding and rumour events, stratified: India feeds (Entrackr, Inc42, YourStory, Mint, ET Startups), global feeds that mention India, and unflagged events with India-like words. Columns: title, source, why flagged, link, `is_india_relevant` for the owner. "India relevant" needs a definition from the owner: Indian company, Indian founders, Indian investors, or India operations (recommend: Indian-incorporated or India-headquartered company, or an India-led round).
- Report precision and recall of the current rule (source region OR location text), of each half alone, and of candidate rules, offline. Nothing changes in alerts.

## 5. Feedback capture design (no change to ranking)

The `feedback` table exists (actions: useful, irrelevant, duplicate, incorrect, watch, mute, save, applied) and is empty; nothing writes it.
Options, to choose from:
- A. CLI (`scripts/feedback.py add useful <alert or round id>`): smallest, no new surface, no migration. Recommended first step.
- B. Telegram replies or inline buttons through the existing bot: best ergonomics, needs the bot token (not set today), callback handling in the worker, and an owner-only check.
- C. Dashboard action: needs a write endpoint, which reverses the read-only dashboard decision (decision 70) and needs its own approval.
Design points: record the alert's `dedup_key`, topic, source and round so feedback can be joined to measurement; report feedback-derived precision per source and topic in the quality report (read-only); ranking is untouched, and any use of feedback in ranking is a later, separately approved change measured against the ts-corrected baseline.

## Done when
1. Feed timestamps fixed with a test and a dry-run-then-apply backfill with before and after counts, and a ts-corrected baseline saved beside the 17 November one.
2. A labelled classifier set of about 250 with a negative-category confusion matrix and an offline candidate-config comparison.
3. A merge map CSV and the descriptor, investor and round-duplicate findings in the report.
4. India labels collected, with the current rule scored.
5. A feedback capture design chosen by the owner.
