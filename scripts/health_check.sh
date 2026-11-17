#!/usr/bin/env bash
# Exit 0 only if backend, database and frontend are all healthy.
fail=0
chk() { if out=$(curl -fsS -m "${3:-5}" "$2" 2>&1); then echo "OK   $1"; else echo "FAIL $1 ($2)"; fail=1; fi; }
chk "backend live"  http://127.0.0.1:8000/health/live
chk "backend ready (db, extensions, migration)" http://127.0.0.1:8000/health/ready
chk "metrics"       http://127.0.0.1:8000/metrics
# The worker is a separate process: it is alive only if its heartbeat (written every 30 s) is recent.
age=$(curl -fsS -m 5 http://127.0.0.1:8000/metrics 2>/dev/null | awk '/^worker_heartbeat_age_seconds /{print int($2)}')
if [ -n "$age" ] && [ "$age" -lt 180 ]; then echo "OK   worker heartbeat (${age}s ago)"; else echo "FAIL worker heartbeat (${age:-none})"; fail=1; fi
chk "frontend"      http://127.0.0.1:3010/ 40
exit $fail
