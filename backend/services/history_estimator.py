"""Estimate how long each historical visit lasted, CONSERVATIVELY.

Chrome history only says *when* a page was opened. Rules:
  1. If Chrome recorded a visit_duration (>=1s) trust it, capped at HISTORY_MAX_VISIT_SECONDS.
  2. Otherwise the estimate is the gap to the next visit, capped at HISTORY_ESTIMATE_CAP_SECONDS
     (5 min): we never assume someone stared at one page for an hour.
  3. If the next visit is further away than HISTORY_IDLE_GAP_SECONDS (or there is none) the user
     probably left: use the small HISTORY_DEFAULT_SECONDS instead of the gap.
  4. A visit never extends past the next visit's start (history sessions never overlap).
  5. Visits that last <1s are redirect hops / same-second navigations and are dropped.
  6. Consecutive visits to the same signature within HISTORY_MERGE_GAP_SECONDS are merged
     (reloads, redirects) so one real stay is one session.

Deterministic for a fixed [window_start, window_end) plus lookahead, so chunk re-runs are idempotent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import settings
from services.history_processor import CleanVisit


@dataclass
class HistorySession:
    start: datetime
    end: datetime
    domain: str
    path: str
    url: str
    title: str
    signature: str
    visit_count: int = 1

    @property
    def duration(self) -> int:
        return int((self.end - self.start).total_seconds())


def lookahead() -> timedelta:
    """How far past the window end we must read so the last visit's gap is computed correctly."""
    return timedelta(seconds=max(3600, settings.history_idle_gap_seconds + 60))


def estimate_sessions(visits: list[CleanVisit], window_start: datetime, window_end: datetime) -> list[HistorySession]:
    visits = sorted(visits, key=lambda v: v.start)
    n = len(visits)
    raw: list[HistorySession] = []
    for i, v in enumerate(visits):
        if not (window_start <= v.start < window_end):
            continue
        gap = (visits[i + 1].start - v.start).total_seconds() if i + 1 < n else None

        cd = v.chrome_duration_seconds
        if cd is not None and cd >= 1:
            dur = min(cd, settings.history_max_visit_seconds)
        elif gap is None or gap > settings.history_idle_gap_seconds:
            dur = settings.history_default_seconds
        else:
            dur = min(gap, settings.history_estimate_cap_seconds)

        if gap is not None:
            dur = min(dur, gap)  # never overlap the next visit
        dur = int(dur)
        if dur < 1:
            continue  # redirect hop
        raw.append(HistorySession(
            start=v.start, end=v.start + timedelta(seconds=dur), domain=v.domain, path=v.path,
            url=v.url, title=v.title, signature=v.signature,
        ))

    merged: list[HistorySession] = []
    for s in raw:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.signature == s.signature
            and (s.start - prev.end).total_seconds() <= settings.history_merge_gap_seconds
        ):
            prev.end = max(prev.end, s.end)
            prev.visit_count += 1
        else:
            merged.append(s)
    return merged
