"""Endpoints used by the Chrome extension (all require the API token)."""
from __future__ import annotations

from typing import Any, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import database as db
from config import settings
from security import require_token
from services import activity_storage as storage
from services import bootstrap, timeutil

router = APIRouter(prefix="/api/extension", dependencies=[Depends(require_token)])


class ActivityIn(BaseModel):
    id: str = Field(min_length=8, max_length=80)
    start: Union[str, int, float]
    end: Union[str, int, float]
    url: str = Field(max_length=4000)
    title: str = Field(default="", max_length=1000)


class IngestIn(BaseModel):
    activities: list[ActivityIn] = Field(max_length=200)
    tz: str | None = None


class ActivateIn(BaseModel):
    timezone: str | None = None


class ExcludedIn(BaseModel):
    domains: list[str] = Field(max_length=500)


def _maybe_update_tz(tz: str | None) -> None:
    if tz and timeutil.valid_tz(tz) and tz != db.get_state("timezone"):
        db.set_state("timezone", tz)


def _guard_demo() -> None:
    if settings.app_mode == "demo" and not settings.demo_allow_ingest:
        raise HTTPException(403, "Demo mode: live ingestion is disabled (synthetic data only).")


@router.post("/activate")
def activate(body: ActivateIn) -> dict:
    """Record the exact activation timestamp (once). Live tracking starts from this instant."""
    _guard_demo()
    _maybe_update_tz(body.timezone)
    ts = timeutil.to_iso(timeutil.utcnow())
    created = db.set_state_if_absent("activation_ts", ts)
    activation = storage.get_activation_ts()
    trimmed = 0
    if created:
        trimmed = storage.trim_history_to(activation)  # boundary rule: no history/measured overlap
    return {
        "activation_ts": timeutil.to_iso(activation),
        "newly_activated": created,
        "history_rows_trimmed": trimmed,
        "bootstrap_completed": bootstrap.is_completed(),
        "timezone": storage.get_tz_name(),
    }


@router.get("/config")
def config() -> dict:
    act = storage.get_activation_ts()
    return {
        "activated": act is not None,
        "activation_ts": timeutil.to_iso(act) if act else None,
        "excluded_domains": storage.excluded_domains(),
        "bootstrap_completed": bootstrap.is_completed(),
        "timezone": storage.get_tz_name(),
        "app_mode": settings.app_mode,
        "server_time": timeutil.to_iso(timeutil.utcnow()),
    }


@router.post("/excluded-domains")
def set_excluded(body: ExcludedIn) -> dict:
    from services import privacy
    cleaned = sorted({privacy.normalize_domain(d) for d in body.domains if d.strip()})
    db.set_state("excluded_domains", cleaned)
    return {"excluded_domains": cleaned}


@router.post("/activities")
def ingest(body: IngestIn) -> dict:
    """Idempotent upload. Any 200 means EVERY activity in the request is settled (accepted,
    duplicate, excluded or permanently rejected) so the extension may drop them from its queue."""
    _guard_demo()
    _maybe_update_tz(body.tz)
    stats = storage.insert_live([a.model_dump() for a in body.activities])
    db.set_state("last_ingest_at", timeutil.to_iso(timeutil.utcnow()))
    if stats["accepted"]:
        from services.classification_worker import worker
        worker.wake()
    return {**stats, "acked": [a.id for a in body.activities]}
