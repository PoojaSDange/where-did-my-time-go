"""Monthly analysis. The architecture gets cheaper over time: raw -> daily -> monthly.

  1. daily summaries (deterministic for history days, AI-enriched for live days) are the input
  2. monthly metrics are aggregated deterministically from them
  3. Groq writes the monthly narrative from metrics + available daily insights
  4. history months get their narrative ONCE (bootstrap); old history is never re-read/re-classified
  5. the current month stays 'in_progress' (deterministic only) and keeps estimated vs measured separate
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import database as db
from config import settings
from models import AN_COMPLETE, AN_IN_PROGRESS, AN_NO_DATA, AN_TERMINAL_OK, AN_WAITING, PRODUCTIVE_CATEGORIES
from services import activity_storage as storage
from services import ai_classifier, analytics_tools as at, groq_client, timeutil
from services.daily_analysis import LLMCall, default_llm

log = logging.getLogger("wdmt.monthly")

MONTHLY_SYSTEM = (
    "You write a person's MONTHLY browsing summary from VERIFIED METRICS (already computed - copy numbers exactly, "
    "do no arithmetic), a weekly breakdown, and short daily insights where available. Reply with ONLY JSON: "
    '{"summary": "<=90 words, second person","patterns": ["<=4 items"],"wins": ["<=3 items"],'
    '"watch_outs": ["<=3 items"],"suggestion": "one gentle actionable sentence","confidence": "high|medium|low"}. '
    "Be conservative: a category is not automatically waste. Time from browser history is an ESTIMATE, time from "
    "the extension is MEASURED: always say which the conclusions rest on."
)


def _weekly(days: list[dict]) -> list[dict]:
    weeks: dict[str, dict] = defaultdict(lambda: {"total": 0, "productive": 0, "wasted": 0})
    for d in days:
        day = date.fromisoformat(d["day"])
        wk = (day - timedelta(days=day.weekday())).isoformat()
        w = weeks[wk]
        w["total"] += d["total_seconds"]
        w["wasted"] += d["wasted_seconds"]
        w["productive"] += sum(s for c, s in d["category_seconds"].items() if c in PRODUCTIVE_CATEGORIES)
    fd = at.fmt_duration
    return [{"week_of": k, "total": fd(v["total"]), "productive": fd(v["productive"]), "wasted": fd(v["wasted"])}
            for k, v in sorted(weeks.items())]


def _prompt(month: str, agg: dict, days: list[dict]) -> str:
    fd = at.fmt_duration
    active = [d for d in days if d["total_seconds"] > 0]
    worst = max(active, key=lambda d: d["wasted_seconds"], default=None)
    best = max(active, key=lambda d: sum(s for c, s in d["category_seconds"].items() if c in PRODUCTIVE_CATEGORIES),
               default=None)
    insights = []
    for d in active:
        ai = d["ai_analysis"] if isinstance(d["ai_analysis"], dict) else None
        if ai and ai.get("summary"):
            insights.append({"day": d["day"], "insight": ai["summary"][:220]})
    payload = {
        "month": month,
        "active_days": agg["days_count"],
        "total": fd(agg["total_seconds"]),
        "measured_by_extension": fd(agg["measured_seconds"]),
        "estimated_from_history": fd(agg["estimated_seconds"]),
        "flagged_distracting": fd(agg["wasted_seconds"]),
        "unclassified": fd(agg["unclassified_seconds"]),
        "coverage_pct": agg["coverage"].get("classified_pct"),
        "categories": {c: fd(s) for c, s in sorted(agg["category_seconds"].items(), key=lambda kv: -kv[1])},
        "top_distractions": [{"domain": x["domain"], "time": fd(x["seconds"])} for x in agg["top_distractions"]],
        "weekly": _weekly(days),
        "most_flagged_day": {"day": worst["day"], "flagged": fd(worst["wasted_seconds"])} if worst else None,
        "most_productive_day": {"day": best["day"]} if best else None,
        "daily_insights": insights[-10:],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _set_status(month: str, status: str) -> None:
    with db.connection() as c:
        c.execute("UPDATE monthly_summaries SET analysis_status=? WHERE month=?", (status, month))


def _clean(data: dict) -> dict:
    def strs(v, n, ln):
        return [str(x)[:ln] for x in (v if isinstance(v, list) else [])][:n]

    conf = str(data.get("confidence", "medium")).lower()
    return {
        "summary": str(data.get("summary", ""))[:800],
        "patterns": strs(data.get("patterns"), 4, 160),
        "wins": strs(data.get("wins"), 3, 160),
        "watch_outs": strs(data.get("watch_outs"), 3, 160),
        "suggestion": str(data.get("suggestion", ""))[:300],
        "confidence": conf if conf in ("high", "medium", "low") else "medium",
    }


def refresh_month(month: str, tz_name: str, now: Optional[datetime] = None) -> tuple[dict, list[dict]]:
    """Deterministic monthly aggregation (also keeps daily summaries fresh)."""
    tz = timeutil.get_tz(tz_name)
    first, last = timeutil.month_bounds(month, tz)
    days = at.ensure_daily_range(first, last, tz_name, now)
    return at.upsert_monthly(month, tz_name, days), days


def generate_monthly(month: str, tz_name: Optional[str] = None, llm: Optional[LLMCall] = None,
                     force: bool = False, now: Optional[datetime] = None) -> str:
    tz_name = tz_name or storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    agg, days = refresh_month(month, tz_name, now)
    current = timeutil.month_key(timeutil.today_local(tz, now))
    if month >= current:
        _set_status(month, AN_IN_PROGRESS)
        return AN_IN_PROGRESS
    if agg["total_seconds"] == 0:
        _set_status(month, AN_NO_DATA)
        return AN_NO_DATA
    existing = at.get_monthly(month)
    if existing and existing["analysis_status"] == AN_COMPLETE and not force:
        return AN_COMPLETE
    # Live days must be fully analysed (or terminal) first; history days are deterministic and always ready.
    if any(d["has_measured"] and d["analysis_status"] not in AN_TERMINAL_OK for d in days):
        _set_status(month, AN_WAITING)
        return AN_WAITING

    llm = llm or default_llm
    payload = _prompt(month, agg, days)
    try:
        ai: Optional[dict] = None
        for _ in range(2):
            text = llm(MONTHLY_SYSTEM, payload, groq_client.PRIORITY_MONTHLY, 600)
            try:
                data = ai_classifier.extract_json(text)
            except ai_classifier.MalformedResponse:
                continue
            if isinstance(data, dict) and data.get("summary"):
                ai = _clean(data)
                break
    except groq_client.GroqError as e:
        log.info("monthly analysis for %s deferred: %s", month, e)
        _set_status(month, AN_WAITING)
        return AN_WAITING
    if ai is None:
        _set_status(month, AN_WAITING)
        return AN_WAITING
    with db.connection() as c:
        c.execute(
            """UPDATE monthly_summaries SET ai_analysis=?, ai_model=?, analysis_status='complete',
                      analyzed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE month=?""",
            (json.dumps(ai, ensure_ascii=False), settings.groq_analysis_model, month),
        )
    return AN_COMPLETE


def data_months(tz_name: str, now: Optional[datetime] = None) -> list[str]:
    tz = timeutil.get_tz(tz_name)
    with db.read_connection() as c:
        r = c.execute("SELECT MIN(start_time) AS a FROM activity_sessions").fetchone()
    if not r or not r["a"]:
        return []
    first = timeutil.month_key(timeutil.local_date(timeutil.parse_iso(r["a"]), tz))
    last = timeutil.month_key(timeutil.today_local(tz, now))
    out, m = [], first
    while m <= last:
        out.append(m)
        m = timeutil.shift_month(m, 1)
    return out


def generate_completed_months(tz_name: Optional[str] = None, llm: Optional[LLMCall] = None,
                              now: Optional[datetime] = None) -> dict[str, str]:
    """Narratives for every COMPLETED month that has data and none yet; stops early if the budget is gone."""
    tz_name = tz_name or storage.get_tz_name()
    results: dict[str, str] = {}
    for m in data_months(tz_name, now):
        results[m] = generate_monthly(m, tz_name, llm, now=now)
        if results[m] == AN_WAITING and not groq_client.budget_remaining(groq_client.PRIORITY_MONTHLY):
            break
    return results
