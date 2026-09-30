"""The ONE gateway to Groq. Every Groq call in the project goes through `chat()`.

  * RPM / TPM sliding-window limiter (configurable)
  * persisted daily token counter (TPD) with PRIORITIES:
        live classification > daily analysis > monthly analysis > supervisor,
    where the supervisor keeps a reserved slice so Ask never dies completely
  * 429: honours retry-after, exponential backoff + jitter, bounded retries
  * circuit breaker: after repeated service failures calls fail fast for a cool-down
  * never blocks the tracker (callers are background threads / request threads, never the extension)

Errors carry `.retryable`: True = the SERVICE is unavailable (budget, rate limit, outage), so the
work should stay pending WITHOUT burning classification attempts; False = the request itself was
rejected (e.g. wrong model name) and counts as a real failed attempt.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import database as db
from config import settings

log = logging.getLogger("wdmt.groq")

PRIORITY_LIVE = 0
PRIORITY_DAILY = 1
PRIORITY_MONTHLY = 2
PRIORITY_SUPERVISOR = 3


class GroqError(RuntimeError):
    retryable = False


class GroqNotConfigured(GroqError):
    retryable = True


class BudgetExhausted(GroqError):
    retryable = True


class RateLimited(GroqError):
    retryable = True

    def __init__(self, msg: str, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after


class CircuitOpen(GroqError):
    retryable = True


class ServiceUnavailable(GroqError):
    retryable = True


# --------------------------------------------------------------------------
# helpers that tests replace
# --------------------------------------------------------------------------
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> float:
    return time.monotonic()


def _post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict, Any]:
    r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    return r.status_code, dict(r.headers), data


# --------------------------------------------------------------------------
# limiter
# --------------------------------------------------------------------------
class _Limiter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: deque[float] = deque()
        self.tokens: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.tokens.clear()

    def _prune(self, now: float) -> None:
        while self.requests and now - self.requests[0] >= 60:
            self.requests.popleft()
        while self.tokens and now - self.tokens[0][0] >= 60:
            self.tokens.popleft()

    def acquire(self, est_tokens: int) -> None:
        if est_tokens > settings.groq_tpm:
            raise GroqError(f"request (~{est_tokens} tokens) exceeds the per-minute budget ({settings.groq_tpm})")
        while True:
            with self.lock:
                now = _now()
                self._prune(now)
                used = sum(t for _, t in self.tokens)
                if len(self.requests) < settings.groq_rpm and used + est_tokens <= settings.groq_tpm:
                    self.requests.append(now)
                    self.tokens.append((now, est_tokens))
                    return
                waits = []
                if len(self.requests) >= settings.groq_rpm:
                    waits.append(60 - (now - self.requests[0]))
                if used + est_tokens > settings.groq_tpm:
                    over, acc = used + est_tokens - settings.groq_tpm, 0
                    for ts, t in self.tokens:
                        acc += t
                        if acc >= over:
                            waits.append(60 - (now - ts))
                            break
                wait = max(0.05, max(waits) if waits else 1.0)
            if wait > settings.groq_max_wait_seconds:
                raise RateLimited(f"per-minute limit; retry in ~{wait:.0f}s", retry_after=wait)
            _sleep(wait)


limiter = _Limiter()


# --------------------------------------------------------------------------
# circuit breaker
# --------------------------------------------------------------------------
class CircuitBreaker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures = 0
        self.open_until = 0.0

    def reset(self) -> None:
        with self.lock:
            self.failures = 0
            self.open_until = 0.0

    def allow(self) -> None:
        with self.lock:
            if self.open_until and _now() < self.open_until:
                raise CircuitOpen(f"Groq circuit open for another {self.open_until - _now():.0f}s")
            if self.open_until and _now() >= self.open_until:
                self.open_until = 0.0  # half-open: let one attempt through
                self.failures = max(0, settings.groq_breaker_threshold - 1)

    def success(self) -> None:
        with self.lock:
            self.failures = 0
            self.open_until = 0.0

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= settings.groq_breaker_threshold:
                self.open_until = _now() + settings.groq_breaker_cooldown_seconds
                log.warning("Groq circuit breaker OPEN for %ss", settings.groq_breaker_cooldown_seconds)

    @property
    def is_open(self) -> bool:
        with self.lock:
            return bool(self.open_until and _now() < self.open_until)


breaker = CircuitBreaker()


# --------------------------------------------------------------------------
# daily budget (persisted)
# --------------------------------------------------------------------------
_budget_lock = threading.Lock()


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def daily_used() -> int:
    st = db.get_state("groq_daily", None) or {}
    return int(st.get("used", 0)) if st.get("date") == _today_key() else 0


def _add_daily(tokens: int) -> None:
    with _budget_lock:
        st = db.get_state("groq_daily", None) or {}
        used = int(st.get("used", 0)) if st.get("date") == _today_key() else 0
        db.set_state("groq_daily", {"date": _today_key(), "used": used + max(0, int(tokens))})


def daily_cap(priority: int) -> int:
    tpd = settings.groq_tpd
    reserve = min(settings.groq_supervisor_reserve_tokens, tpd)
    general = tpd - reserve
    if priority == PRIORITY_LIVE:
        return general
    if priority == PRIORITY_DAILY:
        return int(general * settings.groq_daily_share_analysis)
    if priority == PRIORITY_MONTHLY:
        return int(general * settings.groq_daily_share_monthly)
    return tpd  # supervisor may use everything, including its reserve


def budget_remaining(priority: int) -> int:
    return max(0, daily_cap(priority) - daily_used())


def budget_status() -> dict:
    return {
        "used_today": daily_used(), "daily_limit": settings.groq_tpd,
        "supervisor_reserve": settings.groq_supervisor_reserve_tokens,
        "circuit_open": breaker.is_open, "configured": bool(settings.groq_api_key),
    }


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.2))


def estimate_messages(messages: list[dict], tools: Optional[list] = None) -> int:
    n = sum(estimate_tokens(str(m.get("content") or "")) + 8 for m in messages)
    if tools:
        n += estimate_tokens(json.dumps(tools))
    return n


def reset_for_tests() -> None:
    limiter.reset()
    breaker.reset()


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------
def _retry_after_seconds(headers: dict) -> float:
    raw = {k.lower(): v for k, v in headers.items()}.get("retry-after")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        m = re.fullmatch(r"(?:(\d+)m)?(?:([\d.]+)s)?", str(raw).strip())
        if m:
            return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    return 0.0


def chat(
    messages: list[dict],
    *,
    priority: int,
    model: str,
    max_tokens: int = 512,
    temperature: float = 0.1,
    tools: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict:
    """POST /chat/completions with budget, limiter, retries and breaker. Returns the JSON body."""
    if not settings.groq_api_key:
        raise GroqNotConfigured("GROQ_API_KEY is not set")
    breaker.allow()

    est = estimate_messages(messages, tools) + max_tokens
    if daily_used() + est > daily_cap(priority):
        raise BudgetExhausted(
            f"daily token budget exhausted for priority {priority} "
            f"({daily_used()}/{daily_cap(priority)} used); work stays pending"
        )

    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if extra:
        body.update(extra)
    url = f"{settings.groq_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"}

    last_err: Optional[Exception] = None
    for attempt in range(settings.groq_max_retries + 1):
        limiter.acquire(est)
        try:
            status, resp_headers, data = _post(url, headers, body, settings.groq_timeout_seconds)
        except httpx.HTTPError as e:
            last_err = ServiceUnavailable(f"network error: {type(e).__name__}")
            _backoff(attempt, 0.0)
            continue

        if status == 429:
            ra = _retry_after_seconds(resp_headers)
            wait = max(ra, min(2.0 * (2 ** attempt), 30.0)) * (1 + random.random() * 0.25)
            if wait > settings.groq_max_wait_seconds or attempt >= settings.groq_max_retries:
                breaker.failure()
                raise RateLimited("Groq rate limit (429)", retry_after=max(ra, wait))
            log.info("Groq 429; waiting %.1fs (retry-after=%s)", wait, ra)
            _sleep(wait)
            last_err = RateLimited("429", retry_after=ra)
            continue
        if status >= 500:
            last_err = ServiceUnavailable(f"Groq HTTP {status}")
            _backoff(attempt, 0.0)
            continue
        if status >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = str((data.get("error") or {}).get("message") or data)[:200]
            raise GroqError(f"Groq rejected the request (HTTP {status}): {msg}")

        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        _add_daily(int(usage.get("total_tokens") or est))
        breaker.success()
        return data

    breaker.failure()
    raise last_err or ServiceUnavailable("Groq unavailable")


def _backoff(attempt: int, retry_after: float) -> None:
    if attempt >= settings.groq_max_retries:
        return
    delay = max(retry_after, min(1.5 * (2 ** attempt), 20.0)) * (1 + random.random() * 0.25)
    _sleep(delay)


def message_text(response: dict) -> str:
    try:
        return response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
