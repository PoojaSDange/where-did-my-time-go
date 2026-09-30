#!/usr/bin/env bash
# Usage: run_frontend_tests.sh [frontend_dom.js | frontend_local.js]
#  frontend_dom.js   -> throw-away DEMO-mode backend (synthetic data only)
#  frontend_local.js -> throw-away LOCAL-mode backend with two FAKE Chrome profiles under a fake $HOME
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8765}"
SCRIPT="${1:-frontend_dom.js}"
TMP="$(mktemp -d)"
cd "$HERE/../backend"
if [[ "$SCRIPT" == *local* ]]; then
  python3 - "$TMP" <<'PY'
import sqlite3, sys, json, time, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from services import timeutil
tmp = sys.argv[1]; now = datetime.now(timezone.utc).replace(microsecond=0)
ud = f"{tmp}/home/.config/google-chrome"; info = {}
for name, dom, who, email in (("Default", "default-only.io", "Personal", "me@example.com"), ("Profile 2", "github.com", "Work", "work@example.com")):
    os.makedirs(f"{ud}/{name}")
    c = sqlite3.connect(f"{ud}/{name}/History")
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INT, last_visit_time INT)")
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, visit_duration INTEGER, transition INTEGER)")
    c.execute("INSERT INTO urls VALUES (1, ?, 'a page', 1, 0)", (f"https://{dom}/x",))
    for i in range(20):
        t = now - timedelta(days=2) + timedelta(minutes=i * 9)
        c.execute("INSERT INTO visits(url,visit_time,visit_duration,transition) VALUES (1,?,?,0)", (timeutil.dt_to_chrome_us(t), 120_000_000))
    c.commit(); c.close()
    info[name] = {"name": who, "user_name": email}
json.dump({"profile": {"info_cache": info}}, open(f"{ud}/Local State", "w"))
PY
  export HOME="$TMP/home" HISTORY_DAYS=10
  unset APP_MODE CHROME_HISTORY_PATH
  DB_PATH="$TMP/w.db" DATA_DIR="$TMP/data" python3 -m uvicorn main:app --port "$PORT" >"$TMP/server.log" 2>&1 &
else
  APP_MODE=demo DB_PATH="$TMP/demo.db" DATA_DIR="$TMP/data" python3 -m uvicorn main:app --port "$PORT" >"$TMP/server.log" 2>&1 &
fi
SRV=$!
trap 'kill -9 $SRV 2>/dev/null; rm -rf "$TMP"' EXIT
for i in $(seq 1 40); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.25; done
cd "$HERE" && [ -d node_modules ] || npm install --silent
export WDMT_TOKEN="$(cat "$TMP/data/api_token.txt" 2>/dev/null || true)"
timeout 150 node "$SCRIPT" "http://127.0.0.1:$PORT"
