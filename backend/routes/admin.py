"""Overrides, settings, delete-all-data, manual catch-up, and the supervisor agent endpoint."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import database as db
from config import settings
from models import CATEGORIES
from routes.common import tz_and_name
from security import require_token
from services import activity_storage as storage
from services import agent, analytics_tools as at, groq_client, monthly_analysis, privacy, timeutil
from services import agent, analytics_tools as at, ask_jobs, groq_client, monthly_analysis, privacy, reclassify, timeutil
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


# ---------------------------------------------------------------- overrides
class OverrideIn(BaseModel):
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    scope: Literal["signature", "domain"] = "signature"
    session_id: Optional[str] = None
    domain: Optional[str] = None
    signature: Optional[str] = None


@router.get("/overrides")
def list_overrides() -> dict:
    return {"overrides": storage.list_overrides(), "categories": list(CATEGORIES)}


@router.post("/overrides")
def create_override(body: OverrideIn) -> dict:
    """Correct a classification. Overrides beat cache and LLM and apply to future sessions."""
    domain, signature = body.domain, body.signature
    if body.session_id:
        with db.read_connection() as c:
            r = c.execute("SELECT domain, signature FROM activity_sessions WHERE id=?", (body.session_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "session not found")
        domain, signature = r["domain"], r["signature"]
    value = domain if body.scope == "domain" else signature
    if not value:
        raise HTTPException(400, "domain/signature/session_id required")
    _, name = tz_and_name()
    res = storage.set_override(body.scope, value, body.category, name)
    at.refresh_days_for_override(res["days"], name)              # deterministic numbers follow immediately
    for mk in {d[:7] for d in res["days"]}:
        monthly_analysis.refresh_month(mk, name)
    return {"ok": True, "updated_sessions": res["updated"], "days_recomputed": res["days"]}


@router.delete("/overrides/{override_id}")
def remove_override(override_id: int) -> dict:
    if not storage.delete_override(override_id):
        raise HTTPException(404, "override not found")
    return {"ok": True}

class ReclassifyIn(BaseModel):
    source: Optional[Literal["history_estimated", "extension_measured"]] = None


@router.post("/reclassify-ambiguous")
def reclassify_ambiguous(body: Optional[ReclassifyIn] = None) -> dict:
    """Give sessions the AI marked 'ambiguous' another try (improved prompt + the site's usual category)."""
    try:
        return reclassify.start(body.source if body else None)
    except reclassify.ReclassifyError as e:
        raise HTTPException(400, str(e))

# ---------------------------------------------------------------- settings / privacy
class DomainsIn(BaseModel):
    domains: list[str] = Field(max_length=500)


@router.get("/settings")
def get_settings() -> dict:
    return {
        "timezone": storage.get_tz_name(),
        "excluded_domains": storage.excluded_domains(),
        "sensitive_domains_builtin": settings.sensitive_domains,
        "sensitive_domains_extra": storage.extra_sensitive_domains(),
        "app_mode": settings.app_mode,
    }


@router.post("/settings/excluded-domains")
def set_excluded(body: DomainsIn) -> dict:
    cleaned = sorted({privacy.normalize_domain(d) for d in body.domains if d.strip()})
    db.set_state("excluded_domains", cleaned)
    return {"excluded_domains": cleaned}


@router.post("/settings/sensitive-domains")
def set_sensitive(body: DomainsIn) -> dict:
    """Extra sensitive domains: stored with a generic category, title/URL never sent to an LLM."""
    cleaned = sorted({privacy.normalize_domain(d) for d in body.domains if d.strip()})
    db.set_state("sensitive_domains_extra", cleaned)
    return {"sensitive_domains_extra": cleaned}


class TzIn(BaseModel):
    timezone: str


@router.post("/settings/timezone")
def set_timezone(body: TzIn) -> dict:
    if not timeutil.valid_tz(body.timezone):
        raise HTTPException(400, "unknown IANA timezone")
    db.set_state("timezone", body.timezone)
    return {"timezone": body.timezone}


class DeleteIn(BaseModel):
    confirm: str


@router.post("/data/delete")
def delete_all(body: DeleteIn) -> dict:
    """'Delete all my data': wipes the local database (keeps only the API token + timezone)."""
    if body.confirm != "DELETE":
        raise HTTPException(400, "send {\"confirm\": \"DELETE\"} to wipe all data")
    db.wipe_all_data()
    if settings.app_mode == "demo":
        from services import demo_seed
        demo_seed.seed_if_empty()
    return {"ok": True, "message": "All browsing data deleted."}


@router.post("/admin/catchup")
def run_catchup_now() -> dict:
    from services import catchup
    threading.Thread(target=catchup.run_catchup, daemon=True).start()
    return {"started": True}


# ---------------------------------------------------------------- supervisor agent
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[dict] = Field(default_factory=list, max_length=8)


_ask_log: dict[str, deque] = defaultdict(deque)
_ask_lock = threading.Lock()


def _demo_rate_limit(request: Request) -> None:
    if settings.app_mode != "demo":
        return
    ip = request.client.host if request.client else "?"
    now = time.time()
    with _ask_lock:
        q = _ask_log[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= settings.demo_ask_per_hour:
            raise HTTPException(429, "Demo limit reached: please try again later.")
        q.append(now)


@router.post("/ask/start")
def ask_start(body: AskIn, request: Request) -> dict:
    """Queue a question. It runs in the background and waits out per-minute limits (see services/ask_jobs.py)."""
    _demo_rate_limit(request)
    if not settings.groq_api_key:
        raise HTTPException(503, "The AI agent needs GROQ_API_KEY in backend/.env (see README).")
    return {"job_id": ask_jobs.submit(body.question, body.history)}


@router.get("/ask/status/{job_id}")
def ask_status(job_id: str) -> dict:
    job = ask_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "That question expired or the server restarted. Please ask again.")
    return job


@router.post("/ask")
def ask(body: AskIn, request: Request) -> dict:
    _demo_rate_limit(request)
    try:
        return agent.ask(body.question, body.history)
    except groq_client.GroqNotConfigured:
        raise HTTPException(503, "The AI agent needs GROQ_API_KEY in backend/.env (see README).")
    except groq_client.BudgetExhausted as e:
        raise HTTPException(429, f"Daily AI token budget used up; try again later. ({e})")
    except (groq_client.RateLimited, groq_client.CircuitOpen, groq_client.ServiceUnavailable) as e:
        raise HTTPException(503, f"The AI service is temporarily unavailable. ({e})")
    except groq_client.GroqError as e:
        raise HTTPException(502, f"AI request failed: {e}")
