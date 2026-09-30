"""One-off fix: give sessions that were burned by the bad API keys a fresh start.

Run from the backend/ folder, with the backend STOPPED and the keys already swapped in .env:

    python reset_failed_attempts.py

What it does
  * makes a timestamped backup copy of the database first
  * rows that are 'failed', or 'pending' with attempts already used, go back to
    'pending' with attempt_count = 0 (and no last_attempt_at)
  * NEVER touches 'classified' rows, sensitive rows, or any category already set
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import settings

db_path = Path(settings.db_path)
if not db_path.exists():
    sys.exit(f"Database not found: {db_path}")

# 1) backup (uses SQLite's own backup API, safe even if WAL files exist)
backup = db_path.with_name(f"{db_path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{db_path.suffix}")
src = sqlite3.connect(str(db_path), timeout=30)
dst = sqlite3.connect(str(backup))
src.backup(dst)
dst.close()
print(f"Backup written: {backup}")

# 2) reset
WHERE = """
    sensitive = 0
    AND category IS NULL
    AND (classification_status = 'failed'
         OR (classification_status = 'pending' AND attempt_count > 0))
"""
before = src.execute(
    "SELECT source, classification_status, COUNT(*) FROM activity_sessions "
    "GROUP BY source, classification_status"
).fetchall()

with src:  # one transaction
    n = src.execute(
        f"UPDATE activity_sessions SET classification_status='pending', "
        f"attempt_count=0, last_attempt_at=NULL WHERE {WHERE}"
    ).rowcount

after = src.execute(
    "SELECT source, classification_status, COUNT(*) FROM activity_sessions "
    "GROUP BY source, classification_status"
).fetchall()
src.close()

print(f"\nRows reset to pending: {n}")
print("Before:", before)
print("After: ", after)
