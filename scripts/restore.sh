#!/usr/bin/env bash
# Restore a dump into the database (replaces existing objects). Usage: scripts/restore.sh <file.dump>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "${1:-}" ] || { echo "Usage: $0 <file.dump>"; exit 1; }
URL=$(grep '^DATABASE_URL=' "$ROOT/.env" | cut -d= -f2- | sed 's/+asyncpg//')
PGBIN="/usr/lib/postgresql/$(psql "$URL" -Atc 'show server_version_num' | cut -c1-2)/bin"  # match the server major version
read -rp "This overwrites current data. Type YES: " a; [ "$a" = YES ] || exit 1
"$PGBIN/pg_restore" --clean --if-exists --no-owner -d "$URL" "$1"
# A backup can hold data that live retention has since removed (up to 4 weeks of it): apply the retention rules again.
(cd "$ROOT/backend" && env -u PYTHONPATH "$HOME/.local/bin/uv" run python -m app.retention purge)
echo "Restore complete. Deletion requests made since this backup (python -m app.retention forget ...) must be repeated."
