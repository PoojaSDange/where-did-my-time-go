"""First-run history bootstrap. Runs ONCE; resumable; idempotent.

Stages:  read  ->  classify  ->  daily  ->  monthly  ->  completed
  read      Chrome history (temp copy) -> clean -> estimate -> store, in fixed local-day chunks.
  classify  rules already applied at insert; cache + Gemini for the remaining unique signatures.
  daily     deterministic daily summaries for every history day (NO LLM).
  monthly   LLM monthly narratives for completed history months (Groq), once.

`app_state.bootstrap_completed` guarantees it never runs twice; history is never re-read afterwards.
The bootstrap window (since/until) is persisted at first start so a resumed run computes identical
chunks (=> identical activity_keys => no duplicates).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import database as db
from config import settings
from services import activity_storage as storage
from services import ai_classifier, history_estimator, history_processor, timeutil
from services import history_reader
from services.history_reader import HistoryDisabled, HistoryReader, HistoryUnavailable

log = logging.getLogger("wdmt.bootstrap")

STATE_KEY = "bootstrap_state"
WINDOW_KEY = "bootstrap_window"
_thread: Optional[threading.Thread] = None
_guard = threading.Lock()


def is_completed() -> bool:
    return bool(db.get_state("bootstrap_completed", False))


def get_status() -> dict:
    st = db.get_state(STATE_KEY, None) or {"status": "idle", "stage": None, "done": 0, "total": 0}
    st = dict(st)
    st["completed"] = is_completed()
    if st["completed"]:
        st["status"] = "completed"
    st["running"] = bool(_thread and _thread.is_alive())
    return st


def _set(**kw) -> None:
    st = dict(db.get_state(STATE_KEY, None) or {})
    st.update(kw)
    db.set_state(STATE_KEY, st)


def reset_stale_running() -> None:
    """Called on startup: a 'running' state with no live thread means the process died mid-run."""
    st = db.get_state(STATE_KEY, None)
    if st and st.get("status") == "running" and not (_thread and _thread.is_alive()):
        _set(status="interrupted")


class ProfileChoiceError(ValueError):
    pass


def source_status() -> dict:
    src = db.get_state("history_source", None)
    return {"history_source": ({"id": src["id"], "label": src["label"]} if src else None),
            "history_forced": bool(settings.chrome_history_path)}


def choose_profile(profile_id: str) -> dict:
    """Record which browser profile to import. Switching before completion discards the partial import."""
    prof = history_reader.profile_by_id(profile_id)
    if prof is None:
        raise ProfileChoiceError("Unknown profile. Refresh the list and pick one of the profiles shown.")
    prev = db.get_state("history_source", None)
    if prev and prev.get("id") != prof["id"]:
        with db.connection() as c:  # different profile => the partial import from the old one must not mix in
            c.execute("DELETE FROM activity_sessions WHERE source='history_estimated'")
            c.execute("DELETE FROM daily_summaries")
            c.execute("DELETE FROM monthly_summaries")
        db.delete_state(WINDOW_KEY)
        db.delete_state(STATE_KEY)
    label = f"{prof['browser']} - {prof['name']}" + (f" ({prof['email']})" if prof["email"] else "")
    db.set_state("history_source", {"id": prof["id"], "path": prof["path"], "label": label})
    return prof


def start_in_background(profile_id: Optional[str] = None) -> dict:
    """Idempotent trigger used by the website. Never guesses the profile: it must have been chosen."""
    global _thread
    if settings.app_mode == "demo":
        return {**get_status(), "started": False, "disabled": "demo mode"}
    with _guard:
        if is_completed():
            return {**get_status(), "started": False}
        if _thread and _thread.is_alive():
            return {**get_status(), "started": False}
        if profile_id:
            choose_profile(profile_id)
        if not db.get_state("history_source", None) and not settings.chrome_history_path:
            return {**get_status(), "started": False, "needs_profile": True}
        _thread = threading.Thread(target=_safe_run, name="bootstrap", daemon=True)
        _thread.start()
    return {**get_status(), "started": True}


def _safe_run() -> None:
    try:
        run()
    except (HistoryUnavailable, HistoryDisabled) as e:
        log.error("bootstrap cannot read history: %s", e)
        _set(status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("bootstrap failed")
        _set(status="failed", error=f"{type(e).__name__}: {e}")


def _window(tz) -> tuple[datetime, datetime]:
    w = db.get_state(WINDOW_KEY)
    if w:
        return timeutil.parse_iso(w["since"]), timeutil.parse_iso(w["until"])
    now = timeutil.utcnow()
    activation = storage.get_activation_ts()
    until = min(now, activation) if activation else now
    first_day = timeutil.local_date(until, tz) - timedelta(days=settings.history_days)
    since, _ = timeutil.local_day_bounds(first_day, tz)
    db.set_state(WINDOW_KEY, {"since": timeutil.to_iso(since), "until": timeutil.to_iso(until)})
    return since, until


def _chunks(since: datetime, until: datetime, tz) -> list[tuple[datetime, datetime]]:
    out = []
    day = timeutil.local_date(since, tz)
    last = timeutil.local_date(until, tz)
    while day <= last:
        end_day = min(day + timedelta(days=settings.history_chunk_days - 1), last)
        s, _ = timeutil.local_day_bounds(day, tz)
        _, e = timeutil.local_day_bounds(end_day, tz)
        out.append((max(s, since), min(e, until)))
        day = end_day + timedelta(days=1)
    return out


def run(reader_factory=None, classify_call=None) -> None:
    """Blocking, resumable bootstrap. Safe to call again after a crash."""
    if is_completed():
        return
    if settings.app_mode == "demo":
        raise HistoryDisabled("bootstrap is disabled in demo mode")
    tz_name = storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    since, until = _window(tz)
    st = db.get_state(STATE_KEY, None) or {}
    _set(status="running", error=None, started_at=st.get("started_at") or timeutil.to_iso(timeutil.utcnow()),
         warnings=st.get("warnings", []))

    # ---------------- stage 1: read ----------------
    done_chunks = int((db.get_state(STATE_KEY) or {}).get("chunks_done", 0))
    chunks = _chunks(since, until, tz)
    if done_chunks < len(chunks):
        factory = reader_factory or HistoryReader
        with factory() as reader:
            for idx in range(done_chunks, len(chunks)):
                c_start, c_end = chunks[idx]
                _set(stage="read", done=idx, total=len(chunks), message="Reading browser history")
                raw = reader.visits(c_start, min(c_end + history_estimator.lookahead(), until))
                clean = history_processor.clean_visits(raw, storage.excluded_domains())
                sessions = history_estimator.estimate_sessions(clean, c_start, c_end)
                kept = []
                for s in sessions:
                    s.end = min(s.end, until)  # never beyond the moment we read history
                    if s.end > s.start:
                        kept.append(s)
                storage.insert_history(kept, tz_name)
                _set(chunks_done=idx + 1)
    _set(stage="read", done=len(chunks), total=len(chunks))

    # ---------------- stage 2: classify ----------------
    _set(stage="classify", done=0, total=0, message="Classifying unique pages")
    stats = ai_classifier.classify_history_pending(
        tz_name, progress=lambda d, t: _set(done=d, total=t), call=classify_call
    )
    if stats.get("skipped") == "no_api_key":
        warns = list((db.get_state(STATE_KEY) or {}).get("warnings", []))
        msg = "GEMINI_API_KEY not set: pages not covered by rules stay unclassified until a key is added."
        if msg not in warns:
            warns.append(msg)
        _set(warnings=warns)

    # ---------------- stage 3: deterministic daily summaries (no LLM) ----------------
    from services import daily_analysis  # local import: analytics layer is built on top of storage

    _set(stage="daily", done=0, total=0, message="Building daily summaries")
    daily_analysis.ensure_deterministic_summaries(since, until, tz_name)

    # ---------------- stage 4: monthly narratives (one-time) ----------------
    from services import monthly_analysis

    _set(stage="monthly", done=0, total=0, message="Writing monthly summaries")
    try:
        monthly_analysis.generate_completed_months(tz_name)
    except Exception as e:  # noqa: BLE001  (LLM trouble must not fail the bootstrap; catch-up resumes it)
        log.warning("monthly narratives deferred: %s", e)

    db.set_state("bootstrap_completed", True)
    _set(status="completed", stage="done", message="History ready", finished_at=timeutil.to_iso(timeutil.utcnow()))
    log.info("bootstrap completed")
