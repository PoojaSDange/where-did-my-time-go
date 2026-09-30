"""LLM classification core.

  * `build_prompt_items` / `parse_response` : shared by Gemini (history) and Groq (live worker)
  * `classify_history_pending`             : Gemini pass over unique unclassified signatures
Only redacted, compact fields are ever sent (see privacy.llm_item_fields). Sensitive rows are
never selected. Replies are short ids + enums only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Optional

import httpx

import database as db
from config import settings
from models import CATEGORIES, CATEGORY_SET
from services import activity_storage as storage
from services import privacy, timeutil

log = logging.getLogger("wdmt.ai_classifier")

SYSTEM_PROMPT = (
    "You classify browsing activity for a personal productivity tool. Categories: "
    + ", ".join(CATEGORIES)
    + ". Category is NOT a judgement of waste: YouTube can be learning or entertainment, Google search "
    "can be research, news/shopping/social are neutral. Use the domain, path and title. "
    "Always give your BEST-GUESS category with an honest confidence f from 0 to 1 "
    "(0.9+ clear, 0.5 plausible, 0.3 weak guess). Use 'ambiguous' ONLY when the domain, path and title give no "
    "usable clue at all (e.g. an empty or generic title on a multi-purpose site); a weak low-confidence guess is "
    "better than 'ambiguous'. "
    'Input: JSON list of items {"i":id,"d":domain,"p":path,"t":title,"s":seconds,"n":visits,'
    '"h":optional category this site usually has for this user}. "h" is only a hint: follow the title if it '
    "clearly points elsewhere. "
    'Reply with ONLY compact JSON: {"r":[{"i":id,"c":category,"f":confidence 0-1,"w":reason max 6 words}]}. '
    "No prose, no markdown, one entry per input id."
)


@dataclass
class ClassifyItem:
    key: int                # short id used in the prompt
    signature: str
    domain: str
    path: str
    title: str
    seconds: int = 0
    visits: int = 1
    hint: Optional[str] = None      # the site's usual category for this user (a derived label, no extra raw data)
    prior: Optional[tuple] = None   # (category, share, n) used for the fallback


@dataclass
class ClassResult:
    category: str
    confidence: float
    reason: str


class MalformedResponse(ValueError):
    pass


def item_payload(it: ClassifyItem) -> dict:
    f = privacy.llm_item_fields(it.domain, it.path, it.title)
    d = {"i": it.key, "d": f["d"], "p": f["p"], "t": f["t"]}
    if it.seconds:
        d["s"] = int(it.seconds)
    if it.visits and it.visits > 1:
        d["n"] = int(it.visits)
    if it.hint:
        d["h"] = it.hint
    return d


def payload_json(items: Iterable[ClassifyItem]) -> str:
    return json.dumps([item_payload(i) for i in items], separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.2))


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str):
    """Tolerant JSON extraction: strips <think>, code fences and surrounding prose."""
    if text is None:
        raise MalformedResponse("empty response")
    t = _THINK.sub("", text).strip()
    t = _FENCE.sub("", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = t.find(open_c), t.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(t[a : b + 1])
            except ValueError:
                continue
    raise MalformedResponse("no JSON found in response")


def parse_response(text: str, valid_ids: set[int]) -> dict[int, ClassResult]:
    """Parse a classification reply. Bad ENTRIES are skipped (one bad item never fails the batch).

    Raises MalformedResponse only if nothing usable can be read at all.
    """
    data = extract_json(text)
    if isinstance(data, dict):
        entries = data.get("r") or data.get("results") or data.get("items")
    else:
        entries = data
    if not isinstance(entries, list):
        raise MalformedResponse("missing result list")
    out: dict[int, ClassResult] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            i = int(e.get("i"))
        except (TypeError, ValueError):
            continue
        cat = str(e.get("c", "")).strip().lower()
        if i not in valid_ids or cat not in CATEGORY_SET:
            continue
        try:
            conf = float(e.get("f", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        if cat != "ambiguous" and conf < settings.classify_min_confidence:
            cat = "ambiguous"  # a guess weaker than the floor is not evidence
        reason = str(e.get("w", "") or "")[:80]
        out[i] = ClassResult(cat, conf, reason)
    if entries and not out:
        raise MalformedResponse("no valid entries")
    return out

def attach_priors(items: list[ClassifyItem]) -> None:
    """Give each item its site's usual category (from already-classified sessions of the same domain)."""
    priors = storage.domain_priors([i.domain for i in items])
    for it in items:
        p = priors.get(privacy.normalize_domain(it.domain))
        if p:
            it.prior, it.hint = p, p[0]


def apply_prior_fallback(it: ClassifyItem, res: ClassResult) -> tuple[ClassResult, str]:
    """If the model still says 'ambiguous' but this site has a strong usual category, use that (moderate
    confidence, so it is never cached and never counts as wasted). Returns (result, classified_by)."""
    if res.category == "ambiguous" and it.prior:
        cat, share, n = it.prior
        return ClassResult(cat, 0.6, f"site usually {cat} ({int(share * 100)}% of {n})"[:80]), "prior"
    return res, "llm"

def chunk_items(items: list[ClassifyItem], max_items: int, max_chars: int) -> list[list[ClassifyItem]]:
    """Size-capped chunks (by item count AND serialized size)."""
    chunks: list[list[ClassifyItem]] = []
    cur: list[ClassifyItem] = []
    size = 0
    for it in items:
        s = len(json.dumps(item_payload(it), ensure_ascii=False))
        if cur and (len(cur) >= max_items or size + s > max_chars):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += s
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------
# Gemini (historical bootstrap only)
# --------------------------------------------------------------------------
class GeminiError(RuntimeError):
    """A request-level failure: the call was answered but this chunk could not be used."""


class GeminiUnavailable(GeminiError):
    """The SERVICE cannot help right now (bad/blocked key, quota used up, outage, network).

    This says nothing about the pages, so it must never burn a page's attempts: the pass pauses and
    resumes automatically once Gemini answers again (e.g. after the quota refills)."""


# ---- pause state: survives restarts, is tied to the key+model so fixing either lifts it at once ----
PAUSE_KEY = "gemini_pause"


def _gemini_fingerprint() -> str:
    raw = f"{settings.gemini_api_key}|{settings.gemini_model}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def gemini_pause_status() -> Optional[dict]:
    """Active pause ({until, reason, failures}) or None. A pause recorded for a different key/model is ignored."""
    st = db.get_state(PAUSE_KEY, None)
    if not isinstance(st, dict) or st.get("fp") != _gemini_fingerprint():
        return None
    try:
        until = timeutil.parse_iso(st["until"])
    except (KeyError, TypeError, ValueError):
        return None
    if until <= timeutil.utcnow():
        return None
    return {"until": st["until"], "reason": st.get("reason", ""), "failures": int(st.get("failures", 1))}


def _pause_gemini(reason: str) -> None:
    st = db.get_state(PAUSE_KEY, None)
    prev = int(st.get("failures", 0)) if isinstance(st, dict) and st.get("fp") == _gemini_fingerprint() else 0
    failures = prev + 1
    minutes = min(settings.gemini_pause_max_minutes, settings.gemini_pause_base_minutes * (2 ** (failures - 1)))
    until = timeutil.utcnow() + timedelta(minutes=minutes)
    db.set_state(PAUSE_KEY, {"until": timeutil.to_iso(until), "reason": reason[:200],
                             "failures": failures, "fp": _gemini_fingerprint()})


def _clear_gemini_pause() -> None:
    if db.get_state(PAUSE_KEY, None) is not None:
        db.delete_state(PAUSE_KEY)


_last_gemini_call = 0.0


def _gemini_throttle() -> None:
    global _last_gemini_call
    min_interval = 60.0 / max(1, settings.gemini_rpm)
    wait = _last_gemini_call + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gemini_call = time.monotonic()


def gemini_generate(user_text: str) -> str:
    """One Gemini call (JSON mode). Retries transient errors with backoff. Raises GeminiError."""
    if not settings.gemini_api_key:
        raise GeminiError("GEMINI_API_KEY is not set")
    url = f"{settings.gemini_base_url}/models/{settings.gemini_model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    last: GeminiError = GeminiError("Gemini call failed")
    for attempt in range(settings.gemini_max_retries):
        _gemini_throttle()
        try:
            r = httpx.post(
                url, json=body, headers={"x-goog-api-key": settings.gemini_api_key},
                timeout=settings.gemini_timeout_seconds,
            )
            if r.status_code == 429 or r.status_code >= 500:
                # quota / rate limit / outage: the service's problem, never the pages'
                retry_after = float(r.headers.get("retry-after", 0) or 0)
                time.sleep(min(60.0, max(retry_after, 2.0 * (2 ** attempt))))
                last = GeminiUnavailable(f"HTTP {r.status_code}: {r.text[:150]}")
                continue
            if r.status_code >= 400:
                msg = f"HTTP {r.status_code}: {r.text[:200]}"
                if (r.status_code in (401, 403, 404)
                        or "API_KEY_INVALID" in r.text or "FAILED_PRECONDITION" in r.text):
                    raise GeminiUnavailable(msg)  # bad/blocked key, unknown model, region/billing: not the pages' fault
                raise GeminiError(msg)
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except httpx.HTTPError as e:
            last = GeminiUnavailable(f"network error: {type(e).__name__}")
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
        except (KeyError, IndexError, ValueError) as e:
            last = GeminiError(f"unreadable reply: {type(e).__name__}")
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
    raise last


def _classify_chunk(
    items: list[ClassifyItem], call: Callable[[str], str], depth: int = 0
) -> tuple[dict[int, ClassResult], bool]:
    """Classify one chunk. On a malformed reply, split in half and retry (bounded depth).

    Returns (results, transport_ok). Raises GeminiUnavailable if the service itself is failing.
    """
    ids = {i.key for i in items}
    try:
        text = call(payload_json(items))
    except GeminiUnavailable:
        raise  # the service is down/out of quota: the caller pauses the pass, no page is blamed
    except GeminiError as e:
        log.warning("gemini request failed for this chunk: %s", e)
        return {}, True  # chunk-level problem: its pages get an attempt, the other chunks still run
    try:
        return parse_response(text, ids), True
    except MalformedResponse as e:
        log.warning("malformed gemini reply (%s), %d items, depth %d", e, len(items), depth)
        if len(items) > 1 and depth < 3:
            mid = len(items) // 2
            left, ok1 = _classify_chunk(items[:mid], call, depth + 1)
            right, ok2 = _classify_chunk(items[mid:], call, depth + 1)
            return {**left, **right}, ok1 and ok2
        return {}, True


def classify_history_pending(
    tz_name: str,
    progress: Optional[Callable[[int, int], None]] = None,
    call: Optional[Callable[[str], str]] = None,
) -> dict:
    """Classify every still-pending HISTORY signature that rules could not classify.

    * unique signatures only (each classified once, applied to all its visits)
    * classification_cache first (confident results only are cached)
    * Gemini gets compact redacted items in size-capped chunks
    * failures increment attempt_count; at CLASSIFY_MAX_ATTEMPTS the rows become 'failed'
    """
    if call is None and not settings.gemini_api_key:
        # Configuration problem, not a classification failure: leave rows pending, spend no attempts.
        return {"signatures": 0, "from_cache": 0, "classified": 0, "failed_attempts": 0, "skipped": "no_api_key"}
    paused = gemini_pause_status()
    if paused:
        # Gemini was unavailable (key/quota/outage) a moment ago: don't hammer it, and spend no attempts.
        return {"signatures": 0, "from_cache": 0, "classified": 0, "failed_attempts": 0,
                "skipped": "paused", "paused_until": paused["until"], "reason": paused["reason"]}
    call = call or gemini_generate
    groups = storage.pending_signature_groups("history_estimated")
    stats = {"signatures": len(groups), "from_cache": 0, "classified": 0, "failed_attempts": 0}
    if not groups:
        return stats

    to_ask: list[ClassifyItem] = []
    key_to_sig: dict[int, str] = {}
    for n, g in enumerate(groups, start=1):
        cached = storage.cache_get(g["signature"])
        if cached is not None:
            storage.apply_classification_to_signature(
                g["signature"], "history_estimated", cached["category"], cached["confidence"],
                cached["reason"], "cache", tz_name,
            )
            storage.cache_touch(g["signature"])
            stats["from_cache"] += 1
            continue
        it = ClassifyItem(n, g["signature"], g["domain"], g["path"], g["title"], g["seconds"], g["visits"])
        to_ask.append(it)
        key_to_sig[n] = g["signature"]

    attach_priors(to_ask)
    chunks = chunk_items(to_ask, settings.gemini_chunk_items, settings.gemini_chunk_chars)

    done = 0
    for chunk in chunks:
        try:
            results, transport_ok = _classify_chunk(chunk, call)
        except GeminiUnavailable as e:
            _pause_gemini(str(e))
            stats["paused"] = str(e)
            log.warning("Gemini unavailable (%s); history classification paused, remaining pages stay pending "
                        "with no attempts used", e)
            break  # BEFORE any attempt is recorded; chunks finished earlier in this pass are already saved
        _clear_gemini_pause()  # Gemini answered: any earlier pause is over
        for it in chunk:
            res = results.get(it.key)
            if res is None:
                storage.record_failed_attempt(it.signature, "history_estimated")
                stats["failed_attempts"] += 1
                continue
            res, by = apply_prior_fallback(it, res)
            storage.apply_classification_to_signature(
                it.signature, "history_estimated", res.category, res.confidence, res.reason, by, tz_name
            )
            if by == "llm" and res.category != "ambiguous" and res.confidence >= settings.cache_min_confidence:
                storage.cache_put(it.signature, it.domain, it.path, it.title, res.category,
                                  res.confidence, res.reason, "llm")
            stats["classified"] += 1
        done += len(chunk)
        if progress:
            progress(done, len(to_ask))
        if not transport_ok:
            log.warning("gemini transport failing; stopping this pass early")
            break
    return stats
