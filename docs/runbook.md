# Runbook: the Jobs track

Short, for the owner. Everything is localhost: API 127.0.0.1:8000, dashboard 127.0.0.1:3010. Alerts go to the console until `TELEGRAM_*` is set.

## Start, stop, restart

```
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000     # API (no reload: restart it after backend changes)
cd backend && uv run python -m app.worker                                  # collection, scans, reminders, alerts (restart after backend changes)
cd frontend && npm run build && npm run start -- -p 3010                   # dashboard
```
Run backend commands with `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u ROS_DISTRO` (ROS on this machine breaks the venv). Logs are in `logs/`.

## Is it healthy?

| Look at | Healthy |
|---|---|
| `/sources` and `GET /metrics` | `worker_heartbeat_age_seconds` under 180; sources HEALTHY |
| `/portals` | boards `live`; `broken` needs a look (re-check, or disable) |
| `/scans` | worker "Up"; each active search ran within its interval |
| `/followups` | nothing overdue that you meant to chase |
| Grafana "jobs track" (`ops/grafana/dashboards/jobs-track.json`) | same numbers, over time |

## When an alert fires

Each alert is one per episode: it repeats only after the thing recovered and failed again.

- **Source down: NAME** (3 failures in a row), including company boards. Open `/portals` or `/sources`, read the error. A 404 on a company board means the company left that ATS: disable the board. A 403 or 429 means stop polling it (see the zero-cost and compliance rules); do not work around it.
- **Board broken: NAME** (circuit open, or no success for 3 days, with fewer than 3 failures in a row). Same page; "Re-check" queues one poll.
- **Saved search failing: NAME** (last 3 runs errored). `/scans`, "history" for the error. The watermark did not move, so nothing is lost: the next good run reports what was missed. Fix, or pause or delete the search.
- **Worker silent**, **Stuck queue**, **Backup stale**: unchanged from before the Jobs track.

## Backup and restore

`scripts/backup.sh` (cron, 20:00 IST) dumps the whole database, so tracked jobs, history, follow-ups and saved searches are included. `scripts/restore_test.sh` (cron, Sundays) restores the newest dump into a throwaway cluster, checks every table's row count and every foreign key. Run it by hand after any migration. Back up before applying a migration: `scripts/backup.sh`, then `alembic upgrade head`.

## What retention and `forget` do to your records

Event text is scrubbed after the source's retention period, and `forget` clears a person's name on events. Neither touches `tracked_jobs`, `tracked_job_events` or `followup_log`: a tracked job keeps its own title, company, link and location. Saved-search run history is pruned at 90 days; the searches themselves stay.

## Switches

- Follow-ups off: `enabled: false` in `config/followups.yaml` (no restart). Hides the page, refuses writes, stops reminders.
- A saved search: pause it on `/scans`. A board or job board: Disable on `/portals`.
- Standing rules that stay as they are: `LLM_ENABLED=false`, `AUTO_SOURCE_CAP=0`, Jooble off.
