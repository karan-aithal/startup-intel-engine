# Startup Intelligence Engine

A self-hosted platform that discovers, verifies and reports startup funding (pre-seed to Series C), hiring, and emerging startups worldwide, with India as a priority dimension. It targets ₹0 recurring cost and runs on a single Linux machine (16 GB RAM, no GPU).

The full specification is in [PROMPT.md](PROMPT.md). Decisions made so far are in [docs/decisions.md](docs/decisions.md).

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Discovery and foundation | Done |
| 1 | Core data model, source registry, queue | Done |
| 2 | First vertical slice with real alerts (feeds, press wires, Hacker News, SEC Form D, Greenhouse/Lever/Ashby, Telegram, gold-set benchmark) | Next, waiting for go |
| 3 | Inbox-as-API (IMAP) and share-to-bot | Planned |
| 4 | Public-page pollers and feed auto-discovery | Planned |
| 5 | Company, founder and ATS discovery loop | Planned |
| 6 | Local AI (Ollama) | Planned |
| 7 | Verification, correlation, scoring | Planned |
| 8 | Adaptive acquisition and optional connectors | Planned |
| 9 | Dashboard | Planned |
| 10 | Observability, security, backup | Planned |
| 11 | Quality tuning | Planned |
| 12 | Optional and approval-dependent connectors (off by default) | Planned |

The work goes one phase at a time. Each phase ends with a report and waits for an explicit go.

## Stack

- Backend: Python 3.12 (via `uv`), FastAPI, SQLAlchemy 2 (async), Alembic
- Database: PostgreSQL 18 on 127.0.0.1:5432 with `pg_trgm` and pgvector
- Frontend: Next.js, TypeScript, SCSS
- Ports: API 8000, dashboard 3000, both bound to localhost only

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
./scripts/run.sh              # backend on :8000, frontend on :3000, Ctrl+C stops both
./scripts/health_check.sh
```

## Test

```bash
cd backend
unset PYTHONPATH              # ROS on this machine leaks system packages into the venv
uv run pytest                 # runs in a throwaway schema, your real database is untouched
```

## Backups

`scripts/backup.sh` and `scripts/restore.sh` use `~/backups/startup-intel`. The plan is a daily run at 20:00 IST, keeping 7 daily and 4 weekly copies.

## Notes

- Keep the repo on ext4. NTFS breaks venv symlinks and slows database files.
- The machine is not always on (about 12 h/day), so collectors need cursors and catch-up after downtime.
- Raw content is kept for 90 days, or less where a source's terms require.
- Never commit `.env`.
