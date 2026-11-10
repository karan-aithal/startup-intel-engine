#!/usr/bin/env bash
# Install deps, apply migrations. Safe to re-run.
set -euo pipefail
unset PYTHONPATH  # ROS on this machine leaks system packages into the venv
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -f "$ROOT/.env" ] || { echo "Missing .env - run: sudo bash scripts/db_setup.sh"; exit 1; }
(cd "$ROOT/backend" && uv sync && uv run alembic upgrade head)
(cd "$ROOT/frontend" && npm install)
echo "Bootstrap complete. Start with: scripts/run.sh"
