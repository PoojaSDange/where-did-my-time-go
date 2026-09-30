"""Daily analysis.

  1. deterministic metrics for the LOCAL day (Python/SQL) - always available immediately
  2. ALL evidence for the day (short sessions included), compacted by signature
  3. token-sized batches -> Groq -> per-batch observations (saved as they arrive => resumable)
  4. final Groq synthesis: verified metrics + observations -> daily insight
  5. stored in daily_summaries

A day is analysed ONCE, after it ends, and only when no rows are still pending (failed rows are
terminal and simply show up as coverage gaps). History-only days get deterministic summaries only.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Callable, Optional

import database as db
from config import settings
from models import AN_COMPLETE, AN_IN_PROGRESS, AN_NO_DATA, AN_DETERMINISTIC, AN_PARTIAL, AN_WAITING
from services import activity_storage as storage
from services import ai_classifier, analytics_tools as at, groq_client, timeutil

log = logging.getLogger("wdmt.daily")

OBS_SYSTEM = (
    "You analyse ONE day of a person's browsing activity. Input: JSON list of aggregated items "
    "(d=domain, p=path, t=title, c=category, s=seconds, n=visits, f=first local time, l=last local time, "
    "x=seconds flagged distracting). Write 3-6 short factual observations, max 110 words in total: notable long "
    "sessions, context switching, time-of-day patterns, distractions and what surrounded them. Use ONLY numbers "
    "that appear in the input; never add up or estimate totals. Category is not waste. Plain text, no headers."
)
SYNTH_SYSTEM = (
    "You write a person's daily browsing insight from VERIFIED METRICS (already computed - copy numbers exactly, do "
    "no arithmetic) and OBSERVATIONS. Reply with ONLY JSON: "
    '{"summary": "<=70 words, second person","highlights": ["<=4 items, <=14 words each"],'
    '"distractions": ["<=3 items"],"suggestion": "one gentle actionable sentence","confidence": "high|medium|low"}. '
    "Be conservative: a category is not automatically waste. If coverage is low or data thin, say so and lower "
    "confidence. If the data contains history estimates say they are estimates."
)

LLMCall = Callable[[str, str, int, int], str]  # (system, user, priority, max_tokens) -> text


def default_llm(system: str, user: str, priority: int, max_tokens: int) -> str:
    resp = groq_client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        priority=priority, model=settings.groq_analysis_model, max_tokens=max_tokens, temperature=0.2,
    )
    return groq_client.message_text(resp)


# --------------------------------------------------------------------------
def ensure_deterministic_summaries(since: datetime, until: datetime, tz_name: str) -> int:
    """Deterministic daily summaries for every local day in [since, until] (bootstrap: NO LLM)."""
    tz = timeutil.get_tz(tz_name)
    days = timeutil.days_in_window(since, until, tz)
    for d in days:
        at.refresh_day(d, tz_name)
    return len(days)


# --------------------------------------------------------------------------
def build_evidence(day: date, tz_name: str) -> list[dict]:
    """ALL activity of the local day aggregated by signature. Only redacted fields; sensitive => category only."""
    tz = timeutil.get_tz(tz_name)
    ws, we = timeutil.local_day_bounds(day, tz)
    agg: dict[str, dict] = {}
    for r in storage.sessions_between(ws, we):
        s, e, sec = at._clip(r, ws, we)
        if sec <= 0:
            continue
        cat = r["category"] if r["classification_status"] == "classified" else "unclassified"
        if r["sensitive"]:
            key = f"sensitive:{cat}"
            item = agg.setdefault(key, {"d": "[sensitive]", "c": cat, "s": 0, "n": 0, "f": None, "l": None, "x": 0})
        else:
            f = privacy_fields(r)
            item = agg.setdefault(r["signature"] or r["id"], {**f, "c": cat, "s": 0, "n": 0, "f": None, "l": None, "x": 0})
        item["s"] += sec
        item["n"] += 1
        fl = s.astimezone(tz).strftime("%H:%M")
        ll = e.astimezone(tz).strftime("%H:%M")
        item["f"] = fl if item["f"] is None or fl < item["f"] else item["f"]
        item["l"] = ll if item["l"] is None or ll > item["l"] else item["l"]
        if r["is_wasted"]:
            item["x"] += sec
    out = sorted(agg.values(), key=lambda x: -x["s"])
    for it in out:
        if not it["x"]:
            del it["x"]
    return out


def privacy_fields(r) -> dict:
    from services import privacy
    parts = privacy.split_url(r["url"] or "")
    path = parts[1] if parts else "/"
    f = privacy.llm_item_fields(r["domain"], path, r["title"])
    return {"d": f["d"], "p": f["p"], "t": f["t"]}


def make_batches(evidence: list[dict]) -> list[list[dict]]:
    budget = max(400, settings.analysis_batch_tokens - groq_client.estimate_tokens(OBS_SYSTEM) - 250)
    batches, cur, used = [], [], 0
    for it in evidence:
        t = groq_client.estimate_tokens(json.dumps(it, ensure_ascii=False, separators=(",", ":")))
        if cur and used + t > budget:
            batches.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += t
    if cur:
        batches.append(cur)
    return batches


def _fp(batch: list[dict]) -> str:
    return hashlib.sha1(json.dumps(batch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _metrics_prompt(day_row: dict) -> dict:
    fd = at.fmt_duration
    return {
        "date": day_row["day"],
        "total": fd(day_row["total_seconds"]),
        "measured_by_extension": fd(day_row["measured_seconds"]),
        "estimated_from_history": fd(day_row["estimated_seconds"]),
        "flagged_distracting": fd(day_row["wasted_seconds"]),
        "unclassified": fd(day_row["unclassified_seconds"]),
        "coverage_pct": day_row["coverage"].get("classified_pct"),
        "categories": {c: fd(s) for c, s in sorted(day_row["category_seconds"].items(), key=lambda kv: -kv[1])},
        "top_distractions": [{"domain": x["domain"], "time": fd(x["seconds"])} for x in day_row["top_distractions"]],
    }


def _set_status(day: date, status: str, **extra) -> None:
    with db.connection() as c:
        c.execute("UPDATE daily_summaries SET analysis_status=? WHERE day=?", (status, day.isoformat()))
        if "observations" in extra:
            c.execute("UPDATE daily_summaries SET partial_observations=? WHERE day=?",
                      (json.dumps(extra["observations"]), day.isoformat()))
        if "error" in extra:
            row = c.execute("SELECT coverage FROM daily_summaries WHERE day=?", (day.isoformat(),)).fetchone()
            cov = json.loads(row["coverage"]) if row else {}
            cov["analysis_error"] = extra["error"]
            c.execute("UPDATE daily_summaries SET coverage=? WHERE day=?", (json.dumps(cov), day.isoformat()))


def _clean_ai(data: dict) -> dict:
    def strs(v, n, ln):
        return [str(x)[:ln] for x in (v if isinstance(v, list) else [])][:n]

    conf = str(data.get("confidence", "medium")).lower()
    return {
        "summary": str(data.get("summary", ""))[:600],
        "highlights": strs(data.get("highlights"), 4, 140),
        "distractions": strs(data.get("distractions"), 3, 140),
        "suggestion": str(data.get("suggestion", ""))[:300],
        "confidence": conf if conf in ("high", "medium", "low") else "medium",
    }


def analyze_day(day: date, tz_name: Optional[str] = None, llm: Optional[LLMCall] = None,
                now: Optional[datetime] = None) -> str:
    """Analyse one completed local day. Returns the resulting analysis_status."""
    tz_name = tz_name or storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    today = timeutil.today_local(tz, now)
    at.refresh_day(day, tz_name, now)
    row = at.get_daily(day)
    assert row is not None
    status = row["analysis_status"]
    if day >= today or status in (AN_NO_DATA, AN_DETERMINISTIC, AN_COMPLETE, AN_IN_PROGRESS):
        return status

    if row["coverage"].get("pending", 0) > 0:
        _set_status(day, AN_WAITING)          # rows still awaiting classification: analyse later, record the gap
        return AN_WAITING

    llm = llm or default_llm
    evidence = build_evidence(day, tz_name)
    batches = make_batches(evidence)
    obs: list[dict] = list(row["partial_observations"] or [])
    have = {o["fp"] for o in obs}
    try:
        for b in batches:
            fp = _fp(b)
            if fp in have:
                continue  # resume: this batch was already observed before the budget ran out
            text = llm(OBS_SYSTEM, json.dumps(b, ensure_ascii=False, separators=(",", ":")),
                       groq_client.PRIORITY_DAILY, 350).strip()
            obs.append({"fp": fp, "text": text[:900]})
            have.add(fp)
            _set_status(day, AN_PARTIAL, observations=obs)
        obs = [o for o in obs if o["fp"] in {_fp(b) for b in batches}]  # drop stale observations
        ai = _synthesize(row, obs, llm)
    except groq_client.GroqError as e:
        log.info("daily analysis for %s paused: %s", day, e)
        _set_status(day, AN_PARTIAL if obs else AN_WAITING, error=f"{type(e).__name__}: {e}"[:200])
        return AN_PARTIAL if obs else AN_WAITING

    with db.connection() as c:
        c.execute(
            """UPDATE daily_summaries SET ai_analysis=?, ai_model=?, analysis_status='complete',
                      partial_observations='[]', analyzed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE day=?""",
            (json.dumps(ai, ensure_ascii=False), settings.groq_analysis_model, day.isoformat()),
        )
    return AN_COMPLETE


def _synthesize(row: dict, obs: list[dict], llm: LLMCall) -> dict:
    payload = json.dumps({"metrics": _metrics_prompt(row), "observations": [o["text"] for o in obs]},
                         ensure_ascii=False, separators=(",", ":"))
    last: Optional[Exception] = None
    for _ in range(2):
        text = llm(SYNTH_SYSTEM, payload, groq_client.PRIORITY_DAILY, 500)
        try:
            data = ai_classifier.extract_json(text)
            if isinstance(data, dict) and data.get("summary"):
                return _clean_ai(data)
            last = ai_classifier.MalformedResponse("missing summary")
        except ai_classifier.MalformedResponse as e:
            last = e
    log.warning("synthesis malformed twice (%s); falling back to observations only", last)
    return {"summary": " ".join(o["text"] for o in obs)[:600], "highlights": [], "distractions": [],
            "suggestion": "", "confidence": "low", "fallback": True}


