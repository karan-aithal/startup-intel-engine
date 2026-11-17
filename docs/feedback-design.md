# Feedback capture design (Phase 12 step 5)

Status: route A approved and built (2026-11-17, decision 86): `scripts/feedback.py`, `services/feedback.py`, `config/quality.yaml` `feedback:`, tests in `tests/test_feedback.py`. Routes B and C stay out of scope. No table changes; ranking, scoring and alert text are untouched. The owner's answers: route A; the eight actions and seven reason codes kept; watch and mute record-only; 5 ratings a day; no reference line in alerts. Differences from the text below: `add` takes `--drawn` (instead of the tool guessing which alerts came from `next`), `undo` takes `--action` for marks other than the rating, and a reference may include the dashes of the id.

## What feedback is for

Feedback measures one thing the other evaluations cannot: **is each alert useful to the owner?** It gives a precision per topic and per source, the reasons an alert was wrong (which feed the negative categories in step 2 and the India rule in step 4), and how often a duplicate slipped through.

It cannot measure recall. The owner only sees alerts that were sent, so a missed event produces no feedback. Recall comes from labelled sets (step 2 for funding, step 4 for India).

It does not touch ranking. Using feedback in ranking is a later, separate decision (last section).

## Facts this design rests on (checked against the live database, 2026-11-17)

- **Volume.** 258 alerts in 3 days (97, 57, 104), about 86 a day. 167 Global Hiring, 47 Global Funding, 27 India Funding, 17 India Hiring. Rating everything is unrealistic and would drown funding in hiring, so capture must work from a sample.
- **The `feedback` table exists and is empty.** Columns: `id`, `action`, `source_event_id`, `company_id`, `funding_round_id`, `job_posting_id`, `note`, `created_at`. Actions allowed by a check constraint: useful, irrelevant, duplicate, incorrect, watch, mute, save, applied. All links are `ON DELETE SET NULL`. Nothing in the code writes it.
- **Alerts carry no usable ids.** `alert_deliveries.company_id`, `funding_round_id` and `job_posting_id` are NULL for all 258 alerts, and the Telegram message shows no reference.
- **An alert resolves to exactly one event** by `dedup_key = 'event:' || content_hash`, plus the source name and URL in the payload. By hash alone 24 alerts match several events (identical job posts); by hash, source and URL all 258 match exactly one. No schema change is needed.
- **Company resolution is partial.** `source_events.company_id` is NULL for all alerted events. 66 of 74 funding alerts reach a company through `funding_round_claims`, and 56 reach a funding round through `evidence`. Hiring alerts have no company row (only 5 ATS boards are tied to a company); 183 of 184 carry the company's name in `source_events.company_name`. So the event is the reliable anchor, the company and round are best effort.
- **`watched` and `muted` on `companies`:** `watched` feeds the auto-watch cap in `ats.py`; `muted` is read by nothing. Recording a "watch" or "mute" must therefore not flip those flags: that would change acquisition or alert behaviour.

## Capture options

| | Route | For | Against |
|---|---|---|---|
| A | Command line `scripts/feedback.py` | Smallest. No new network surface, no migration, nothing changes in alerts. Works today. | Needs a terminal. Rating is a deliberate act, not a tap. |
| B | Telegram replies or inline buttons through the bot | Best ergonomics: rate where the alert arrives. | The bot token is not set (alerts print to the console today). Buttons need callback handling in the worker. Adding buttons or a reference line changes what an alert looks like: needs approval. Owner-only check needed (the share-to-bot allow-list can be reused). |
| C | Dashboard action | One click beside the company page. | Needs a write endpoint and reverses decision 70 (read-only API). The API has no login and listens on 127.0.0.1, so any web page open in the owner's browser could post to it (CORS does not block a simple POST): it would need a token or an origin check first. |

**Recommendation: A now, B when Telegram is switched on, C not planned.** A measures what we need with the least risk, and its rows have the same shape B would write, so nothing is thrown away.

## Design of route A

**Commands.**
- `recent [--topic T] [--n 20]`: the latest alerts with a short reference (the first 8 characters of `alert_deliveries.id`), topic, source, title and any existing rating. No change to the alert text is needed: references come from this list.
- `next [--topic T]`: a random unrated alert, drawn evenly across the four topics so hiring does not swamp funding. This is the unbiased path: label what the draw gives, not only what stood out.
- `add ACTION REF [--why CODE] [--of REF] [--text "..."]`: record a rating. `REF` is the short reference and must match exactly one alert.
- `undo REF`: remove the owner's latest rating of that alert.
- `stats`: ratings so far per topic and source, with the interval from the measurement section.

**Row written to `feedback`** (no migration):
- `action`: one of the existing eight.
- `source_event_id`: the alert's event (always resolvable).
- `company_id` and `funding_round_id`: filled when the claims or evidence give them, else NULL.
- `note`: a JSON object, always valid JSON, with `alert` (the alert's full id, so alert-level queries need no extra join), `topic`, `source`, `company` (name from the payload), `via` (`next` or `chosen`) and optional `why`, `of`, `text`. Free text lives only inside `text`.

**Meaning of each action** (what it measures):
- `useful`, `irrelevant`: precision of the alert. `incorrect`: it was a real alert but wrong, and `--why` is required: `wrong_label`, `wrong_company`, `wrong_amount`, `wrong_stage`, `not_startup_funding` (a fund, a project loan, a naming deal), `not_india`, `other`. These codes line up with the negative categories planned for step 2.
- `duplicate`: the same event alerted twice; `--of REF` names the first alert. Measures dedup misses.
- `save`, `applied`: interest signals on hiring alerts.
- `watch`, `mute`: recorded and counted only. They do not change `companies.watched` or `muted`; turning them into behaviour is a separate approval.

**Rules the tool enforces** (the table cannot): the four rating actions are mutually exclusive per alert, the latest wins (a new rating replaces the earlier one in one transaction, so history is never ambiguous); `incorrect` needs `--why`; `duplicate` needs `--of` pointing at a different alert; a reference that matches zero or several alerts is refused. The tool writes only `feedback`.

## Sampling and statistical limits

- About 86 alerts a day cannot all be rated. A budget of 5 to 10 ratings a day from `next` gives roughly 150 to 300 a month, evenly across topics.
- A precision is reported only once a topic or source has at least 30 ratings, as a Wilson 95 percent interval, never a bare percentage. With 30 ratings the 95 percent interval is 11 points wide when every alert was useful and 34 points wide at 50 percent (for example 27 useful of 30 gives 74 to 97 percent): it can tell a source at 95 percent from one at 60 percent, not 85 from 75.
- Ratings of alerts the owner chose to look at are biased towards the memorable ones; `next` exists to avoid that, and `stats` splits drawn ratings (`via: next`) from chosen ones.

## Measurement (read-only, added to the quality report when the CLI exists)

Checked to run against the live schema (they return no rows today because nothing is rated). Latest rating per alert:

```sql
WITH latest AS (
  SELECT DISTINCT ON (note::jsonb->>'alert') note::jsonb->>'alert' AS alert_id, action, note::jsonb->>'why' AS why
  FROM feedback WHERE action IN ('useful','irrelevant','incorrect','duplicate') AND note ~ '^\{'
  ORDER BY note::jsonb->>'alert', created_at DESC)
SELECT a.topic, count(*) AS rated, count(*) FILTER (WHERE l.action = 'useful') AS useful
FROM latest l JOIN alert_deliveries a ON a.id::text = l.alert_id GROUP BY 1;
```
The same with `a.payload->>'source'` gives precision per source; grouping `incorrect` rows by `why` gives the error breakdown; the sampling frame (unrated alerts per topic) is `alert_deliveries` with no matching `feedback.note->>'alert'`. The alert-to-event key used by the CLI is `a.dedup_key = 'event:' || e.content_hash`, joined to `source_registry` by `payload->>'source'` and to the event by `source_url` or `canonical_url` equal to `payload->>'url'`.

## Using feedback in ranking (not now)

Only after every one of these holds, and only as its own approved change:
1. At least 30 drawn ratings per source or topic that would be adjusted.
2. The proposal changes a display ranking first, not alert thresholds, verification status or what is sent. Muting a company so it stops alerting is an alert-semantics change and needs its own approval.
3. The effect is measured against `ranking_baseline-ts-corrected` with the quality report's compare (overlap and mean rank shift), so a change is attributed to feedback and not to timestamps or classifier edits.
4. Feedback never edits a verification status: confirmed facts and the owner's opinion stay in separate tables.

## Privacy and retention

Feedback is the owner's own opinion, stored locally and covered by the encrypted backups like the rest of the database. Event and company deletions null the links (`SET NULL`); the JSON note keeps only the alert id, topic, source and company name already in the alert. A "forget" request for a person (the deletion ledger) does not reach feedback notes, because those hold company names, not author names.

## Build plan once a route is chosen (route A)

About 150 lines in `scripts/feedback.py` and a `services/feedback.py` for the resolution and the rules, tests on the scratch schema (reference resolution, exclusivity, `--why` and `--of` validation, undo, the sampling draw being even across topics, the SQL above on seeded ratings), a feedback section in `quality_report.py` showing "no ratings yet" until there are some, and a README entry. No migration, no change to alert text, ranking or scoring.

Route B later adds: a bot token and `TELEGRAM_OWNER_ID`, a command parser (`/useful ref`) reusing the share-to-bot allow-list, and, only if the owner approves a change to alert layout, a reference line or buttons.

## Measurement rules as built (decision 87)

- **Precision** = useful / (useful + irrelevant + incorrect + duplicate), using the latest rating of each alert and only ratings made through `next` (`--drawn`). A duplicate counts as not useful. Marks (save, applied, watch, mute) are not in either number. Interval: Wilson score, 95%, reported only from 30 drawn ratings per topic or source; below that, "insufficient sample".
- **Reviewer:** not recorded; one owner is assumed.
- **Drawn status** lives on the rating row. A revision keeps it. `undo` removes the row, so the alert returns to the pool and today's count. A draw that is never rated leaves no trace, and `--drawn` cannot be verified.

## Decisions needed from the owner

1. Route: A now (recommended), or wait for B.
2. The vocabulary: keep the eight actions and the seven `incorrect` reason codes, or change them.
3. Whether `watch` and `mute` should stay record-only (recommended) or later drive behaviour.
4. A daily rating budget you can sustain (5 to 10 recommended).
5. Whether a short reference line may ever appear in alerts (only matters for B).
