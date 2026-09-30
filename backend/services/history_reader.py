"""Read Chrome history straight from disk.

Chrome keeps its `History` SQLite file locked while running, so we NEVER open it in place:
the file (and any journal/WAL sidecars) is copied to a temp dir and the COPY is read.
Chrome timestamps are microseconds since 1601-01-01 UTC (see timeutil.chrome_us_to_dt).
This module is disabled in demo mode.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from config import settings
from services import timeutil

log = logging.getLogger("wdmt.history_reader")

# Chrome "core" transition types that are sub-frame navigations (iframes) -> not user browsing.
_SUBFRAME_TRANSITIONS = {3, 4}


class HistoryUnavailable(RuntimeError):
    pass


class HistoryDisabled(RuntimeError):
    pass


@dataclass
class RawVisit:
    start: datetime
    chrome_duration_seconds: Optional[float]  # Chrome's own visit_duration if present
    url: str
    title: str


def _user_data_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return []
        base = Path(local)
        return [base / "Google/Chrome/User Data", base / "Microsoft/Edge/User Data", base / "Chromium/User Data"]
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
        return [base / "Google/Chrome", base / "Chromium"]
    cfg = home / ".config"
    return [cfg / "google-chrome", cfg / "chromium"]


def _browser_label(user_data: Path) -> str:
    parts = " ".join(user_data.parts).lower()
    return "Edge" if "edge" in parts else ("Chromium" if "chromium" in parts else "Chrome")


def _profile_meta(user_data: Path, profile_dir: str) -> dict:
    """Friendly name / email from the browser's own 'Local State' file (best effort)."""
    try:
        info = json.loads((user_data / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"][profile_dir]
        return {"name": info.get("name") or profile_dir, "email": info.get("user_name") or info.get("gaia_name") or ""}
    except Exception:  # noqa: BLE001 - metadata is a nicety, never a requirement
        return {"name": profile_dir, "email": ""}


def list_profiles() -> list[dict]:
    """Every browser profile that has a History file. NOTHING is chosen automatically:
    the user picks one on the website. Most recently used first (just for display order)."""
    out: list[dict] = []
    for ud in _user_data_dirs():
        if not ud.is_dir():
            continue
        for n in ["Default"] + sorted(p.name for p in ud.glob("Profile *")):
            h = ud / n / "History"
            if not h.is_file():
                continue
            st = h.stat()
            meta = _profile_meta(ud, n)
            out.append({
                "id": f"{ud.parent.name if ud.name == 'User Data' else ud.name}|{n}|{ud.name}",
                "browser": _browser_label(ud), "profile_dir": n, "name": meta["name"], "email": meta["email"],
                "path": str(h), "size_bytes": st.st_size, "last_used": int(st.st_mtime),
            })
    return sorted(out, key=lambda x: -x["last_used"])


def profile_by_id(profile_id: str) -> Optional[dict]:
    """Only ids we generated ourselves are accepted (never a client-supplied file path)."""
    return next((p for p in list_profiles() if p["id"] == profile_id), None)


def resolve_history_file(raw: str) -> Path:
    """Accept either the History FILE or the profile FOLDER that contains it."""
    p = Path(raw.strip().strip('"')).expanduser()
    if p.is_dir():
        if (p / "History").is_file():
            return p / "History"
        profiles = sorted(x.name for x in p.glob("Profile *")) + (["Default"] if (p / "Default").is_dir() else [])
        hint = f" It looks like the 'User Data' folder; point to a profile inside it, e.g. {p / (profiles[0] if profiles else 'Default') / 'History'}" if profiles else ""
        raise HistoryUnavailable(f"No 'History' file inside {p}.{hint}")
    if not p.exists():
        raise HistoryUnavailable(f"CHROME_HISTORY_PATH does not exist: {p}")
    return p


def find_history_path() -> Path:
    """The history file the USER chose (stored in the DB), or an explicit CHROME_HISTORY_PATH. Never a guess."""
    import database as db

    chosen = db.get_state("history_source", None)
    if chosen and chosen.get("path"):
        p = Path(chosen["path"])
        if not p.exists():
            raise HistoryUnavailable(f"The chosen profile's history file no longer exists: {p}. Choose a profile again.")
        return p
    if settings.chrome_history_path:
        return resolve_history_file(settings.chrome_history_path)
    raise HistoryUnavailable("No Chrome profile has been chosen yet. Pick the profile to import on the website.")


def _snapshot(src: Path, dst: Path, attempts: int = 4) -> None:
    """Make a private copy of a (possibly locked) SQLite file. Never reads the original in place."""
    last: Optional[Exception] = None
    for i in range(attempts):                       # 1) plain file copy (works while Chrome is open on most systems)
        try:
            shutil.copyfile(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(0.3 * (i + 1))
    try:                                            # 2) read-only, no-locking SQLite backup into the temp file
        uri = src.resolve().as_uri() + "?mode=ro&immutable=1"
        s_conn = sqlite3.connect(uri, uri=True)
        try:
            d_conn = sqlite3.connect(str(dst))
            try:
                s_conn.backup(d_conn)
            finally:
                d_conn.close()
        finally:
            s_conn.close()
        return
    except sqlite3.Error as e:
        last = e
    raise HistoryUnavailable(
        f"Could not copy Chrome's history file ({src}): {last}. Try closing Chrome once, "
        "or check that CHROME_HISTORY_PATH points to the 'History' file of your profile."
    )


class HistoryReader:
    """Context manager: copies the DB once, then answers windowed queries on the copy."""

    def __init__(self, source: Optional[Path] = None) -> None:
        if settings.app_mode == "demo":
            raise HistoryDisabled("history_reader is disabled in demo mode")
        self._source = source
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "HistoryReader":
        src = self._source or find_history_path()
        self._tmp = tempfile.TemporaryDirectory(prefix="wdmt_hist_")
        dst = Path(self._tmp.name) / "History"
        _snapshot(src, dst)
        for suffix in ("-journal", "-wal", "-shm"):  # sidecars so a hot journal can recover
            side = Path(str(src) + suffix)
            if side.exists():
                try:
                    shutil.copyfile(side, Path(str(dst) + suffix))
                except OSError:
                    pass
        try:
            self._conn = sqlite3.connect(str(dst))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("SELECT 1 FROM visits LIMIT 1")
        except sqlite3.Error as e:
            self.close()
            raise HistoryUnavailable(f"Chrome history copy is not readable: {e}")
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def visits(self, since: datetime, until: datetime) -> list[RawVisit]:
        """Top-level visits with since <= visit_time < until, oldest first."""
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT v.visit_time AS t, v.visit_duration AS dur, v.transition AS tr,
                   u.url AS url, u.title AS title
            FROM visits v JOIN urls u ON u.id = v.url
            WHERE v.visit_time >= ? AND v.visit_time < ?
            ORDER BY v.visit_time ASC
            """,
            (timeutil.dt_to_chrome_us(since), timeutil.dt_to_chrome_us(until)),
        ).fetchall()
        out: list[RawVisit] = []
        for r in rows:
            if (int(r["tr"] or 0) & 0xFF) in _SUBFRAME_TRANSITIONS:
                continue
            dur = (r["dur"] or 0) / 1_000_000 if (r["dur"] or 0) > 0 else None
            out.append(RawVisit(
                start=timeutil.chrome_us_to_dt(int(r["t"])),
                chrome_duration_seconds=dur,
                url=r["url"] or "",
                title=r["title"] or "",
            ))
        return out
