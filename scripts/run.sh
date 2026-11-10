#!/usr/bin/env bash
# Start backend (8000) and frontend (3000), localhost only. Ctrl+C stops both.
set -euo pipefail
unset PYTHONPATH  # ROS on this machine leaks system packages into the venv
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$ROOT/logs"
(cd "$ROOT/backend" && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000) > "$ROOT/logs/backend.log" 2>&1 &
B=$!
(cd "$ROOT/frontend" && npm run dev -- -H 127.0.0.1 -p 3000) > "$ROOT/logs/frontend.log" 2>&1 &
F=$!
trap 'kill $B $F 2>/dev/null; exit 0' INT TERM
echo "backend pid $B, frontend pid $F. Logs in $ROOT/logs. Ctrl+C to stop."
wait
