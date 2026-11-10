#!/usr/bin/env bash
# Dump the database. Retention: 7 daily + 4 weekly (Sundays). Override dir: BACKUP_DIR=...
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${BACKUP_DIR:-$HOME/backups/startup-intel}"; mkdir -p "$DIR/daily" "$DIR/weekly"
URL=$(grep '^DATABASE_URL=' "$ROOT/.env" | cut -d= -f2- | sed 's/+asyncpg//')
TS=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump --format=custom "$URL" -f "$DIR/daily/db_$TS.dump"
if [ "$(date -u +%u)" = 7 ]; then cp "$DIR/daily/db_$TS.dump" "$DIR/weekly/"; fi
ls -1t "$DIR"/daily/*.dump | tail -n +8 | xargs -r rm --
{ ls -1t "$DIR"/weekly/*.dump 2>/dev/null || true; } | tail -n +5 | xargs -r rm --
echo "Backup written: $DIR/daily/db_$TS.dump"
