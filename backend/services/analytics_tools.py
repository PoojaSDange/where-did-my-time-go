"""Deterministic analytics. PYTHON/SQL DOES ALL ARITHMETIC; the LLM only writes narrative.

Used by: the website APIs, daily/monthly summaries, and the supervisor agent's tools.
Every metric keeps history_estimated and extension_measured separate.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import database as db
from models import CATEGORIES, PRODUCTIVE_CATEGORIES, SOURCE_HISTORY, SOURCE_LIVE
from services import activity_storage as storage
from services import privacy, timeutil

SOURCES = (SOURCE_HISTORY, SOURCE_LIVE)


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"


# --------------------------------------------------------------------------
# core metric computation
# --------------------------------------------------------------------------
def _clip(row, ws: datetime, we: datetime) -> tuple[datetime, datetime, int]:
    s = max(timeutil.parse_iso(row["start_time"]), ws)
    e = min(timeutil.parse_iso(row["end_time"]), we)
    return s, e, max(0, int((e - s).total_seconds()))


def _empty_source() -> dict:
    return {"total": 0, "wasted": 0, "categories": {}}


def compute_metrics(rows: Iterable, ws: datetime, we: datetime) -> dict:
    """Aggregate rows over [ws, we). Sessions are clipped to the window."""
    total = wasted = unclassified = 0
    cats: dict[str, int] = defaultdict(int)
    by_source = {s: _empty_source() for s in SOURCES}
    distractions: dict[str, dict] = {}
    n = classified = pending = failed = 0
    pending_s = failed_s = 0
    for r in rows:
        _, _, sec = _clip(r, ws, we)
        if sec <= 0:
            continue
        n += 1
        total += sec
        src = by_source[r["source"]]
        src["total"] += sec
        status = r["classification_status"]
        if status == "classified" and r["category"]:
            classified += 1
            cats[r["category"]] += sec
            src["categories"][r["category"]] = src["categories"].get(r["category"], 0) + sec
        elif status == "failed":
            failed += 1
            failed_s += sec
            unclassified += sec
        else:
            pending += 1
            pending_s += sec
            unclassified += sec
        if r["is_wasted"] and status == "classified":
            wasted += sec
            src["wasted"] += sec
            d = distractions.setdefault(r["domain"], {"domain": r["domain"], "seconds": 0, "sessions": 0})
            d["seconds"] += sec
            d["sessions"] += 1

    top_cats = [
        {"category": c, "seconds": s, "pct": round(100.0 * s / total, 1) if total else 0.0}
        for c, s in sorted(cats.items(), key=lambda kv: -kv[1])[:5]
    ]
    top_dis = sorted(distractions.values(), key=lambda d: -d["seconds"])[:5]
    return {
        "total_seconds": total,
        "wasted_seconds": wasted,
        "estimated_seconds": by_source[SOURCE_HISTORY]["total"],
        "measured_seconds": by_source[SOURCE_LIVE]["total"],
        "unclassified_seconds": unclassified,
        "category_seconds": dict(cats),
        "by_source": by_source,
        "top_categories": top_cats,
        "top_distractions": top_dis,
        "coverage": {
            "sessions": n, "classified": classified, "pending": pending, "failed": failed,
            "pending_seconds": pending_s, "failed_seconds": failed_s,
            "classified_pct": round(100.0 * (total - unclassified) / total, 1) if total else 100.0,
        },
    }


def metrics_for_window(ws: datetime, we: datetime) -> dict:
    return compute_metrics(storage.sessions_between(ws, we), ws, we)


def productive_seconds(metrics: dict) -> int:
    return sum(s for c, s in metrics["category_seconds"].items() if c in PRODUCTIVE_CATEGORIES)


# --------------------------------------------------------------------------
# summary tables
# --------------------------------------------------------------------------
def _j(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def initial_daily_status(day: date, today: date, metrics: dict, existing: Optional[dict]) -> str:
    if metrics["total_seconds"] == 0:
        return "no_data"
    if metrics["measured_seconds"] == 0:
        return "deterministic_only"
    if day >= today:
        return "in_progress"
    if existing and existing["analysis_status"] in ("complete", "partial"):
        # Late upload changed the day's total => the narrative is stale and must be redone.
        if existing["total_seconds"] != metrics["total_seconds"] and existing["analysis_status"] == "complete":
            return "waiting"
        return existing["analysis_status"]
    return "waiting"


def upsert_daily(day: date, tz_name: str, metrics: dict, today: date) -> str:
    """Store deterministic metrics (never touches the AI narrative unless it became stale)."""
    key = day.isoformat()
    with db.connection() as c:
        existing = db.row_to_dict(c.execute("SELECT * FROM daily_summaries WHERE day=?", (key,)).fetchone())
        status = initial_daily_status(day, today, metrics, existing)
        reset_ai = bool(existing and existing["analysis_status"] == "complete" and status == "waiting")
        has_measured = 1 if metrics["measured_seconds"] > 0 else 0
        c.execute(
            """INSERT INTO daily_summaries
               (day, timezone, total_seconds, wasted_seconds, estimated_seconds, measured_seconds, unclassified_seconds,
                category_seconds, by_source, top_categories, top_distractions, coverage, has_measured,
                analysis_status, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
               ON CONFLICT(day) DO UPDATE SET
                 timezone=excluded.timezone, total_seconds=excluded.total_seconds, wasted_seconds=excluded.wasted_seconds,
                 estimated_seconds=excluded.estimated_seconds, measured_seconds=excluded.measured_seconds,
                 unclassified_seconds=excluded.unclassified_seconds, category_seconds=excluded.category_seconds,
                 by_source=excluded.by_source, top_categories=excluded.top_categories,
                 top_distractions=excluded.top_distractions, coverage=excluded.coverage,
                 has_measured=excluded.has_measured, analysis_status=excluded.analysis_status,
                 computed_at=excluded.computed_at""",
            (key, tz_name, metrics["total_seconds"], metrics["wasted_seconds"], metrics["estimated_seconds"],
             metrics["measured_seconds"], metrics["unclassified_seconds"], _j(metrics["category_seconds"]),
             _j(metrics["by_source"]), _j(metrics["top_categories"]), _j(metrics["top_distractions"]),
             _j(metrics["coverage"]), has_measured, status),
        )
        if reset_ai:
            c.execute("UPDATE daily_summaries SET partial_observations='[]' WHERE day=?", (key,))
    return status


def refresh_day(day: date, tz_name: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Recompute + store the deterministic summary of one LOCAL day. Returns the metrics."""
    tz_name = tz_name or storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    ws, we = timeutil.local_day_bounds(day, tz)
    m = metrics_for_window(ws, we)
    upsert_daily(day, tz_name, m, timeutil.today_local(tz, now))
    return m


def get_daily(day: date | str) -> Optional[dict]:
    key = day if isinstance(day, str) else day.isoformat()
    with db.read_connection() as c:
        return db.row_to_dict(c.execute("SELECT * FROM daily_summaries WHERE day=?", (key,)).fetchone())


def list_daily(first: date, last: date) -> list[dict]:
    with db.read_connection() as c:
        rows = c.execute(
            "SELECT * FROM daily_summaries WHERE day BETWEEN ? AND ? ORDER BY day",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
    return [db.row_to_dict(r) for r in rows]  # type: ignore[misc]


def ensure_daily_range(first: date, last: date, tz_name: str, now: Optional[datetime] = None) -> list[dict]:
    """Return daily summaries for [first,last]; (re)compute missing days and today."""
    tz = timeutil.get_tz(tz_name)
    today = timeutil.today_local(tz, now)
    have = {d["day"]: d for d in list_daily(first, last)}
    for d in timeutil.iter_days(first, last):
        if d.isoformat() not in have or d >= today:
            refresh_day(d, tz_name, now)
    return list_daily(first, last)


def upsert_monthly(month: str, tz_name: str, days: list[dict], status: Optional[str] = None) -> dict:
    """Aggregate DAILY summaries into a monthly row (deterministic)."""
    agg = aggregate_daily_rows(days)
    with db.connection() as c:
        existing = db.row_to_dict(c.execute("SELECT * FROM monthly_summaries WHERE month=?", (month,)).fetchone())
        st = status or (existing["analysis_status"] if existing else "in_progress")
        c.execute(
            """INSERT INTO monthly_summaries
               (month, timezone, days_count, total_seconds, wasted_seconds, estimated_seconds, measured_seconds,
                unclassified_seconds, category_seconds, by_source, top_categories, top_distractions, coverage,
                analysis_status, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))
               ON CONFLICT(month) DO UPDATE SET
                 timezone=excluded.timezone, days_count=excluded.days_count, total_seconds=excluded.total_seconds,
                 wasted_seconds=excluded.wasted_seconds, estimated_seconds=excluded.estimated_seconds,
                 measured_seconds=excluded.measured_seconds, unclassified_seconds=excluded.unclassified_seconds,
                 category_seconds=excluded.category_seconds, by_source=excluded.by_source,
                 top_categories=excluded.top_categories, top_distractions=excluded.top_distractions,
                 coverage=excluded.coverage, analysis_status=excluded.analysis_status,
                 computed_at=excluded.computed_at""",
            (month, tz_name, agg["days_count"], agg["total_seconds"], agg["wasted_seconds"], agg["estimated_seconds"],
             agg["measured_seconds"], agg["unclassified_seconds"], _j(agg["category_seconds"]), _j(agg["by_source"]),
             _j(agg["top_categories"]), _j(agg["top_distractions"]), _j(agg["coverage"]), st),
        )
    return agg


def aggregate_daily_rows(days: list[dict]) -> dict:
    total = wasted = est = meas = uncl = 0
    cats: dict[str, int] = defaultdict(int)
    by_source = {s: _empty_source() for s in SOURCES}
    dis: dict[str, dict] = {}
    cov = {"sessions": 0, "classified": 0, "pending": 0, "failed": 0, "pending_seconds": 0, "failed_seconds": 0}
    active_days = 0
    for d in days:
        if d["total_seconds"] > 0:
            active_days += 1
        total += d["total_seconds"]
        wasted += d["wasted_seconds"]
        est += d["estimated_seconds"]
        meas += d["measured_seconds"]
        uncl += d["unclassified_seconds"]
        for c, s in (d["category_seconds"] or {}).items():
            cats[c] += s
        for src, v in (d["by_source"] or {}).items():
            t = by_source.setdefault(src, _empty_source())
            t["total"] += v.get("total", 0)
            t["wasted"] += v.get("wasted", 0)
            for c, s in (v.get("categories") or {}).items():
                t["categories"][c] = t["categories"].get(c, 0) + s
        for x in d["top_distractions"] or []:
            e = dis.setdefault(x["domain"], {"domain": x["domain"], "seconds": 0, "sessions": 0})
            e["seconds"] += x["seconds"]
            e["sessions"] += x["sessions"]
        for k in cov:
            cov[k] += (d["coverage"] or {}).get(k, 0)
    cov["classified_pct"] = round(100.0 * (total - uncl) / total, 1) if total else 100.0
    return {
        "days_count": active_days,
        "total_seconds": total, "wasted_seconds": wasted, "estimated_seconds": est, "measured_seconds": meas,
        "unclassified_seconds": uncl, "category_seconds": dict(cats), "by_source": by_source,
        "top_categories": [
            {"category": c, "seconds": s, "pct": round(100.0 * s / total, 1) if total else 0.0}
            for c, s in sorted(cats.items(), key=lambda kv: -kv[1])[:5]
        ],
        "top_distractions": sorted(dis.values(), key=lambda x: -x["seconds"])[:5],
        "coverage": cov,
    }


def get_monthly(month: str) -> Optional[dict]:
    with db.read_connection() as c:
        return db.row_to_dict(c.execute("SELECT * FROM monthly_summaries WHERE month=?", (month,)).fetchone())


def list_monthly() -> list[dict]:
    with db.read_connection() as c:
        return [db.row_to_dict(r) for r in c.execute("SELECT * FROM monthly_summaries ORDER BY month")]  # type: ignore[misc]


def refresh_days_for_override(days: list[str], tz_name: str) -> None:
    for d in days:
        refresh_day(date.fromisoformat(d), tz_name)


# --------------------------------------------------------------------------
# insight cards (deterministic, per range)
# --------------------------------------------------------------------------
def insight_cards(rows: list, ws: datetime, we: datetime, tz: ZoneInfo) -> dict:
    hour_focus: dict[int, int] = defaultdict(int)
    prod_sessions = []
    dom_wasted: dict[str, int] = defaultdict(int)
    learning = wasted = 0
    for r in rows:
        s, e, sec = _clip(r, ws, we)
        if sec <= 0 or r["classification_status"] != "classified":
            continue
        cat = r["category"]
        if cat in PRODUCTIVE_CATEGORIES:
            prod_sessions.append((s, e, r["domain"]))
            cur = s
            while cur < e:  # spread over local hours
                nxt = min(e, (cur.astimezone(tz).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timeutil.UTC))
                hour_focus[cur.astimezone(tz).hour] += int((nxt - cur).total_seconds())
                cur = nxt
        if cat in ("learning", "research"):
            learning += sec
        if r["is_wasted"]:
            wasted += sec
            dom_wasted[r["domain"]] += sec

    best_window = None
    if hour_focus:
        best_h = max(range(24), key=lambda h: sum(hour_focus.get((h + i) % 24, 0) for i in range(3)))
        best_window = {"start_hour": best_h, "end_hour": (best_h + 3) % 24,
                       "seconds": sum(hour_focus.get((best_h + i) % 24, 0) for i in range(3))}
    # longest uninterrupted productive run (consecutive productive sessions, gaps <= 2 min)
    prod_sessions.sort()
    longest: Optional[dict] = None
    runs: list[tuple[datetime, datetime, str]] = []
    for s, e, d in prod_sessions:
        if runs and (s - runs[-1][1]).total_seconds() <= 120:
            rs, re_, rd = runs[-1]
            runs[-1] = (rs, max(re_, e), rd)
        else:
            runs.append((s, e, d))
    for rs, re_, rd in runs:
        secs = int((re_ - rs).total_seconds())
        if longest is None or secs > longest["seconds"]:
            longest = {"seconds": secs, "domain": rd, "start": timeutil.to_iso(rs)}
    top_dis = max(dom_wasted.items(), key=lambda kv: kv[1]) if dom_wasted else None
    return {
        "most_productive_window": best_window,
        "biggest_distraction": {"domain": top_dis[0], "seconds": top_dis[1]} if top_dis else None,
        "longest_focus": longest,
        "learning_seconds": learning,
        "wasted_seconds": wasted,
        "learning_to_wasted_ratio": round(learning / wasted, 2) if wasted else None,
    }


# --------------------------------------------------------------------------
# supervisor-agent tools: compact evidence, estimated vs measured LABELLED, no invented numbers
# --------------------------------------------------------------------------
def _range(spec: str, tz: ZoneInfo, now: Optional[datetime] = None):
    return timeutil.resolve_range(spec, tz, now)


def _label(m: dict) -> str:
    return (f"{fmt_duration(m['measured_seconds'])} measured (extension) + "
            f"{fmt_duration(m['estimated_seconds'])} estimated (browser history)")


def _kinds(measured_seconds: float, estimated_seconds: float) -> dict:
    """Which data kinds actually contributed to a tool result (drives the agent's disclosure guard)."""
    return {"measured": bool(measured_seconds), "estimated": bool(estimated_seconds)}


def _cat_check(category: str) -> Optional[dict]:
    if category not in CATEGORIES:
        return {"error": f"unknown category '{category}'", "valid_categories": list(CATEGORIES)}
    return None


def tool_get_category_time(range: str, category: str, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    bad = _cat_check(category)
    if bad:
        return bad
    ws, we, label = _range(range, tz, now)
    rows = storage.sessions_between(ws, we)
    m = compute_metrics(rows, ws, we)
    secs = m["category_seconds"].get(category, 0)
    per_source = {s: m["by_source"][s]["categories"].get(category, 0) for s in SOURCES}
    per_day: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["category"] == category and r["classification_status"] == "classified":
            s, e, sec = _clip(r, ws, we)
            per_day[timeutil.local_date(s, tz).isoformat()] += sec
    days = [{"date": d, "seconds": s, "human": fmt_duration(s)} for d, s in sorted(per_day.items())][-31:]
    return {
        "data": {
            "range": label, "category": category, "seconds": secs, "human": fmt_duration(secs),
            "measured_seconds": per_source[SOURCE_LIVE], "estimated_seconds": per_source[SOURCE_HISTORY],
            "pct_of_all_tracked_time": round(100.0 * secs / m["total_seconds"], 1) if m["total_seconds"] else 0.0,
            "total_tracked": fmt_duration(m["total_seconds"]), "per_day": days,
            "unclassified": fmt_duration(m["unclassified_seconds"]),
        },
        "evidence": [f"{category} in {label}: {fmt_duration(secs)} "
                     f"(measured {fmt_duration(per_source[SOURCE_LIVE])}, estimated {fmt_duration(per_source[SOURCE_HISTORY])})"],
        "kinds": _kinds(per_source[SOURCE_LIVE], per_source[SOURCE_HISTORY]),
    }


def tool_top_distractions(range: str, limit: int = 5, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    ws, we, label = _range(range, tz, now)
    rows = storage.sessions_between(ws, we)
    m = compute_metrics(rows, ws, we)
    limit = max(1, min(int(limit or 5), 10))
    wasted_domains: dict[str, dict] = {}
    social_ent: dict[str, dict] = {}
    for r in rows:
        if r["classification_status"] != "classified":
            continue
        s, e, sec = _clip(r, ws, we)
        if sec <= 0:
            continue
        target = wasted_domains if r["is_wasted"] else (social_ent if r["category"] in ("social_media", "entertainment") else None)
        if target is None:
            continue
        name = "[sensitive]" if r["sensitive"] else r["domain"]
        d = target.setdefault(name, {"domain": name, "seconds": 0, "sessions": 0, "measured": 0, "estimated": 0})
        d["seconds"] += sec
        d["sessions"] += 1
        d["measured" if r["source"] == SOURCE_LIVE else "estimated"] += sec

    def shape(ds):
        return [{**d, "human": fmt_duration(d["seconds"])} for d in sorted(ds.values(), key=lambda x: -x["seconds"])[:limit]]

    out = {
        "range": label,
        "wasted_total": fmt_duration(m["wasted_seconds"]),
        "wasted_by_domain": shape(wasted_domains),
        "other_social_and_entertainment_by_domain": shape(social_ent),
        "note": ("'wasted' = social/entertainment sessions flagged as distracting with enough evidence; "
                 "other social/entertainment time is listed separately and is NOT counted as wasted."),
        "data_split": _label(m),
    }
    ev = [f"{d['domain']}: {d['human']} wasted" for d in out["wasted_by_domain"][:3]]
    used = list(wasted_domains.values()) + list(social_ent.values())
    return {"data": out, "evidence": ev or [f"No wasted time flagged in {label}"],
            "kinds": _kinds(sum(d["measured"] for d in used), sum(d["estimated"] for d in used))}


def tool_top_domains(range: str, category: Optional[str] = None, limit: int = 5, *,
                     tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    if category:
        bad = _cat_check(category)
        if bad:
            return bad
    ws, we, label = _range(range, tz, now)
    agg: dict[str, int] = defaultdict(int)
    kind_secs = {SOURCE_LIVE: 0, SOURCE_HISTORY: 0}
    for r in storage.sessions_between(ws, we):
        if r["classification_status"] != "classified" or (category and r["category"] != category):
            continue
        _, _, sec = _clip(r, ws, we)
        kind_secs[r["source"]] += sec
        agg["[sensitive]" if r["sensitive"] else r["domain"]] += sec
    top = sorted(agg.items(), key=lambda kv: -kv[1])[: max(1, min(int(limit or 5), 10))]
    rows = [{"domain": d, "seconds": s, "human": fmt_duration(s)} for d, s in top]
    return {"data": {"range": label, "category": category or "all", "top_domains": rows},
            "evidence": [f"{r['domain']}: {r['human']}" for r in rows[:3]],
            "kinds": _kinds(kind_secs[SOURCE_LIVE], kind_secs[SOURCE_HISTORY])}


def tool_compare_periods(range_a: str, range_b: str, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    out = {}
    for key, spec in (("a", range_a), ("b", range_b)):
        ws, we, label = _range(spec, tz, now)
        m = metrics_for_window(ws, we)
        out[key] = {"range": label, "total": m["total_seconds"], "wasted": m["wasted_seconds"],
                    "measured": m["measured_seconds"], "estimated": m["estimated_seconds"],
                    "categories": m["category_seconds"]}
    cats = sorted(set(out["a"]["categories"]) | set(out["b"]["categories"]))
    diff = []
    for c in cats:
        a, b = out["a"]["categories"].get(c, 0), out["b"]["categories"].get(c, 0)
        diff.append({"category": c, "a": fmt_duration(a), "b": fmt_duration(b),
                     "change_seconds": a - b,
                     "change": ("+" if a - b >= 0 else "-") + fmt_duration(abs(a - b))})
    diff.sort(key=lambda d: -abs(d["change_seconds"]))
    data = {
        "a": {"range": out["a"]["range"], "total": fmt_duration(out["a"]["total"]), "wasted": fmt_duration(out["a"]["wasted"]),
              "measured": fmt_duration(out["a"]["measured"]), "estimated": fmt_duration(out["a"]["estimated"])},
        "b": {"range": out["b"]["range"], "total": fmt_duration(out["b"]["total"]), "wasted": fmt_duration(out["b"]["wasted"]),
              "measured": fmt_duration(out["b"]["measured"]), "estimated": fmt_duration(out["b"]["estimated"])},
        "category_changes_a_minus_b": diff[:8],
        "note": "'a minus b': positive = more time in period a. Mixed measured/estimated periods are not strictly comparable.",
    }
    return {"data": data, "evidence": [
        f"{data['a']['range']}: {data['a']['total']} total, {data['a']['wasted']} wasted",
        f"{data['b']['range']}: {data['b']['total']} total, {data['b']['wasted']} wasted"],
        "kinds": _kinds(out["a"]["measured"] + out["b"]["measured"], out["a"]["estimated"] + out["b"]["estimated"])}


def tool_get_daily_summary(date_str: str, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        return {"error": "date must be YYYY-MM-DD (local date)"}
    tz_name = str(tz)
    ensure_daily_range(day, day, tz_name)
    d = get_daily(day)
    if not d or d["total_seconds"] == 0:
        return {"data": {"date": date_str, "note": "no tracked activity that day"}, "evidence": [f"No activity on {date_str}"]}
    ai = d["ai_analysis"] if isinstance(d["ai_analysis"], dict) else None
    data = {
        "date": date_str, "total": fmt_duration(d["total_seconds"]), "wasted": fmt_duration(d["wasted_seconds"]),
        "measured": fmt_duration(d["measured_seconds"]), "estimated": fmt_duration(d["estimated_seconds"]),
        "unclassified": fmt_duration(d["unclassified_seconds"]),
        "categories": {c: fmt_duration(s) for c, s in sorted(d["category_seconds"].items(), key=lambda kv: -kv[1])[:8]},
        "top_distractions": [{"domain": x["domain"], "time": fmt_duration(x["seconds"])} for x in d["top_distractions"][:3]],
        "analysis_status": d["analysis_status"], "coverage_pct": d["coverage"].get("classified_pct"),
        "ai_summary": (ai or {}).get("summary"),
        "data_kind": "measured" if d["measured_seconds"] and not d["estimated_seconds"] else
                     ("estimated (from browser history)" if not d["measured_seconds"] else "mixed measured + estimated"),
    }
    return {"data": data, "evidence": [f"{date_str}: {data['total']} total, {data['wasted']} wasted ({data['data_kind']})"],
            "kinds": _kinds(d["measured_seconds"], d["estimated_seconds"])}


def tool_get_monthly_summary(month: str, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    try:
        first, last = timeutil.month_bounds(month, tz)
    except (ValueError, IndexError):
        return {"error": "month must be YYYY-MM"}
    ensure_daily_range(first, last, str(tz))
    upsert_monthly(month, str(tz), list_daily(first, last))
    m = get_monthly(month)
    if not m or m["total_seconds"] == 0:
        return {"data": {"month": month, "note": "no tracked activity"}, "evidence": [f"No activity in {month}"]}
    ai = m["ai_analysis"] if isinstance(m["ai_analysis"], dict) else None
    data = {
        "month": month, "total": fmt_duration(m["total_seconds"]), "wasted": fmt_duration(m["wasted_seconds"]),
        "measured": fmt_duration(m["measured_seconds"]), "estimated": fmt_duration(m["estimated_seconds"]),
        "categories": {c: fmt_duration(s) for c, s in sorted(m["category_seconds"].items(), key=lambda kv: -kv[1])[:8]},
        "analysis_status": m["analysis_status"], "ai_summary": (ai or {}).get("summary"),
    }
    return {"data": data, "evidence": [f"{month}: {data['total']} total, {data['wasted']} wasted"],
            "kinds": _kinds(m["measured_seconds"], m["estimated_seconds"])}


def tool_search_activity(query: str, range: str = "last_7_days", limit: int = 8, *,
                         tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return {"error": "query too short"}
    ws, we, label = _range(range, tz, now)
    agg: dict[str, dict] = {}
    for r in storage.sessions_between(ws, we):
        if r["sensitive"]:
            continue  # never expose sensitive titles/URLs to the LLM, even when searched for
        title = privacy.redact_title(r["title"])
        if q not in r["domain"].lower() and q not in title.lower():
            continue
        _, _, sec = _clip(r, ws, we)
        a = agg.setdefault(r["signature"] or r["id"], {
            "domain": r["domain"], "title": title, "category": r["category"] or "unclassified",
            "seconds": 0, "visits": 0, "measured": 0, "estimated": 0})
        a["seconds"] += sec
        a["visits"] += 1
        a["measured" if r["source"] == SOURCE_LIVE else "estimated"] += sec
    top = sorted(agg.values(), key=lambda x: -x["seconds"])[: max(1, min(int(limit or 8), 15))]
    for t in top:
        t["human"] = fmt_duration(t["seconds"])
    total = sum(a["seconds"] for a in agg.values())
    return {"data": {"range": label, "query": q, "matches": top, "total_matching": fmt_duration(total),
                     "distinct_pages": len(agg)},
            "evidence": [f"'{q}' in {label}: {fmt_duration(total)} across {len(agg)} pages"],
            "kinds": _kinds(sum(a["measured"] for a in agg.values()), sum(a["estimated"] for a in agg.values()))}


def tool_measured_vs_estimated(range: str, *, tz: ZoneInfo, now: Optional[datetime] = None) -> dict:
    ws, we, label = _range(range, tz, now)
    m = metrics_for_window(ws, we)
    data = {
        "range": label,
        "measured": {"seconds": m["measured_seconds"], "human": fmt_duration(m["measured_seconds"]),
                     "meaning": "tracked live by the extension (accurate)"},
        "estimated": {"seconds": m["estimated_seconds"], "human": fmt_duration(m["estimated_seconds"]),
                      "meaning": "reconstructed from browser history before activation (approximate, conservative)"},
        "unclassified": fmt_duration(m["unclassified_seconds"]),
        "coverage_pct": m["coverage"]["classified_pct"],
    }
    return {"data": data, "evidence": [f"{label}: {_label(m)}"],
            "kinds": _kinds(m["measured_seconds"], m["estimated_seconds"])}


TOOL_FUNCS = {
    "get_category_time": tool_get_category_time,
    "top_distractions": tool_top_distractions,
    "top_domains": tool_top_domains,
    "compare_periods": tool_compare_periods,
    "get_daily_summary": tool_get_daily_summary,
    "get_monthly_summary": tool_get_monthly_summary,
    "search_activity": tool_search_activity,
    "measured_vs_estimated": tool_measured_vs_estimated,
}
