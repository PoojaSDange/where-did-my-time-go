"""ALL database access lives behind this module (plus the services that call it).

SQLite, one short-lived connection per unit of work, WAL mode so the API, the
classification worker and the catch-up job can run concurrently.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from config import settings

log = logging.getLogger("wdmt.db")

_CATS = (
    "'focused_work','learning','research','communication','social_media','entertainment',"
    "'shopping','news','productivity','personal','creative','break','ambiguous'"
)

# --------------------------------------------------------------------------
# Migrations: ordered, append-only. Never edit an applied migration; add a new one.
# --------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        f"""
        CREATE TABLE activity_sessions (
            id                    TEXT PRIMARY KEY,               -- client-generated, unique
            start_time            TEXT NOT NULL,                  -- UTC ISO
            end_time              TEXT NOT NULL,                  -- UTC ISO
            domain                TEXT NOT NULL,
            url                   TEXT,                           -- normalized: no query/fragment
            title                 TEXT,
            category              TEXT CHECK (category IS NULL OR category IN ({_CATS})),
            duration              INTEGER NOT NULL CHECK (duration >= 0),  -- seconds
            is_wasted             INTEGER NOT NULL DEFAULT 0,
            confidence            REAL,
            reason                TEXT,
            source                TEXT NOT NULL CHECK (source IN ('history_estimated','extension_measured')),
            classification_status TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (classification_status IN ('pending','classified','failed')),
            activity_key          TEXT NOT NULL UNIQUE,           -- idempotency / dedupe key
            attempt_count         INTEGER NOT NULL DEFAULT 0,
            last_attempt_at       TEXT,
            signature             TEXT,                           -- hash(domain+path+title)
            sensitive             INTEGER NOT NULL DEFAULT 0,     -- never sent to an LLM
            classified_by         TEXT,                           -- rule|cache|llm|override|sensitive
            created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX idx_act_start        ON activity_sessions (start_time);
        CREATE INDEX idx_act_end          ON activity_sessions (end_time);
        CREATE INDEX idx_act_source_start ON activity_sessions (source, start_time);
        CREATE INDEX idx_act_status       ON activity_sessions (classification_status, source);
        CREATE INDEX idx_act_domain       ON activity_sessions (domain);
        CREATE INDEX idx_act_signature    ON activity_sessions (signature);

        CREATE TABLE app_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        CREATE TABLE classification_cache (
            signature   TEXT PRIMARY KEY,
            domain      TEXT NOT NULL,
            path        TEXT,
            title       TEXT,
            category    TEXT NOT NULL CHECK (category IN ({_CATS})),
            confidence  REAL NOT NULL,
            reason      TEXT,
            origin      TEXT,                                     -- llm | rule
            hits        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        CREATE TABLE user_overrides (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            match_type  TEXT NOT NULL CHECK (match_type IN ('signature','domain')),
            match_value TEXT NOT NULL,
            category    TEXT NOT NULL CHECK (category IN ({_CATS})),
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE (match_type, match_value)
        );

        CREATE TABLE daily_summaries (
            day                 TEXT PRIMARY KEY,                 -- local date YYYY-MM-DD
            timezone            TEXT NOT NULL,
            total_seconds       INTEGER NOT NULL DEFAULT 0,
            wasted_seconds      INTEGER NOT NULL DEFAULT 0,
            estimated_seconds   INTEGER NOT NULL DEFAULT 0,       -- history_estimated
            measured_seconds    INTEGER NOT NULL DEFAULT 0,       -- extension_measured
            unclassified_seconds INTEGER NOT NULL DEFAULT 0,      -- pending + failed
            category_seconds    TEXT NOT NULL DEFAULT '{{}}',     -- JSON map category->seconds
            by_source           TEXT NOT NULL DEFAULT '{{}}',     -- JSON map source->(category->s)
            top_categories      TEXT NOT NULL DEFAULT '[]',
            top_distractions    TEXT NOT NULL DEFAULT '[]',
            coverage            TEXT NOT NULL DEFAULT '{{}}',     -- sessions/pending/failed counts, pct
            has_measured        INTEGER NOT NULL DEFAULT 0,
            ai_analysis         TEXT,                             -- JSON narrative
            ai_model            TEXT,
            analysis_status     TEXT NOT NULL DEFAULT 'in_progress',
            partial_observations TEXT NOT NULL DEFAULT '[]',      -- resumable batch observations
            computed_at         TEXT,
            analyzed_at         TEXT
        );

        CREATE TABLE monthly_summaries (
            month               TEXT PRIMARY KEY,                 -- YYYY-MM (local)
            timezone            TEXT NOT NULL,
            days_count          INTEGER NOT NULL DEFAULT 0,
            total_seconds       INTEGER NOT NULL DEFAULT 0,
            wasted_seconds      INTEGER NOT NULL DEFAULT 0,
            estimated_seconds   INTEGER NOT NULL DEFAULT 0,
            measured_seconds    INTEGER NOT NULL DEFAULT 0,
            unclassified_seconds INTEGER NOT NULL DEFAULT 0,
            category_seconds    TEXT NOT NULL DEFAULT '{{}}',
            by_source           TEXT NOT NULL DEFAULT '{{}}',
            top_categories      TEXT NOT NULL DEFAULT '[]',
            top_distractions    TEXT NOT NULL DEFAULT '[]',
            coverage            TEXT NOT NULL DEFAULT '{{}}',
            ai_analysis         TEXT,
            ai_model            TEXT,
            analysis_status     TEXT NOT NULL DEFAULT 'in_progress',
            computed_at         TEXT,
            analyzed_at         TEXT
        );
        """,
    ),
]

_JSON_COLUMNS = {
    "category_seconds", "by_source", "top_categories", "top_distractions",
    "coverage", "ai_analysis", "partial_observations",
}


def _db_path() -> str:
    return settings.db_path


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    """New connection (caller closes it). Row factory = sqlite3.Row."""
    p = path or _db_path()
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)  # autocommit; we manage BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Connection with one transaction: commit on success, rollback on error."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def init_db() -> int:
    """Create/upgrade the schema. Safe to call on every start. Returns schema version."""
    conn = get_connection()
    try:
        ver = current_version(conn)
        for v, sql in MIGRATIONS:
            if v <= ver:
                continue
            log.info("applying migration %s", v)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (v,),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            ver = v
        return ver
    finally:
        conn.close()


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


# --------------------------------------------------------------------------
# app_state (key/value)
# --------------------------------------------------------------------------
def get_state(key: str, default: Any = None, *, conn: Optional[sqlite3.Connection] = None) -> Any:
    def _q(c: sqlite3.Connection) -> Any:
        row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    if conn is not None:
        return _q(conn)
    with read_connection() as c:
        return _q(c)


def set_state(key: str, value: Any, *, conn: Optional[sqlite3.Connection] = None) -> None:
    payload = json.dumps(value)
    sql = (
        "INSERT INTO app_state(key, value, updated_at) VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
    )
    if conn is not None:
        conn.execute(sql, (key, payload))
        return
    with connection() as c:
        c.execute(sql, (key, payload))


def set_state_if_absent(key: str, value: Any) -> bool:
    """Atomically set; returns True only if this call created the key."""
    with connection() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO app_state(key, value) VALUES (?,?)", (key, json.dumps(value))
        )
        return cur.rowcount == 1


def delete_state(key: str) -> None:
    with connection() as c:
        c.execute("DELETE FROM app_state WHERE key=?", (key,))


# --------------------------------------------------------------------------
# row helpers
# --------------------------------------------------------------------------
def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """sqlite3.Row -> dict, decoding known JSON columns."""
    if row is None:
        return None
    d = dict(row)
    for k in _JSON_COLUMNS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


# --------------------------------------------------------------------------
# destructive
# --------------------------------------------------------------------------
# State keys that must SURVIVE a data wipe (identity/security/config, not browsing data).
_KEEP_STATE_KEYS = ("api_token_hash", "timezone")


def wipe_all_data() -> None:
    """'Delete all my data': empties every data table and resets lifecycle state.

    Keeps the API token hash and timezone so the extension stays authorised.
    Bootstrap/activation are reset so the product returns to a fresh-install state.
    """
    with connection() as c:
        for table in (
            "activity_sessions", "daily_summaries", "monthly_summaries",
            "classification_cache", "user_overrides",
        ):
            c.execute(f"DELETE FROM {table}")
        placeholders = ",".join("?" for _ in _KEEP_STATE_KEYS)
        c.execute(f"DELETE FROM app_state WHERE key NOT IN ({placeholders})", _KEEP_STATE_KEYS)
    conn = get_connection()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()


def db_file_exists() -> bool:
    return Path(_db_path()).exists()
