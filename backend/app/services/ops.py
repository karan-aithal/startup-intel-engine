"""Operational visibility, derived from the database so it survives restarts and covers every process.

`metrics_text` renders Prometheus text on scrape (no in-process counters to lose). `problems` lists what needs the owner's
attention; `raise_alerts` queues each through the outbox, so a failed notification retries like any other alert.
A problem's key identifies an episode (it changes only after recovery), so a persistent failure alerts once, not every pass.
"""
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.services import outbox
from app.services.registry import FAILURES_UNAVAILABLE

SRC = "r.name AS source, r.connector_type AS route"
RUNS = "FROM source_runs u JOIN source_registry r ON r.id = u.source_id"
EVT = "FROM source_events e JOIN source_registry r ON r.id = e.source_id"

# (name, type, help, sql). Every column but the last is a label; the last (v) is the value. NULL values are skipped.
METRICS = [
    ("source_last_success_timestamp", "gauge", "Unix time of the last successful poll",
     f"SELECT {SRC}, extract(epoch from r.last_success_at) AS v FROM source_registry r WHERE r.enabled"),
    ("source_detection_latency_seconds", "gauge", "Median seconds from publication to our detection, items published in the last 7 days, non-job events, first poll excluded",
     f"SELECT {SRC}, percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from e.detected_at - e.published_at)) AS v {EVT} "
     + "WHERE e.detected_at > now() - interval '7 days' AND e.published_at <= e.detected_at AND e.published_at > e.detected_at - interval '7 days' "
     + "AND e.detected_at > (SELECT min(started_at) FROM source_runs WHERE source_id = r.id) + interval '1 hour' "
     + "AND e.event_type <> 'job' GROUP BY 1, 2"),
    ("source_http_errors_total", "counter", "Failed polls (any error, not only HTTP status)",
     f"SELECT {SRC}, count(*) AS v {RUNS} WHERE u.status = 'error' GROUP BY 1, 2"),
    ("source_rate_limit_total", "counter", "Polls deferred by a rate limit or quota",
     f"SELECT {SRC}, count(*) AS v {RUNS} WHERE u.status = 'skipped' GROUP BY 1, 2"),
    ("source_access_denied_total", "counter", "Polls refused with 401 or 403",
     f"SELECT {SRC}, count(*) AS v {RUNS} WHERE u.status = 'error' AND u.error ~ '\\m(401|403)\\M' GROUP BY 1, 2"),
    ("source_events_received_total", "counter", "Events stored per source",
     f"SELECT {SRC}, count(*) AS v {EVT} GROUP BY 1, 2"),
    ("source_events_relevant_total", "counter", "Events classified funding or hiring",
     f"SELECT {SRC}, count(*) AS v {EVT} WHERE e.processing_status IN ('funding', 'hiring') GROUP BY 1, 2"),
    ("source_events_deduplicated_total", "counter", "Fetched items dropped as already stored",
     f"SELECT {SRC}, sum(u.items_fetched - u.items_new) AS v {RUNS} WHERE u.status = 'ok' GROUP BY 1, 2"),
    ("source_health", "gauge", "Routes per health state",
     "SELECT health_status AS state, count(*) AS v FROM source_registry GROUP BY 1"),
    ("company_discovered_total", "counter", "Companies known", "SELECT count(*) AS v FROM companies"),
    ("ats_boards_discovered_total", "counter", "ATS boards known", "SELECT count(*) AS v FROM ats_boards"),
    ("llm_extraction_failures_total", "counter", "Model calls that did not return a valid result",
     "SELECT outcome, count(*) AS v FROM llm_attempts WHERE outcome <> 'ok' GROUP BY 1"),
    ("job_queue_pending", "gauge", "Jobs waiting, by kind",
     "SELECT kind, count(*) AS v FROM job_queue WHERE status = 'pending' GROUP BY 1"),
    ("job_queue_failed", "gauge", "Dead-lettered jobs, by kind",
     "SELECT kind, count(*) AS v FROM job_queue WHERE status IN ('failed', 'dead') GROUP BY 1"),
    ("telegram_delivery_failures_total", "counter", "Alerts that failed at least once",
     "SELECT count(*) AS v FROM alert_deliveries WHERE last_error IS NOT NULL"),
    ("alert_deliveries", "gauge", "Alerts by delivery state", "SELECT status, count(*) AS v FROM alert_deliveries GROUP BY 1"),
    ("funding_verified_total", "counter", "Funding rounds with verified status",
     "SELECT count(*) AS v FROM funding_rounds WHERE verification_status = 'VERIFIED'"),
    ("jobs_discovered_total", "counter", "Job events stored", "SELECT count(*) AS v FROM source_events WHERE event_type = 'job'"),
    ("worker_heartbeat_age_seconds", "gauge", "Seconds since the newest worker heartbeat",
     "SELECT extract(epoch from now() - max(last_seen_at)) AS v FROM worker_heartbeats"),
]


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def metrics_text(s) -> str:
    out = []
    for name, kind, help_, sql in METRICS:
        out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for row in (await s.execute(text(sql))).mappings():
            labels = ",".join(f'{k}="{_esc(v)}"' for k, v in row.items() if k != "v")
            if row["v"] is not None:
                out.append(f"{name}{{{labels}}} {float(row['v'])}" if labels else f"{name} {float(row['v'])}")
    return "\n".join(out) + "\n"


def _epoch(d) -> int:
    return int(d.timestamp()) if d else 0


async def problems(s, *, llm_enabled: bool = False, backup_dir: Path | None = None, now: datetime | None = None) -> list[tuple[str, str]]:
    """(episode key, message) for each current problem. Thresholds are deliberately conservative: alert after repeats, not blips."""
    now, out = now or datetime.now(UTC), []

    # 1. A source that keeps failing. The episode id is its last success, so it alerts once until it recovers.
    for r in (await s.execute(text("""SELECT id, name, connector_type, consecutive_failures, last_success_at, last_error FROM source_registry
            WHERE enabled AND consecutive_failures >= :n"""), {"n": FAILURES_UNAVAILABLE})).all():
        last = f"last success {r.last_success_at:%Y-%m-%d %H:%M} UTC" if r.last_success_at else "never succeeded"
        out.append((f"source_down:{r.id}:{_epoch(r.last_success_at)}",
                    f"Source down: {r.name} ({r.connector_type}), {r.consecutive_failures} failures in a row, {last}. {(r.last_error or '').splitlines()[0][:150] if r.last_error else ''}"))

    # 2. Silent failure: reachable and answering "ok", but returning nothing after a week of returning items.
    for r in (await s.execute(text("""SELECT r.id, r.name, r.connector_type,
            max(u.started_at) FILTER (WHERE u.items_fetched > 0) AS last_items
        FROM source_runs u JOIN source_registry r ON r.id = u.source_id
        WHERE r.enabled AND r.connector_type NOT IN ('stream', 'discovery') AND u.status = 'ok' AND u.started_at > now() - interval '8 days'
        GROUP BY r.id, r.name, r.connector_type
        HAVING count(*) FILTER (WHERE u.started_at > now() - interval '24 hours') >= 3
           AND coalesce(sum(u.items_fetched) FILTER (WHERE u.started_at > now() - interval '24 hours'), 0) = 0
           AND coalesce(sum(u.items_fetched) FILTER (WHERE u.started_at <= now() - interval '24 hours'), 0) >= 5"""))).all():
        out.append((f"source_silent:{r.id}:{_epoch(r.last_items)}",
                    f"Silent source: {r.name} ({r.connector_type}) answers but has returned no items for 24 h (last items {r.last_items:%Y-%m-%d %H:%M} UTC)."))

    # 3. Jobs that ran out of retries in the last day, and jobs stuck waiting (model jobs are excluded while the model is off).
    skip = [] if llm_enabled else ["llm_enrich"]
    for r in (await s.execute(text("""SELECT kind, count(*) AS n FROM job_queue
            WHERE status = 'dead' AND finished_at > now() - interval '24 hours' AND kind <> ALL(CAST(:skip AS text[])) GROUP BY kind"""), {"skip": skip})).all():
        out.append((f"queue_dead:{r.kind}:{now:%Y-%m-%d}", f"Dead jobs: {r.n} '{r.kind}' job(s) exhausted their retries in the last 24 h."))
    for r in (await s.execute(text("""SELECT kind, count(*) AS n FROM job_queue
            WHERE status = 'pending' AND run_at < now() - interval '30 minutes' AND kind <> ALL(CAST(:skip AS text[])) GROUP BY kind"""), {"skip": skip})).all():
        out.append((f"queue_stuck:{r.kind}:{now:%Y-%m-%d}", f"Stuck queue: {r.n} '{r.kind}' job(s) pending for over 30 minutes (is the worker running?)."))

    # 4. Notifications that gave up, or are not being delivered.
    n = (await s.execute(text("SELECT count(*) FROM alert_deliveries WHERE status = 'dead' AND next_attempt_at > now() - interval '24 hours'"))).scalar_one()
    if n:
        out.append((f"delivery_dead:{now:%Y-%m-%d}", f"Alerts undeliverable: {n} alert(s) failed all retries in the last 24 h."))
    n = (await s.execute(text("""SELECT count(*) FROM alert_deliveries WHERE status IN ('pending', 'failed')
            AND next_attempt_at < now() - interval '15 minutes' AND topic IS DISTINCT FROM 'Ops'"""))).scalar_one()
    if n:
        out.append((f"delivery_stuck:{now:%Y-%m-%d}", f"Alert backlog: {n} alert(s) due for over 15 minutes and still unsent."))

    # 5. The worker itself (only a process outside the worker can raise this).
    last = (await s.execute(text("SELECT max(last_seen_at) FROM worker_heartbeats"))).scalar_one()
    if last and (now - last).total_seconds() > 180:
        out.append((f"worker_stale:{_epoch(last)}", f"Worker silent: no heartbeat since {last:%Y-%m-%d %H:%M} UTC."))

    # 6. Backups.
    if backup_dir is not None:
        files = list((backup_dir / "daily").glob("db_*.dump"))
        newest = max((f.stat().st_mtime for f in files), default=0)
        if now.timestamp() - newest > 36 * 3600:
            out.append((f"backup_stale:{now:%Y-%m-%d}", "Backup stale: no database backup in the last 36 h."
                        if files else "Backup missing: no database backup found."))
    return out


async def raise_alerts(s, found: list[tuple[str, str]], now: datetime | None = None) -> int:
    """Queue each problem as an ops alert. Returns how many were new (an existing key is not queued twice)."""
    now, new = now or datetime.now(UTC), 0
    for key, msg in found:
        payload = {"label": "ops", "title": " ".join(msg.split()), "source": "watchdog", "detected_at": now.isoformat()}
        if await outbox.add_alert(s, key, payload, topic="Ops"):
            new += 1
    return new


async def watch(sm, cfg) -> int:
    """One failure-alerting pass: find problems, queue the new ones. Used by the worker loop and by the watchdog CLI."""
    async with sm() as s:
        found = await problems(s, llm_enabled=cfg.llm_enabled, backup_dir=Path(cfg.backup_dir).expanduser())
        new = await raise_alerts(s, found)
        await s.commit()
    return new
