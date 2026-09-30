"""Catch-up job.

The PC may be off or asleep at midnight, so nothing depends on "the server was running at midnight".
On startup and every CATCHUP_INTERVAL_MINUTES while running this job:
  * gives pending HISTORY items another classification pass (bootstrap never re-reads history)
  * marks retry-exhausted items as failed (terminal)
  * finds completed local days that have no completed analysis and processes them, OLDEST FIRST,
    respecting the token budget (partial progress is saved and resumed, never restarted)
  * writes monthly narratives for completed months that lack one
  * keeps today's + the current month's deterministic numbers fresh
No continuous regeneration during the day: a day is analysed once, after it ends.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from models import AN_COMPLETE, AN_PARTIAL, AN_TERMINAL_OK
from services import activity_storage as storage
from services import ai_classifier, analytics_tools as at, bootstrap, daily_analysis, groq_client, monthly_analysis, timeutil

log = logging.getLogger("wdmt.catchup")
_running = threading.Lock()


def run_catchup(tz_name: Optional[str] = None, llm=None, now: Optional[datetime] = None, classify_call=None) -> dict:
    if not _running.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return _run(tz_name, llm, now, classify_call)
    finally:
        _running.release()


def _run(tz_name, llm, now, classify_call) -> dict:
    tz_name = tz_name or storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    today = timeutil.today_local(tz, now)
    out: dict = {"days": {}, "months": {}, "history_retry": None}

    if bootstrap.is_completed() and (classify_call or settings.gemini_api_key):
        try:
            out["history_retry"] = ai_classifier.classify_history_pending(tz_name, call=classify_call)
        except Exception as e:  # noqa: BLE001
            log.warning("history classification retry failed: %s", e)
    storage.fail_exhausted()

    activation = storage.get_activation_ts()
    if activation is not None:
        first = timeutil.local_date(activation, tz)
        last = today - timedelta(days=1)
        processed = 0
        for d in timeutil.iter_days(first, last):  # oldest first
            at.refresh_day(d, tz_name, now)
            row = at.get_daily(d)
            if row is None or row["analysis_status"] in AN_TERMINAL_OK:
                continue
            status = daily_analysis.analyze_day(d, tz_name, llm, now)
            out["days"][d.isoformat()] = status
            if status in (AN_COMPLETE, AN_PARTIAL):
                processed += 1
            if status == AN_PARTIAL or not groq_client.budget_remaining(groq_client.PRIORITY_DAILY):
                break  # budget / rate limit hit: resume in the next window, oldest day first
            if processed >= settings.analysis_max_daily_ai_days_per_run:
                break

    try:
        out["months"] = monthly_analysis.generate_completed_months(tz_name, llm, now)
    except Exception as e:  # noqa: BLE001
        log.warning("monthly catch-up failed: %s", e)

    # cheap, always-available deterministic numbers for today / this month
    at.refresh_day(today, tz_name, now)
    monthly_analysis.refresh_month(timeutil.month_key(today), tz_name, now)
    return out


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, initial_delay: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(initial_delay,), name="catchup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, initial_delay: float) -> None:
        if self._stop.wait(initial_delay):
            return
        while not self._stop.is_set():
            try:
                run_catchup()
            except Exception:  # noqa: BLE001
                log.exception("catch-up iteration failed")
            if self._stop.wait(max(60.0, settings.catchup_interval_minutes * 60.0)):
                return


scheduler = Scheduler()
