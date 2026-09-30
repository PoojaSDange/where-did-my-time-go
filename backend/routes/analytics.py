"""Website read APIs: dashboard, activity, insights, trends."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from models import PRODUCTIVE_CATEGORIES, SOURCE_HISTORY, SOURCE_LIVE, UI_GROUPS
from routes.common import parse_range, session_dict, tz_and_name
from security import require_token
from services import activity_storage as storage
from services import analytics_tools as at
from services import monthly_analysis, timeutil

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _group_totals(cat_seconds: dict, wasted: int, total: int) -> dict:
    """The website's colour groups. Wasted time is carved OUT of its category so groups never double count."""
    focus = sum(cat_seconds.get(c, 0) for c in UI_GROUPS["focus"])
    learn = sum(cat_seconds.get(c, 0) for c in UI_GROUPS["learn"])
    brk = sum(cat_seconds.get(c, 0) for c in UI_GROUPS["break"])
    return {"focus": focus, "learn": learn, "break": brk, "distract": wasted}


@router.get("/dashboard")
def dashboard(spec: str = Query("today", alias="range")) -> dict:
    ws, we, label, tz, name = parse_range(spec)
    rows = storage.sessions_between(ws, we)
    m = at.compute_metrics(rows, ws, we)
    prev = at.metrics_for_window(ws - (we - ws), ws)
    total = m["total_seconds"]
    cats = m["category_seconds"]
    other = total - m["unclassified_seconds"] - sum(
        v for k, v in cats.items() if k in {*UI_GROUPS["focus"], *UI_GROUPS["learn"], *UI_GROUPS["break"]}
    )
    groups = _group_totals(cats, m["wasted_seconds"], total)
    domain_agg: dict[str, int] = defaultdict(int)
    for r in rows:
        _, _, sec = at._clip(r, ws, we)
        domain_agg[r["domain"]] += sec
    return {
        "range": {"spec": spec, "label": label, "start": timeutil.to_iso(ws), "end": timeutil.to_iso(we), "timezone": name},
        "totals": {
            "total": total, "productive": at.productive_seconds(m), "learning": groups["learn"],
            "focus": groups["focus"], "break": groups["break"], "wasted": m["wasted_seconds"],
            "unclassified": m["unclassified_seconds"], "estimated": m["estimated_seconds"],
            "measured": m["measured_seconds"], "other": max(0, other - m["wasted_seconds"]),
        },
        "previous": {"total": prev["total_seconds"], "productive": at.productive_seconds(prev),
                     "wasted": prev["wasted_seconds"], "learning": sum(prev["category_seconds"].get(c, 0) for c in UI_GROUPS["learn"])},
        "groups": groups,
        "categories": [{"category": c, "seconds": s, "pct": round(100.0 * s / total, 1) if total else 0}
                       for c, s in sorted(cats.items(), key=lambda kv: -kv[1])],
        "by_source": m["by_source"],
        "top_domains": [{"domain": d, "seconds": s} for d, s in sorted(domain_agg.items(), key=lambda kv: -kv[1])[:6]],
        "top_distractions": m["top_distractions"],
        "coverage": m["coverage"],
        "cards": at.insight_cards(rows, ws, we, tz),
    }


@router.get("/activity")
def activity(
    spec: str = Query("today", alias="range"), category: Optional[str] = None, source: Optional[str] = None,
    q: Optional[str] = None, status: Optional[str] = None,
    limit: int = Query(300, ge=1, le=1000), offset: int = Query(0, ge=0),
) -> dict:
    ws, we, label, tz, name = parse_range(spec)
    rows = storage.sessions_between(ws, we)
    m = at.compute_metrics(rows, ws, we)
    ql = (q or "").strip().lower()

    def keep(r) -> bool:
        if category == "unclassified":
            if r["classification_status"] == "classified":
                return False
        elif category and r["category"] != category:
            return False
        if source and r["source"] != source:
            return False
        if status and r["classification_status"] != status:
            return False
        if ql and ql not in r["domain"].lower() and ql not in (r["title"] or "").lower():
            return False
        return True

    filtered = [r for r in rows if keep(r)]
    domains: dict[str, dict] = {}
    hourly = [{"focus": 0, "learn": 0, "break": 0, "distract": 0, "other": 0} for _ in range(24)]
    from models import ui_group_for
    for r in filtered:
        s, e, sec = at._clip(r, ws, we)
        d = domains.setdefault(r["domain"], {"domain": r["domain"], "seconds": 0, "visits": 0, "category": r["category"]})
        d["seconds"] += sec
        d["visits"] += 1
        cur = s
        while cur < e:
            local = cur.astimezone(tz)
            nxt = min(e, (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timeutil.UTC))
            g = ui_group_for(r["category"] if r["classification_status"] == "classified" else None, bool(r["is_wasted"]))
            hourly[local.hour][g] += int((nxt - cur).total_seconds())
            cur = nxt
    filtered.sort(key=lambda r: r["start_time"], reverse=True)
    page = filtered[offset: offset + limit]
    return {
        "range": {"spec": spec, "label": label, "start": timeutil.to_iso(ws), "end": timeutil.to_iso(we), "timezone": name},
        "total_sessions": len(filtered),
        "sessions": [session_dict(r, tz) for r in page],
        "domains": sorted(domains.values(), key=lambda d: -d["seconds"])[:25],
        "hourly": hourly,
        "coverage": m["coverage"],
        "totals": {"total": m["total_seconds"], "wasted": m["wasted_seconds"], "estimated": m["estimated_seconds"],
                   "measured": m["measured_seconds"]},
    }


@router.get("/insights")
def insights(days: int = Query(14, ge=1, le=90), spec: str = Query("last_7_days", alias="range")) -> dict:
    tz, name = tz_and_name()
    today = timeutil.today_local(tz)
    first = today - timedelta(days=days - 1)
    daily = at.ensure_daily_range(first, today, name)
    months = []
    for mk in monthly_analysis.data_months(name):
        monthly_analysis.refresh_month(mk, name)
    months = at.list_monthly()
    ws, we, label, _, _ = parse_range(spec)
    return {
        "timezone": name,
        "daily": [d for d in reversed(daily) if d["total_seconds"] > 0],
        "monthly": list(reversed([m for m in months if m["total_seconds"] > 0])),
        "cards": at.insight_cards(storage.sessions_between(ws, we), ws, we, tz),
        "cards_range": label,
        "waiting_days": sum(1 for d in daily if d["analysis_status"] in ("waiting", "partial")),
    }


def _first_data_day(tz) -> Optional[date]:
    """Local date of the earliest stored session (no point computing empty days before it)."""
    from database import read_connection
    with read_connection() as c:
        r = c.execute("SELECT MIN(start_time) AS a FROM activity_sessions").fetchone()
    return timeutil.local_date(timeutil.parse_iso(r["a"]), tz) if r and r["a"] else None


@router.get("/trends")
def trends(days: int = Query(60, ge=1, le=400)) -> dict:
    tz, name = tz_and_name()
    today = timeutil.today_local(tz)
    first = max(today - timedelta(days=days - 1), _first_data_day(tz) or today)
    daily = at.ensure_daily_range(first, today, name)
    act = storage.get_activation_ts()
    series = []
    cat_totals: dict[str, int] = defaultdict(int)
    for d in daily:
        cats = d["category_seconds"]
        for c, s in cats.items():
            cat_totals[c] += s
        series.append({
            "day": d["day"], "total": d["total_seconds"],
            "productive": sum(s for c, s in cats.items() if c in PRODUCTIVE_CATEGORIES),
            "learning": sum(cats.get(c, 0) for c in UI_GROUPS["learn"]),
            "focus": sum(cats.get(c, 0) for c in UI_GROUPS["focus"]),
            "wasted": d["wasted_seconds"], "estimated": d["estimated_seconds"], "measured": d["measured_seconds"],
            "unclassified": d["unclassified_seconds"], "categories": cats, "status": d["analysis_status"],
        })
    months = []
    for mk in monthly_analysis.data_months(name):
        agg, _ = monthly_analysis.refresh_month(mk, name)
        months.append({"month": mk, "total": agg["total_seconds"], "wasted": agg["wasted_seconds"],
                       "estimated": agg["estimated_seconds"], "measured": agg["measured_seconds"],
                       "categories": agg["category_seconds"], "days": agg["days_count"],
                       "productive": sum(s for c, s in agg["category_seconds"].items() if c in PRODUCTIVE_CATEGORIES)})
    return {
        "timezone": name, "first_day": first.isoformat(), "last_day": today.isoformat(),
        "activation_day": timeutil.local_date(act, tz).isoformat() if act else None,
        "daily": series, "monthly": months,
        "category_totals": dict(sorted(cat_totals.items(), key=lambda kv: -kv[1])),
    }
