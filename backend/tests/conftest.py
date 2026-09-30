"""Shared fixtures: every test gets a fresh temp DB and a known timezone."""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
import database as db  # noqa: E402
from services import timeutil  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch):
    settings.reload()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "app_mode", "local")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "gemini_rpm", 100000)  # no sleeping in tests
    db.init_db()
    db.set_state("timezone", "Asia/Kolkata")
    # reset module-level singletons that keep state between tests
    try:
        from services import groq_client
        groq_client.reset_for_tests()
    except ImportError:
        pass
    yield


def make_chrome_history(path: Path, visits: list[dict]) -> Path:
    """Create a Chrome-shaped History file. visit: {url,title,start(datetime UTC),dur_s,transition}."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INT, last_visit_time INT)")
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, "
                 "visit_duration INTEGER, transition INTEGER)")
    url_ids: dict[str, int] = {}
    for v in visits:
        key = (v["url"], v.get("title", ""))
        if key not in url_ids:
            cur = conn.execute("INSERT INTO urls(url,title,visit_count,last_visit_time) VALUES (?,?,1,0)",
                               (v["url"], v.get("title", "")))
            url_ids[key] = cur.lastrowid
        conn.execute(
            "INSERT INTO visits(url, visit_time, visit_duration, transition) VALUES (?,?,?,?)",
            (url_ids[key], timeutil.dt_to_chrome_us(v["start"]), int(v.get("dur_s", 0) * 1_000_000),
             v.get("transition", 0)),
        )
    conn.commit()
    conn.close()
    return path


def dt(s: str) -> datetime:
    return timeutil.parse_iso(s)
