#!/usr/bin/env bash
# Install (or --remove) this project's scheduled jobs in your crontab. Safe to re-run: it replaces its own block only.
#   20:00 IST daily   backup (7 daily + 4 weekly, plus the encrypted config bundle)
#   20:30 IST Sunday  restore test into a throwaway cluster
#   every 5 minutes   watchdog (alerts for failed sources, stuck queues, a dead worker or a stale backup; works when the worker is down)
#   00:15 IST daily   log rotation
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BEGIN="# >>> startup-intel (managed by install_cron.sh)"; END="# <<< startup-intel"
CUR="$(crontab -l 2>/dev/null || true)"
KEEP="$(printf '%s\n' "$CUR" | sed "/^$BEGIN\$/,/^$END\$/d")"
if [ "${1:-}" = "--remove" ]; then printf '%s\n' "$KEEP" | crontab -; echo "Removed."; exit 0; fi

STATE="$HOME/.local/state/startup-intel"; mkdir -p "$STATE" "$ROOT/logs"
sed "s#@ROOT@#$ROOT#" "$ROOT/ops/logrotate.conf" > "$STATE/logrotate.conf"
BLOCK="$BEGIN
CRON_TZ=Asia/Kolkata
PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
0 20 * * * $ROOT/scripts/backup.sh >> $ROOT/logs/backup.log 2>&1
30 20 * * 0 $ROOT/scripts/restore_test.sh >> $ROOT/logs/backup.log 2>&1
*/5 * * * * cd $ROOT/backend && env -u PYTHONPATH uv run python -m app.watchdog >> $ROOT/logs/watchdog.log 2>&1
15 0 * * * logrotate -s $STATE/logrotate.state $STATE/logrotate.conf
$END"
printf '%s\n%s\n' "$KEEP" "$BLOCK" | crontab -
echo "Installed. View with: crontab -l"
