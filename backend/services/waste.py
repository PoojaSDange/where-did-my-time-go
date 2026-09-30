"""Decide `is_wasted` PER SESSION, never per page.

A category alone never makes time "wasted": news, shopping, YouTube-as-learning etc. are
not automatically wasted. Time is flagged only with sufficient evidence of unnecessary or
distracting activity:

  * category is social_media or entertainment (the only categories that can be a distraction)
  * classification confidence is high enough (>= WASTE_MIN_CONFIDENCE)
  * the session is long enough to be a real detour (>= WASTE_MIN_SECONDS)
  * AND either it happened during the user's usual work hours on a weekday,
    or it was a long session (>= WASTE_LONG_SECONDS) regardless of the clock.

Ambiguous / unknown / low-confidence => never wasted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config import settings

DISTRACTION_CATEGORIES = frozenset({"social_media", "entertainment"})


def decide_is_wasted(
    category: Optional[str],
    duration_seconds: int,
    confidence: Optional[float],
    start_utc: datetime,
    tz: ZoneInfo,
) -> bool:
    if category not in DISTRACTION_CATEGORIES:
        return False
    if confidence is None or confidence < settings.waste_min_confidence:
        return False
    if duration_seconds < settings.waste_min_seconds:
        return False
    if duration_seconds >= settings.waste_long_seconds:
        return True
    local = start_utc.astimezone(tz)
    in_work_hours = local.weekday() < 5 and settings.work_start_hour <= local.hour < settings.work_end_hour
    return in_work_hours
