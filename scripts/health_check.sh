#!/usr/bin/env bash
# Exit 0 only if backend, database and frontend are all healthy.
fail=0
chk() { if out=$(curl -fsS -m "${3:-5}" "$2" 2>&1); then echo "OK   $1"; else echo "FAIL $1 ($2)"; fail=1; fi; }
chk "backend live"  http://127.0.0.1:8000/health/live
chk "backend ready (db, extensions, migration)" http://127.0.0.1:8000/health/ready
chk "frontend"      http://127.0.0.1:3000/ 40
exit $fail
