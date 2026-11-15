#!/usr/bin/env bash
# One-time DB setup on a local PostgreSQL cluster (version auto-detected). Run: sudo bash scripts/db_setup.sh
# Port defaults to 5432; for a fresh PG18 cluster: sudo PGPORT_TARGET=5433 bash scripts/db_setup.sh (after scripts/pg18_cluster.sh).
# Creates role + database, enables pg_trgm and pgvector, writes .env if missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${SUDO_USER:-$USER}"
DB=startup_intel; ROLE=startup_intel; PORT="${PGPORT_TARGET:-5432}"
PSQL="sudo -u postgres psql -p $PORT -v ON_ERROR_STOP=1 -X -q"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash $0"; exit 1; fi
PGV="$(sudo -u postgres psql -p $PORT -Atc "show server_version_num" | cut -c1-2)"  # 140xxx -> 14, 180xxx -> 18
if ! ls "/usr/share/postgresql/$PGV/extension/vector.control" >/dev/null 2>&1; then
  echo "Installing pgvector for PG$PGV..."
  # Ubuntu repos lack pgvector; add the official PGDG apt repo (idempotent) if the package isn't found.
  apt-cache show "postgresql-$PGV-pgvector" >/dev/null 2>&1 \
    || { /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y; apt-get update -qq; }
  apt-get install -y "postgresql-$PGV-pgvector"
fi
PASS="$(openssl rand -hex 16)"
if sudo -u postgres psql -p $PORT -Atc "select 1 from pg_roles where rolname='$ROLE'" | grep -q 1; then
  echo "Role $ROLE exists; keeping its password."; PASS=""
else
  $PSQL -c "CREATE ROLE $ROLE LOGIN PASSWORD '$PASS'"
fi
sudo -u postgres psql -p $PORT -Atc "select 1 from pg_database where datname='$DB'" | grep -q 1 \
  || $PSQL -c "CREATE DATABASE $DB OWNER $ROLE"
$PSQL -d $DB -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;"
URL="postgresql+asyncpg://$ROLE:$PASS@127.0.0.1:$PORT/$DB"
if [ -n "$PASS" ] && [ ! -f "$ROOT/.env" ]; then
  sed "s|^DATABASE_URL=.*|DATABASE_URL=$URL|" "$ROOT/.env.example" > "$ROOT/.env"
  chown "$OWNER":"$OWNER" "$ROOT/.env"; chmod 600 "$ROOT/.env"
  echo ".env written."
elif [ -n "$PASS" ]; then  # existing .env: back it up and point DATABASE_URL at this cluster
  cp -p "$ROOT/.env" "$ROOT/.env.bak"
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$URL|" "$ROOT/.env"
  echo ".env DATABASE_URL updated for port $PORT (previous file saved as .env.bak)."
fi
echo "Done: database $DB ready (pg_trgm + vector enabled)."
