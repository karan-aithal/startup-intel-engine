#!/usr/bin/env bash
# Prove a backup restores: load the newest dump (or the file given) into a throwaway Postgres cluster built in a temp directory,
# then check every table has exactly the rows the dump holds. Never touches the live database. Usage: scripts/restore_test.sh [file.dump]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${BACKUP_DIR:-$HOME/backups/startup-intel}"
DUMP="${1:-$(ls -1t "$DIR"/daily/*.dump | head -1)}"; [ -f "$DUMP" ] || { echo "No dump found"; exit 1; }
URL=$(grep '^DATABASE_URL=' "$ROOT/.env" | cut -d= -f2- | sed 's/+asyncpg//')
PGBIN="/usr/lib/postgresql/$(psql "$URL" -Atc 'show server_version_num' | cut -c1-2)/bin"
T=$(mktemp -d); PORT="${RESTORE_TEST_PORT:-5544}"
trap '"$PGBIN/pg_ctl" -D "$T/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$T"' EXIT
"$PGBIN/initdb" -D "$T/data" -U postgres -A trust -E UTF8 >/dev/null
"$PGBIN/pg_ctl" -D "$T/data" -o "-p $PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$T" -l "$T/pg.log" -w start >/dev/null
P=("$PGBIN/psql" -h 127.0.0.1 -p "$PORT" -U postgres -qAt)
"${P[@]}" -c 'create database restore_check' >/dev/null

# --exit-on-error: any failed statement (a missing extension, a broken foreign key) fails the test.
"$PGBIN/pg_restore" --exit-on-error --no-owner --no-privileges -h 127.0.0.1 -p "$PORT" -U postgres -d restore_check "$DUMP"

# Rows the dump holds (read from its COPY blocks) versus rows the restored database holds. Empty tables are skipped on both sides.
"$PGBIN/pg_restore" --data-only -f - "$DUMP" \
  | awk '/^COPY /{t=$2; sub(/^public\./,"",t); n=0; in_copy=1; next} /^\\\.$/{if(in_copy&&n>0)print t, n; in_copy=0; next} in_copy{n++}' | sort > "$T/dump.counts"
"${P[@]}" -d restore_check -F ' ' -c "select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name), false, true, '')))[1]::text::int
  from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'" | awk '$2>0' | sort > "$T/restored.counts"
if ! diff "$T/dump.counts" "$T/restored.counts"; then echo "FAIL: restored row counts differ from the dump (diff above)"; exit 1; fi

# Schema state and constraints: every foreign key must validate, and the migration revision must be present.
BAD=$("${P[@]}" -d restore_check -c "select count(*) from pg_constraint where contype = 'f' and not convalidated")
REV=$("${P[@]}" -d restore_check -c "select version_num from alembic_version")
[ "$BAD" = 0 ] || { echo "FAIL: $BAD unvalidated foreign keys"; exit 1; }
echo "OK  restored $(basename "$DUMP") into a clean cluster: $(wc -l < "$T/restored.counts") tables with rows, $(awk '{s+=$2} END {print s}' "$T/restored.counts") rows, migration $REV, all foreign keys valid"
