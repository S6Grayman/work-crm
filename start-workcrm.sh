#!/usr/bin/env bash
# Double-click or: ./start-workcrm.sh
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Install it, then try again."
  exit 1
fi
(sleep 1; xdg-open "http://127.0.0.1:8765" >/dev/null 2>&1 || true) &
echo "WorkCRM  http://127.0.0.1:8765"
echo "Leave this window open. Close it to stop."
exec python3 crm.py
