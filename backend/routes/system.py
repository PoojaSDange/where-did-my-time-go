"""Health, public bootstrap config (no secrets in local mode), status and bootstrap control."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database as db
from config import settings
from security import require_token
from services import activity_storage as storage
from services import bootstrap, groq_client, history_reader, timeutil

public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@public.get("/health")
def health() -> dict:
    return {"ok": True}


@public.get("/public/config")
def public_config() -> dict:
    """Unauthenticated: only what the website needs to know before it has a token."""
    out = {"app_mode": settings.app_mode, "requires_token": True}
    if settings.app_mode == "demo" and settings.demo_public_token:
        out["demo_token"] = settings.demo_token  # demo data is synthetic by construction
    return out


def _first_day() -> str | None:
    with db.read_connection() as c:
        r = c.execute("SELECT MIN(start_time) AS a FROM activity_sessions").fetchone()
    if not r or not r["a"]:
        return None
    return timeutil.local_date(timeutil.parse_iso(r["a"]), storage.get_tz()).isoformat()


def _months() -> list[str]:
    with db.read_connection() as c:
        return [r["month"] for r in c.execute("SELECT month FROM monthly_summaries WHERE total_seconds>0 ORDER BY month")]


@router.get("/status")
def status() -> dict:
    from services.classification_worker import worker
    from services import ai_classifier
    act = storage.get_activation_ts()
    counts = storage.count_by_status()
    return {
        "app_mode": settings.app_mode,
        "timezone": storage.get_tz_name(),
        "activated": act is not None,
        "activation_ts": timeutil.to_iso(act) if act else None,
        "bootstrap": bootstrap.get_status(),
        **bootstrap.source_status(),
        "classification": {**counts, "worker_last": worker.last_result},
        "last_ingest_at": db.get_state("last_ingest_at"),
        "groq": groq_client.budget_status(),
        "gemini_configured": bool(settings.gemini_api_key),
        "gemini_pause": ai_classifier.gemini_pause_status(),  # None, or {until, reason, failures}
        "has_data": storage.total_rows() > 0,
        "first_data_day": _first_day(),
        "months": _months(),
        "excluded_domains": storage.excluded_domains(),
        "server_time": timeutil.to_iso(timeutil.utcnow()),
    }


class StartIn(BaseModel):
    profile_id: Optional[str] = None


@router.get("/history/profiles")
def history_profiles() -> dict:
    """Browser profiles found on this PC, for the user to choose from. Nothing is imported until they pick one."""
    if settings.app_mode == "demo":
        return {"profiles": [], "disabled": True, **bootstrap.source_status()}
    return {"profiles": history_reader.list_profiles(), **bootstrap.source_status()}


@router.post("/bootstrap/start")
def bootstrap_start(body: Optional[StartIn] = None) -> dict:
    """Start (or resume) the one-time history import. First call must carry the chosen profile_id."""
    try:
        return bootstrap.start_in_background(body.profile_id if body else None)
    except bootstrap.ProfileChoiceError as e:
        raise HTTPException(400, str(e))


@router.get("/bootstrap/status")
def bootstrap_status() -> dict:
    return bootstrap.get_status()
