#!/usr/bin/env bash
# Dump the database. Retention: 7 daily + 4 weekly (Sundays). Override dir: BACKUP_DIR=...
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
umask 077  # dumps hold everything collected: owner only
DIR="${BACKUP_DIR:-$HOME/backups/startup-intel}"; mkdir -p "$DIR/daily" "$DIR/weekly"; chmod 700 "$DIR"
URL=$(grep '^DATABASE_URL=' "$ROOT/.env" | cut -d= -f2- | sed 's/+asyncpg//')
# pg_dump must match the server major version (the PATH default can be an older client; it refuses to dump a newer server)
PGBIN="/usr/lib/postgresql/$(psql "$URL" -Atc 'show server_version_num' | cut -c1-2)/bin"
TS=$(date -u +%Y%m%dT%H%M%SZ)
"$PGBIN/pg_dump" --format=custom "$URL" -f "$DIR/daily/db_$TS.dump"
# A dump that cannot be read back is not a backup: fail loudly (the watchdog then reports a stale backup).
"$PGBIN/pg_restore" --list "$DIR/daily/db_$TS.dump" > /dev/null || { rm -f "$DIR/daily/db_$TS.dump"; echo "Backup unreadable, removed" >&2; exit 1; }
if [ "$(date -u +%u)" = 7 ]; then cp "$DIR/daily/db_$TS.dump" "$DIR/weekly/"; fi
ls -1t "$DIR"/daily/*.dump | tail -n +8 | xargs -r rm --
{ ls -1t "$DIR"/weekly/*.dump 2>/dev/null || true; } | tail -n +5 | xargs -r rm --

# Credentials and configuration (.env, config/, migrations), AES-256 encrypted. The key lives outside the backup directory:
# keep a copy somewhere else (password manager), because without it this bundle cannot be opened.
KEY="${BACKUP_KEY_FILE:-$HOME/.config/startup-intel/backup.key}"
if [ ! -f "$KEY" ]; then
  mkdir -p "$(dirname "$KEY")"; (umask 077; openssl rand -base64 33 > "$KEY")
  echo "NEW backup key created at $KEY. Copy it somewhere safe NOW; encrypted bundles are unreadable without it." >&2
fi
mkdir -p "$DIR/config"
LEDGER="${DELETION_LEDGER:-$HOME/.config/startup-intel/deletions.jsonl}"; touch "$LEDGER"; chmod 600 "$LEDGER"  # deletion requests must survive a restore
tar -czf - -C "$ROOT" .env config backend/alembic/versions -C "$(dirname "$LEDGER")" "$(basename "$LEDGER")" \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$KEY" -out "$DIR/config/config_$TS.tar.gz.enc"
chmod 600 "$DIR/config/config_$TS.tar.gz.enc"
ls -1t "$DIR"/config/*.enc | tail -n +8 | xargs -r rm --
echo "Backup written: $DIR/daily/db_$TS.dump (+ encrypted config bundle)"
