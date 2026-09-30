"""Read-only snapshot of classification state. Run from backend/:  python check_state.py"""
import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from config import settings

c = sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True)  # safe for paths with spaces
now = datetime.now(timezone.utc)
print("now (UTC):", now.strftime("%Y-%m-%d %H:%M:%S"))

row = c.execute("SELECT value FROM app_state WHERE key='gemini_pause'").fetchone()
if row:
    p = json.loads(row[0]); p.pop("fp", None)
    print("GEMINI PAUSE:", p)
else:
    print("GEMINI PAUSE: none")

print("\nsource / status / attempts -> rows, unique pages")
for r in c.execute("SELECT source, classification_status, attempt_count, COUNT(*), COUNT(DISTINCT signature) "
                   "FROM activity_sessions GROUP BY 1,2,3 ORDER BY 1,2,3"):
    print("  ", r)

print("\nlast attempt on any row:", c.execute("SELECT MAX(last_attempt_at) FROM activity_sessions").fetchone()[0])
print("cache rows (confident AI results):", c.execute("SELECT COUNT(*) FROM classification_cache").fetchone()[0])
print("classified by:", c.execute("SELECT classified_by, COUNT(*) FROM activity_sessions "
                                  "WHERE classification_status='classified' GROUP BY 1").fetchall())
