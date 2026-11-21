# Startup Intelligence Engine

A self-hosted platform that discovers, verifies and reports startup funding (pre-seed to Series C), hiring, and emerging startups worldwide, with India as a priority dimension. It targets ₹0 recurring cost and runs on a single Linux machine (16 GB RAM, no GPU).

The full specification is in [PROMPT.md](PROMPT.md). Decisions made so far are in [docs/decisions.md](docs/decisions.md).

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Discovery and foundation | Done |
| 1 | Core data model, source registry, queue | Done |
| 2 | First vertical slice with real alerts (feeds, press wires, Hacker News, SEC Form D, Greenhouse/Lever/Ashby, Telegram, gold-set benchmark) | Done |
| 3 | Inbox-as-API (IMAP) and share-to-bot | Done |
| 4 | Public-page pollers and feed auto-discovery | Done |
| 5 | Company, founder and ATS discovery loop | Done |
| 6 | Local AI (llama-server) | Done |
| 7 | Verification, correlation, scoring | Done |
| 8 | Adaptive acquisition and optional connectors | Done |
| 9 | Dashboard | Done |
| 10 | Observability, security, backup | Built, awaiting acceptance |
| 11 | Quality tuning | Built (decision 79) |
| 12 | Data correctness and measurement (feed timestamps, classifier and India evaluation, identity report, feedback capture) | In progress, see `docs/phase12-plan.md` |
| 13 | Company merges, renames and exclusions | Built (decision 92) |
| Jobs PR #4 | India and remote-open-to-India jobs: `services/geo.py`, Himalayas and Remotive, `/explore` | Merged, recorded in decisions 93 to 96 |
| Jobs J0 | Baseline check on real data, Himalayas and Remotive enabled, gaps measured | Awaiting acceptance (decisions 94, 97, 98) |
| Jobs J1 | Write guard and the job record (`job_postings`, migration 0011) | Applied to the live database 2026-11-18 (migration 0011, 7184 records backfilled); awaiting acceptance (decisions 100, 101) |
| Jobs J2 | Keka, Darwinbox, TurboHire, Zoho Recruit and SmartRecruiters boards; workplace type; small-board flag; aggregator list; fetch guard | Built and tested (312 tests), no migration; Darwinbox live smoke blocked by Cloudflare (decisions 102 to 105) |
| Jobs J3 | Jobicy, We Work Remotely and Working Nomads boards | Built, tested (318 tests) and enabled live 2026-11-21 (decision 106) |
| Jobs J4 | Portals tab: board health, counts, enable/disable, re-check, add from a careers URL | Built and tested (342 tests, `tsc`, `eslint`, `next build`), live on the API and dashboard 2026-11-21, not committed (decision 107) |
| Jobs J5 | Explore v2: fit, role and funding badge, open or closed, sorts, new-since-last-visit dot, measured geo accuracy | Built and tested (444 tests, `tsc`, `eslint`, `next build`), live on the API and dashboard 2026-11-21, not committed; owner label corrections pending (decision 108) |

The work goes one phase at a time. Each phase ends with a report and waits for an explicit go.

## Stack

- Backend: Python 3.12 (via `uv`), FastAPI, SQLAlchemy 2 (async), Alembic
- Database: PostgreSQL (14 on this box; any version with pgvector) on 127.0.0.1:5432 with `pg_trgm` and pgvector
- Frontend: Next.js, TypeScript, SCSS
- Ports: API 8000, dashboard 3010, both bound to localhost only

## Layout

```
backend/    FastAPI app (api, connectors, core, db, services), Alembic migrations, tests
frontend/   Next.js dashboard
scripts/    bootstrap, db_setup, run, backup, restore, health_check, install_docker
docs/       decisions.md
logs/       backend.log, frontend.log (not committed)
```

## Setup

```bash
cp .env.example .env          # fill in values printed by scripts/db_setup.sh
./scripts/bootstrap.sh        # install dependencies
./scripts/db_setup.sh         # create the startup_intel database and role
cd backend && uv run alembic upgrade head
```

## Run

```bash
./scripts/run.sh              # backend on :8000, frontend on :3010, Ctrl+C stops both
cd backend && uv run python ../scripts/seed_sources.py --enable   # register approved sources (SEC stays off until SEC_CONTACT_EMAIL is set)
cd backend && uv run python -m app.worker   # collect, classify, deliver alerts (console until TELEGRAM_* is set)
cd backend && uv run python ../scripts/gold_report.py   # precision/recall once docs/gold_set_candidates.csv is labelled
./scripts/health_check.sh
```

## Test

```bash
cd backend
unset PYTHONPATH              # ROS on this machine leaks system packages into the venv
uv run python -m pytest       # runs in a throwaway schema, your real database is untouched
```

## Operations (Phase 10)

Everything here listens on 127.0.0.1 only.

| What | Where |
|---|---|
| Metrics (Prometheus text, computed from the database, so restarts lose nothing) | `http://127.0.0.1:8000/metrics` |
| Prometheus and Grafana (dashboard "Startup Intelligence: operations", read-only, no login) | `docker compose -f ops/docker-compose.yml up -d`, then `127.0.0.1:9191` and `127.0.0.1:3002` |
| Failure alerts to the Telegram "Ops" topic (console if Telegram is not set up) | worker loop every 5 min, plus `backend` `python -m app.watchdog` from cron |
| Scheduled jobs: backup 20:00 IST, restore test Sunday, watchdog, log rotation | `scripts/install_cron.sh` (re-run safe, `--remove` undoes it) |
| One-shot health check (API, database, metrics, worker heartbeat, frontend) | `scripts/health_check.sh` |

The watchdog alerts, once per episode, for: a source that failed 3 times in a row; a source that answers but returned nothing for 24 h after a week of items; jobs that ran out of retries or sit pending 30 min; alerts that cannot be delivered or are overdue; a silent worker (no heartbeat for 3 min); a backup older than 36 h. Failed notifications retry with backoff (8 attempts). Prometheus rules in `ops/alerts.yml` show in the Prometheus UI only; there is no Alertmanager.

## Backups and recovery

`scripts/backup.sh` writes to `~/backups/startup-intel` (owner-only): `daily/` (7 kept), `weekly/` (4 kept, Sundays) and `config/` (7 kept), an AES-256 bundle of `.env`, `config/` and the migrations. The key is `~/.config/startup-intel/backup.key`, created on first run. **Copy that key somewhere else now**: without it the bundle cannot be opened.

Recovery, in order:

1. Stop the worker and API.
2. `scripts/restore_test.sh <file.dump>` proves the dump is good (it restores into a throwaway cluster and compares row counts; no change to your data). It also runs weekly from cron.
3. `scripts/restore.sh <file.dump>` overwrites the live database, then runs the retention purge, which also replays the deletion ledger: a backup can hold raw content that live retention removed, or a person you have since asked to forget, and both are removed again automatically.
4. Lost `.env`, `config/` or the deletion ledger: `openssl enc -d -aes-256-cbc -pbkdf2 -pass file:~/.config/startup-intel/backup.key -in ~/backups/startup-intel/config/<file>.enc | tar -xz -C <target dir>`, then `chmod 600 .env`, and put `deletions.jsonl` back in `~/.config/startup-intel/`.
5. Start the API and worker, then `scripts/health_check.sh`. Queued jobs and unsent alerts resume on their own: leases expire and the work is re-claimed.

## Retention and deletion

Collected events stay as evidence references. After a source's `retention_days` (90 by default, 30 for email and Telegram) the hourly purge removes the event's text, author name and profile link and keeps its title, links, dates, derived fields and a few classification keys (`raw_metadata.scrubbed` marks it). Finished queue jobs are deleted after 30 days, dead ones after 90, source run history after 90, stale worker heartbeats after 7, model output text after 90. `python -m app.retention forget "<name or URL>"` clears a person's name and profile link on request and records the request on a deletion ledger, `~/.config/startup-intel/deletions.jsonl` (outside the database, mode 600, SHA-256 hashes only, included in every encrypted backup). Every purge replays the ledger, so a restore cannot bring a forgotten person back.

## Quality (Phase 11)

`cd backend && uv run python ../scripts/quality_report.py` is read-only and prints classifier precision and recall on the gold set, alert and detection latency, source contribution, why events count as India, duplicate rates against the 5 percent target, and the funding rankings. `--save NAME` stores the rankings as a "before" in `docs/quality/`; after any scoring or threshold change, `--compare NAME` shows how many of the top 25 moved. Thresholds for entity matching and event dedup are in `config/quality.yaml` (defaults equal the old hard-coded values). Alert and scoring rules are unchanged.

## Roadmap (not built yet)

- Least-privilege database roles: separate read-only and backup roles from the owner role the app uses today (needs a one-time superuser step).
- Deletion sync from sources that require it (none in use today).
- Phase 12 (data correctness and measurement: feed timestamp fix, classifier and India evaluation, company identity report, feedback capture design): `docs/phase12-plan.md`; steps 1, 3, 4 and 5 are done or designed, step 2 is built; 50 of the 202 rows in `docs/classifier_labels.csv` are owner-reviewed (30 disagreement rows plus a 20-row agreement spot-check), the other 152 carry only my preliminary label. `cd backend && uv run python ../scripts/classifier_labels.py evaluate` scores your labels only and skips blank rows; with `--use-suggested` a blank `correct_label` falls back to my `suggested` label and the report is headed PRELIMINARY with the owner / fallback row counts (also on `candidates`). `sample-validation` draws a fresh seeded set into `docs/classifier_labels_validation.csv` (42 rows, owner-reviewed from headlines and summaries); add `--validation` to `evaluate` or `candidates` to score it. Feedback capture: `docs/feedback-design.md`; rate alerts with `cd backend && uv run python ../scripts/feedback.py next` (5 a day), `add`, `undo`, `stats`.
- Phase 13 (company merges): `cd backend && uv run python ../scripts/company_merges.py` is a dry run of the approved `docs/quality/identity_merge_map.csv` decisions (merge, rename, not-a-company); `--apply` commits (needs a backup from the last 2 hours), `--undo ID` previews reversing one change (`--apply` commits it), `--list` shows the audit trail. Needs migration 0009. Nothing is deleted; see `docs/decisions.md` row 92.
- See `docs/decisions.md` rows 78 to 81 for the open Phase 11 proposals that need approval.

## Security

Secrets live only in `.env` (mode 600, never committed; the worker warns if it is not 600). Log lines and stored error text are redacted for configured secrets, Telegram bot tokens, bearer tokens and URL passwords. API, dashboard, Prometheus and Grafana bind to 127.0.0.1 and have no login (single owner, local machine).

## Notes

- Keep the repo on ext4. NTFS breaks venv symlinks and slows database files.
- The machine is not always on (about 12 h/day), so collectors need cursors and catch-up after downtime.
- Raw content is kept for 90 days, or less where a source's terms require.
- Never commit `.env`.
