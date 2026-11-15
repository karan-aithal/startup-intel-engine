#!/usr/bin/env bash
# Create a fresh PostgreSQL 18 cluster on :5433 next to the existing one (which is left untouched).
# Run: sudo bash scripts/pg18_cluster.sh && sudo PGPORT_TARGET=5433 bash scripts/db_setup.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0"; exit 1; }
PORT=5433
dpkg -s postgresql-18 >/dev/null 2>&1 || apt-get install -y postgresql-18
apt-get install -y postgresql-18-pgvector
if pg_lsclusters -h | awk '{print $1}' | grep -qx 18; then
  echo "A PG18 cluster already exists:"; pg_lsclusters
else
  pg_createcluster 18 main --port "$PORT" --start
fi
pg_lsclusters
