"""Re-run the AI on sessions it previously marked 'ambiguous' (uses the improved prompt + site hints)."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from config import settings
from models import SOURCE_HISTORY, SOURCE_LIVE
from services import activity_storage as storage
from services import ai_classifier, analytics_tools as at, monthly_analysis

log = logging.getLogger("wdmt.reclassify")
_lock = threading.Lock()


class ReclassifyError(ValueError):
    pass


def start(source: Optional[str] = None) -> dict:
    """Reset LLM-'ambiguous' rows to pending and re-classify them in the background.

    Only sources whose API key is configured are touched (otherwise rows would just sit 'pending')."""
    sources = []
    if source in (None, SOURCE_HISTORY) and settings.gemini_api_key:
        sources.append(SOURCE_HISTORY)
    if source in (None, SOURCE_LIVE) and settings.groq_api_key:
        sources.append(SOURCE_LIVE)
    if not sources:
        raise ReclassifyError("Re-classifying needs GEMINI_API_KEY (history) and/or GROQ_API_KEY (live) in backend/.env.")
    if not _lock.acquire(blocking=False):
        raise ReclassifyError("A re-classification is already running. Try again in a minute.")
    try:
        tz_name = storage.get_tz_name()
        res = storage.reset_ambiguous(sources, tz_name)
    except Exception:
        _lock.release()
        raise
    threading.Thread(target=_run, args=(tz_name, sources, res["days"]), daemon=True, name="reclassify").start()
    return {"reset": res["reset"], "sources": sources}


def _run(tz_name: str, sources: list[str], days: list[str]) -> None:
    try:
        if SOURCE_HISTORY in sources:
            ai_classifier.classify_history_pending(tz_name)
        if SOURCE_LIVE in sources:
            from services.classification_worker import worker
            for _ in range(30):
                s = worker.run_once()
                if s.get("stopped") or not storage.fetch_pending_live(1):
                    break
        at.refresh_days_for_override(days, tz_name)
        for m in {d[:7] for d in days}:
            monthly_analysis.refresh_month(m, tz_name)
    except Exception:  # noqa: BLE001
        log.exception("re-classification failed")
    finally:
        _lock.release()