"""Ask AI as a background job that WAITS OUT per-minute limits and resumes where it stopped.

Before: a question ran inside one web request. When the per-minute token window was full, the request
failed and the agent's progress (conversation + tool results so far) was thrown away, so every retry paid
for the early steps again and burned the daily budget for no answer.

Now: /api/ask/start queues the question and a single worker thread runs it. The agent's conversation lives
in memory for the whole job, so when Groq/our limiter says "wait ~40s" we simply sleep and retry THE SAME
STEP: earlier steps are never re-sent or re-paid. The browser polls /api/ask/status/<id> and shows progress.

What still ends a job (with a clear message, never a silent hang):
  * the DAILY token budget is used up (waiting would take hours)       -> error, nothing is retried
  * one single step is larger than the per-minute budget (waiting cannot help) -> error
  * the total waiting for one question passes ASK_MAX_WAIT_SECONDS      -> error
"""
from __future__ import annotations

import logging
import math
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings
from services import agent, groq_client

log = logging.getLogger("wdmt.ask")

MAX_SINGLE_WAIT = 90.0      # never sleep longer than this in one go (the limiter window is 60s)
KEEP_FINISHED = 50          # finished jobs kept for polling
TTL_SECONDS = 1800          # ...for at most 30 minutes


@dataclass
class Job:
    id: str
    question: str
    history: list
    status: str = "queued"          # queued | running | waiting | done | error
    step: int = 0
    wait_until: float = 0.0         # epoch seconds, only while status == "waiting"
    waited_total: float = 0.0
    result: Optional[dict] = None
    error: Optional[dict] = None    # {"status": http-ish code, "message": str}
    created: float = field(default_factory=time.time)
    finished: float = 0.0


_jobs: dict[str, Job] = {}
_queue: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def _sleep(seconds: float) -> None:   # replaced in tests
    time.sleep(seconds)


# ------------------------------------------------------------------ public API
def submit(question: str, history: Optional[list] = None) -> str:
    job = Job(id=secrets.token_urlsafe(9), question=question, history=list(history or []))
    with _lock:
        _prune()
        _jobs[job.id] = job
    _queue.put(job.id)
    _ensure_worker()
    return job.id


def get(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        ahead = sum(1 for j in _jobs.values()
                    if j.id != job.id and j.created <= job.created and j.status in ("queued", "running", "waiting"))
        return _view(job, ahead)


def reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


# ------------------------------------------------------------------ internals
def _view(job: Job, ahead: int) -> dict:
    remaining = max(0, math.ceil(job.wait_until - time.time())) if job.status == "waiting" else 0
    if job.status == "queued":
        msg = f"Waiting in line ({ahead} ahead of you)..." if ahead else "Starting..."
    elif job.status == "waiting":
        msg = (f"Hit the per-minute limit. Resuming from step {job.step} in ~{remaining}s "
               f"(progress so far is kept)...")
    elif job.status == "running":
        msg = f"Looking across your activity (step {job.step})..." if job.step else "Looking across your activity..."
    else:
        msg = ""
    return {"id": job.id, "status": job.status, "message": msg, "step": job.step,
            "waiting_seconds": remaining, "result": job.result, "error": job.error}


def _prune() -> None:
    now = time.time()
    done = sorted((j for j in _jobs.values() if j.status in ("done", "error")), key=lambda j: j.finished)
    excess = len(done) - KEEP_FINISHED
    for i, j in enumerate(done):
        if i < excess or now - j.finished > TTL_SECONDS:
            _jobs.pop(j.id, None)


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="ask-worker", daemon=True)
        _worker.start()


def _loop() -> None:
    while True:
        job_id = _queue.get()
        with _lock:
            job = _jobs.get(job_id)
        if job is None:
            continue
        try:
            _run(job)
        except Exception:  # noqa: BLE001 - the worker must never die
            log.exception("ask job crashed")
            _fail(job, 500, "Something went wrong while answering. Please try again.")


def _make_chat(job: Job):
    """Wrap the real Groq call: on a per-minute limit, wait and retry THE SAME step."""
    def chat(messages: list[dict], tools: Optional[list]) -> dict:
        job.step += 1
        while True:
            try:
                resp = agent._default_chat(messages, tools)   # looked up at call time so tests can replace it
                job.status = "running"
                return resp
            except groq_client.RateLimited as e:
                wait = min(MAX_SINGLE_WAIT, max(2.0, float(e.retry_after or 10.0)) + 1.0)
                if job.waited_total + wait > settings.ask_max_wait_seconds:
                    raise
                job.waited_total += wait
                job.wait_until = time.time() + wait
                job.status = "waiting"
                log.info("ask: per-minute limit at step %d, waiting %.0fs (total %.0fs)", job.step, wait, job.waited_total)
                _sleep(wait)
                job.status = "running"
    return chat


def _run(job: Job) -> None:
    job.status = "running"
    try:
        job.result = agent.ask(job.question, job.history, chat_fn=_make_chat(job))
        job.status = "done"
        job.finished = time.time()
    except groq_client.GroqNotConfigured:
        _fail(job, 503, "The AI agent needs GROQ_API_KEY in backend/.env (see README).")
    except groq_client.BudgetExhausted as e:
        _fail(job, 429, "Daily AI token budget used up. It resets at 00:00 UTC (5:30 AM IST); try again after that. "
                        f"({e})")
    except groq_client.RateLimited as e:
        _fail(job, 503, f"Still rate limited after waiting about {int(job.waited_total)}s; try again in a minute. ({e})")
    except (groq_client.CircuitOpen, groq_client.ServiceUnavailable) as e:
        _fail(job, 503, f"The AI service is temporarily unavailable. ({e})")
    except groq_client.GroqError as e:
        if "exceeds the per-minute budget" in str(e):
            _fail(job, 502, "This question pulled more data than fits in one request. Try a narrower question "
                            "(one day or one site), or lower AGENT_TOOL_RESULT_CHARS in backend/.env.")
        else:
            _fail(job, 502, f"AI request failed: {e}")


def _fail(job: Job, status: int, message: str) -> None:
    job.error = {"status": status, "message": message}
    job.status = "error"
    job.finished = time.time()
