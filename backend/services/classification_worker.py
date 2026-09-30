"""Persistent background worker classifying live (extension_measured) sessions with Groq + Qwen.

Loop: fetch pending rows -> resolve cheaply (override / rule / cache) -> unique signatures ->
token-sized batches -> Groq -> update successes -> leave failures pending.

Failure semantics (see groq_client):
  * service-level trouble (budget, rate limit, outage, open circuit): rows stay pending, NO attempt
    is burned, the worker backs off exponentially. A long outage must not permanently fail data.
  * item-level failure (malformed reply, item missing/invalid, request rejected): attempt_count += 1;
    after CLASSIFY_MAX_ATTEMPTS the row becomes 'failed' (never a fabricated category).
The tracker never waits on this worker.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import database as db
from config import settings
from models import SOURCE_LIVE
from services import activity_storage as storage
from services import ai_classifier, groq_client
from services.ai_classifier import ClassifyItem, MalformedResponse

log = logging.getLogger("wdmt.worker")


class ClassificationWorker:
    def __init__(self, call: Optional[Callable[[str], str]] = None) -> None:
        self._call = call  # injectable for tests: payload_json -> reply text
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.consecutive_failures = 0
        self.last_result: dict = {}
        self._run_lock = threading.Lock()  # the loop and a manual re-classify must not process the same rows twice

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="classification-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_result = self.run_once()
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("classification worker iteration failed")
                self.consecutive_failures += 1
            delay = self.next_delay()
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def next_delay(self) -> float:
        if self.consecutive_failures <= 0:
            return settings.classify_worker_interval_seconds
        d = settings.worker_backoff_base_seconds * (2 ** (self.consecutive_failures - 1))
        return min(settings.worker_backoff_max_seconds, d)

    # ---- one iteration --------------------------------------------------
    def run_once(self) -> dict:
        with self._run_lock:
            return self._run_once()

    def _run_once(self) -> dict:
        summary = {"resolved_cheap": 0, "classified": 0, "item_failures": 0, "stopped": None, "batches": 0}
        rows = storage.fetch_pending_live(limit=300)
        if not rows:
            self.consecutive_failures = 0
            return summary
        tz_name = storage.get_tz_name()

        # 1) cheap resolution: overrides, rules, cache (rows may have become resolvable since ingest)
        remaining = []
        with db.connection() as c:
            resolved: list[tuple] = []
            for r in rows:
                pc = storage.preclassify(c, r["domain"], _path(r["url"]), r["title"] or "", r["signature"] or "")
                if pc.status == "classified" and pc.category:
                    resolved.append((r["signature"], pc))
                else:
                    remaining.append(r)
        seen = set()
        for sig, pc in resolved:
            if sig in seen:
                continue
            seen.add(sig)
            n = storage.apply_classification_to_signature(sig, SOURCE_LIVE, pc.category, pc.confidence or 0.9,
                                                          pc.reason or "", pc.by or "rule", tz_name)
            summary["resolved_cheap"] += n
        if not remaining:
            self.consecutive_failures = 0
            return summary

        # 2) LLM for unique unresolved signatures
        if self._call is None and not settings.groq_api_key:
            summary["stopped"] = "no_api_key"
            return summary
        groups: dict[str, dict] = {}
        for r in remaining:
            g = groups.setdefault(r["signature"], {"row": r, "seconds": 0, "visits": 0})
            g["seconds"] += r["duration"]
            g["visits"] += 1
        items: list[ClassifyItem] = []
        for n, (sig, g) in enumerate(groups.items(), start=1):
            r = g["row"]
            items.append(ClassifyItem(n, sig, r["domain"], _path(r["url"]), r["title"] or "", g["seconds"], g["visits"]))

        ai_classifier.attach_priors(items)
        for batch in self._token_batches(items):
            try:
                results = self._classify_batch(batch, summary)
            except groq_client.GroqError as e:
                if e.retryable:
                    summary["stopped"] = f"{type(e).__name__}: {e}"
                    self.consecutive_failures += 1
                    log.warning("live classification paused: %s", e)
                    return summary
                # non-retryable (e.g. bad model name): counts as a real failed attempt
                storage_ids = [i.signature for i in batch]
                for sig in storage_ids:
                    storage.record_failed_attempt(sig, SOURCE_LIVE)
                summary["item_failures"] += len(batch)
                log.error("Groq rejected batch: %s", e)
                continue
            for it in batch:
                res = results.get(it.key)
                if res is None:
                    storage.record_failed_attempt(it.signature, SOURCE_LIVE)
                    summary["item_failures"] += 1
                    continue
                res, by = ai_classifier.apply_prior_fallback(it, res)
                storage.apply_classification_to_signature(
                    it.signature, SOURCE_LIVE, res.category, res.confidence, res.reason, by, tz_name
                )
                if by == "llm" and res.category != "ambiguous":
                    storage.cache_put(it.signature, it.domain, it.path, it.title, res.category,
                                      res.confidence, res.reason, "llm")
                summary["classified"] += 1
            summary["batches"] += 1
        self.consecutive_failures = 0
        return summary

    # ---- batching -------------------------------------------------------
    def _token_batches(self, items: list[ClassifyItem]) -> list[list[ClassifyItem]]:
        base = groq_client.estimate_tokens(ai_classifier.SYSTEM_PROMPT)
        batches: list[list[ClassifyItem]] = []
        cur: list[ClassifyItem] = []
        used = base
        for it in items:
            t = groq_client.estimate_tokens(json.dumps(ai_classifier.item_payload(it), ensure_ascii=False)) + 22  # + reply
            if cur and (used + t > settings.live_batch_tokens or len(cur) >= settings.live_batch_max_items):
                batches.append(cur)
                cur, used = [], base
            cur.append(it)
            used += t
        if cur:
            batches.append(cur)
        return batches

    def _classify_batch(self, items: list[ClassifyItem], summary: dict, depth: int = 0) -> dict:
        """Classify a batch; on a malformed reply split it in half (bounded) so one bad item can't sink the rest."""
        ids = {i.key for i in items}
        text = self._ask(items)
        try:
            return ai_classifier.parse_response(text, ids)
        except MalformedResponse as e:
            log.warning("malformed live reply (%s); %d items depth %d", e, len(items), depth)
            if len(items) > 1 and depth < 3:
                mid = len(items) // 2
                left = self._classify_batch(items[:mid], summary, depth + 1)
                right = self._classify_batch(items[mid:], summary, depth + 1)
                return {**left, **right}
            return {}

    def _ask(self, items: list[ClassifyItem]) -> str:
        payload = ai_classifier.payload_json(items)
        if self._call is not None:
            return self._call(payload)
        try:
            extra = json.loads(settings.groq_live_extra_json or "{}")
        except ValueError:
            extra = {}
        resp = groq_client.chat(
            [{"role": "system", "content": ai_classifier.SYSTEM_PROMPT}, {"role": "user", "content": payload}],
            priority=groq_client.PRIORITY_LIVE,
            model=settings.groq_live_model,
            max_tokens=min(1500, 40 * len(items) + 60),
            temperature=0.0,
            extra=extra or None,
        )
        return groq_client.message_text(resp)


def _path(url: Optional[str]) -> str:
    from services import privacy
    parts = privacy.split_url(url or "")
    return parts[1] if parts else "/"


worker = ClassificationWorker()
