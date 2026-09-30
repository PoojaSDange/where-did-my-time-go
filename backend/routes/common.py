"""Shared helpers for the website routes."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from services import activity_storage as storage
from services import timeutil


def tz_and_name() -> tuple[ZoneInfo, str]:
    name = storage.get_tz_name()
    return timeutil.get_tz(name), name


def parse_range(spec: str, now: Optional[datetime] = None) -> tuple[datetime, datetime, str, ZoneInfo, str]:
    tz, name = tz_and_name()
    try:
        ws, we, label = timeutil.resolve_range(spec, tz, now)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ws, we, label, tz, name


def session_dict(r, tz: ZoneInfo) -> dict:
    s = timeutil.parse_iso(r["start_time"])
    e = timeutil.parse_iso(r["end_time"])
    return {
        "id": r["id"], "start": r["start_time"], "end": r["end_time"],
        "local_start": s.astimezone(tz).strftime("%Y-%m-%d %H:%M"), "local_end": e.astimezone(tz).strftime("%H:%M"),
        "domain": r["domain"], "url": r["url"], "title": r["title"] or "", "duration": r["duration"],
        "category": r["category"], "status": r["classification_status"], "is_wasted": bool(r["is_wasted"]),
        "confidence": r["confidence"], "reason": r["reason"], "source": r["source"],
        "classified_by": r["classified_by"], "sensitive": bool(r["sensitive"]), "signature": r["signature"],
        "attempts": r["attempt_count"],
    }
