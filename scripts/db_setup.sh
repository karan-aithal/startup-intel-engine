#!/usr/bin/env bash
# One-time DB setup on the existing PostgreSQL 18. Run: sudo bash scripts/db_setup.sh
# Creates role + database, enables pg_trgm and pgvector, writes .env if missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${SUDO_USER:-$USER}"
DB=startup_intel; ROLE=startup_intel
PSQL="sudo -u postgres psql -p 5432 -v ON_ERROR_STOP=1 -X -q"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash $0"; exit 1; fi
if ! ls /usr/share/postgresql/18/extension/vector.control >/dev/null 2>&1; then
  echo "Installing pgvector for PG18..."; apt-get install -y postgresql-18-pgvector
fi
PASS="$(openssl rand -hex 16)"
if sudo -u postgres psql -Atc "select 1 from pg_roles where rolname='$ROLE'" | grep -q 1; then
  echo "Role $ROLE exists; keeping its password."; PASS=""
else
  $PSQL -c "CREATE ROLE $ROLE LOGIN PASSWORD '$PASS'"
fi
sudo -u postgres psql -Atc "select 1 from pg_database where datname='$DB'" | grep -q 1 \
  || $PSQL -c "CREATE DATABASE $DB OWNER $ROLE"
$PSQL -d $DB -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;"
if [ -n "$PASS" ] && [ ! -f "$ROOT/.env" ]; then
  sed "s/CHANGE_ME/$PASS/" "$ROOT/.env.example" > "$ROOT/.env"
  chown "$OWNER":"$OWNER" "$ROOT/.env"; chmod 600 "$ROOT/.env"
  echo ".env written."
elif [ -n "$PASS" ]; then
  echo ".env already exists; set this password manually in DATABASE_URL: $PASS"
fi
echo "Done: database $DB ready (pg_trgm + vector enabled)."
