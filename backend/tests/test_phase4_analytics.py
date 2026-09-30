import json
from datetime import date, timedelta

import pytest

import database as db
from config import settings
from conftest import dt, make_chrome_history
from services import activity_storage as storage
from services import analytics_tools as at
from services import bootstrap, catchup, daily_analysis, groq_client, monthly_analysis, timeutil
from services.history_reader import HistoryReader

TZ = "Asia/Kolkata"
TZI = timeutil.get_tz(TZ)
NOW = dt("2026-09-29T06:00:00Z")          # 11:30 IST on 2026-09-29


def add(i, start, dur, domain, cat, source="extension_measured", wasted=0, status="classified", title="t", sensitive=0):
    s = dt(start)
    with db.connection() as c:
        c.execute(
            """INSERT INTO activity_sessions (id,start_time,end_time,domain,url,title,category,duration,is_wasted,confidence,
               source,classification_status,activity_key,signature,sensitive)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"row-{i:06d}", timeutil.to_iso(s), timeutil.to_iso(s + timedelta(seconds=dur)), domain,
             f"https://{domain}/x", title, cat if status == "classified" else None, dur, wasted, 0.9, source, status,
             f"row-{i:06d}", f"sig{i}", sensitive))


def seed_day():
    # IST 2026-09-27 = 2026-09-26T18:30Z .. 2026-09-27T18:30Z
    add(1, "2026-09-27T04:00:00Z", 3600, "github.com", "focused_work")
    add(2, "2026-09-27T05:10:00Z", 1800, "youtube.com", "learning")
    add(3, "2026-09-27T06:00:00Z", 1500, "instagram.com", "social_media", wasted=1)
    add(4, "2026-09-27T07:00:00Z", 600, "x.io", "entertainment", source="history_estimated")
    add(5, "2026-09-27T08:00:00Z", 300, "pending.io", None, status="pending")
    add(6, "2026-09-27T09:00:00Z", 200, "failed.io", None, status="failed")
    add(7, "2026-09-27T10:00:00Z", 100, "hsbc.com", "personal", sensitive=1)


def test_metrics_are_deterministic_and_separate_estimated_from_measured():
    seed_day()
    m = at.refresh_day(date(2026, 9, 27), TZ, NOW)
    assert m["total_seconds"] == 3600 + 1800 + 1500 + 600 + 300 + 200 + 100
    assert m["measured_seconds"] == m["total_seconds"] - 600 and m["estimated_seconds"] == 600
    assert m["wasted_seconds"] == 1500 and m["unclassified_seconds"] == 500
    assert m["category_seconds"]["learning"] == 1800 and "pending" not in m["category_seconds"]
    assert m["by_source"]["history_estimated"]["categories"] == {"entertainment": 600}
    assert m["coverage"]["pending"] == 1 and m["coverage"]["failed"] == 1 and m["coverage"]["classified_pct"] < 100
    assert m["top_distractions"][0]["domain"] == "instagram.com"


def test_windows_clip_sessions_and_day_uses_local_boundaries():
    # session 2026-09-26T18:00Z..19:00Z crosses IST midnight (18:30Z): half belongs to Sep 26, half to Sep 27
    add(1, "2026-09-26T18:00:00Z", 3600, "a.io", "focused_work")
    d26 = at.refresh_day(date(2026, 9, 26), TZ, NOW); d27 = at.refresh_day(date(2026, 9, 27), TZ, NOW)
    assert d26["total_seconds"] == 1800 and d27["total_seconds"] == 1800


def test_daily_status_rules():
    seed_day()
    today = date(2026, 9, 29)
    assert at.refresh_day(date(2026, 9, 27), TZ, NOW) and at.get_daily("2026-09-27")["analysis_status"] == "waiting"
    at.refresh_day(date(2026, 9, 29), TZ, NOW)
    assert at.get_daily("2026-09-29")["analysis_status"] == "no_data"
    add(50, "2026-09-29T04:00:00Z", 60, "a.io", "focused_work")
    at.refresh_day(date(2026, 9, 29), TZ, NOW)
    assert at.get_daily("2026-09-29")["analysis_status"] == "in_progress"       # not ended yet: never analysed mid-day
    add(51, "2026-09-20T04:00:00Z", 60, "h.io", "learning", source="history_estimated")
    at.refresh_day(date(2026, 9, 20), TZ, NOW)
    assert at.get_daily("2026-09-20")["analysis_status"] == "deterministic_only"  # history days: no per-day LLM


def fake_llm_factory(log):
    def llm(system, user, prio, max_tokens):
        log.append((system[:12], prio))
        if system.startswith("You analyse"):
            return "- long GitHub session at the start of the day\n- Instagram in the afternoon"
        return json.dumps({"summary": "You had a focused morning.", "highlights": ["GitHub 1h"], "distractions": ["Instagram"],
                           "suggestion": "Block 4pm.", "confidence": "high"})
    return llm


def test_daily_analysis_waits_for_pending_then_completes_once():
    seed_day()
    log = []
    st = daily_analysis.analyze_day(date(2026, 9, 27), TZ, fake_llm_factory(log), NOW)
    assert st == "waiting" and log == []                             # pending row => no LLM call, gap recorded
    with db.connection() as c:                                       # the pending row gets classified/failed
        c.execute("update activity_sessions set classification_status='failed' where classification_status='pending'")
    assert daily_analysis.analyze_day(date(2026, 9, 27), TZ, fake_llm_factory(log), NOW) == "complete"
    d = at.get_daily("2026-09-27")
    assert d["ai_analysis"]["summary"] and d["partial_observations"] == [] and d["analysis_status"] == "complete"
    n = len(log)
    assert daily_analysis.analyze_day(date(2026, 9, 27), TZ, fake_llm_factory(log), NOW) == "complete" and len(log) == n  # once only


def test_daily_analysis_today_is_never_analysed():
    add(1, "2026-09-29T04:00:00Z", 600, "github.com", "focused_work")
    log = []
    assert daily_analysis.analyze_day(date(2026, 9, 29), TZ, fake_llm_factory(log), NOW) == "in_progress" and log == []


def test_evidence_includes_short_sessions_aggregated_and_hides_sensitive():
    for i in range(6):
        add(i, f"2026-09-27T04:{i:02d}:00Z", 15, "news.io", "news", title="Same headline")   # 15s sessions, same signature? no: sig differs
    with db.connection() as c:
        c.execute("update activity_sessions set signature='SAME'")
    add(90, "2026-09-27T05:00:00Z", 100, "hsbc.com", "personal", sensitive=1, title="My balance 12345678")
    ev = daily_analysis.build_evidence(date(2026, 9, 27), TZ)
    news = [e for e in ev if e.get("d") == "news.io"][0]
    assert news["n"] == 6 and news["s"] == 90                        # short sessions kept + aggregated
    blob = json.dumps(ev)
    assert "hsbc" not in blob and "balance" not in blob and "[sensitive]" in blob


def test_budget_exhaustion_saves_partial_and_resumes_without_redoing_batches(monkeypatch):
    monkeypatch.setattr(settings, "analysis_batch_tokens", 700)    # force several batches
    for i in range(40):
        add(i, f"2026-09-27T{4 + i // 20:02d}:{(i % 20) * 2:02d}:00Z", 90, f"site{i}.com", "research", title=f"A rather long article title number {i}")
    calls = {"obs": 0}
    def llm(system, user, prio, mt):
        if system.startswith("You analyse"):
            calls["obs"] += 1
            if calls["obs"] == 3 and not calls.get("resumed"):
                raise groq_client.BudgetExhausted("out of tokens")
            return f"observation {calls['obs']}"
        return json.dumps({"summary": "ok", "confidence": "medium"})
    st = daily_analysis.analyze_day(date(2026, 9, 27), TZ, llm, NOW)
    assert st == "partial"
    d = at.get_daily("2026-09-27")
    saved = len(d["partial_observations"]); assert saved == 2 and d["analysis_status"] == "partial"
    total_batches = len(daily_analysis.make_batches(daily_analysis.build_evidence(date(2026, 9, 27), TZ)))
    assert total_batches > 3
    calls["resumed"] = True; before = calls["obs"]
    assert daily_analysis.analyze_day(date(2026, 9, 27), TZ, llm, NOW) == "complete"
    assert calls["obs"] - before == total_batches - saved            # ONLY the missing batches were processed


def test_malformed_synthesis_falls_back_to_observations_not_invented_text():
    add(1, "2026-09-27T04:00:00Z", 600, "github.com", "focused_work")
    def llm(system, user, prio, mt):
        return "- solid GitHub block" if system.startswith("You analyse") else "not json at all"
    assert daily_analysis.analyze_day(date(2026, 9, 27), TZ, llm, NOW) == "complete"
    ai = at.get_daily("2026-09-27")["ai_analysis"]
    assert ai["fallback"] is True and ai["confidence"] == "low" and "GitHub" in ai["summary"]


def test_late_upload_after_complete_marks_day_for_reanalysis():
    add(1, "2026-09-27T04:00:00Z", 600, "github.com", "focused_work")
    assert daily_analysis.analyze_day(date(2026, 9, 27), TZ, fake_llm_factory([]), NOW) == "complete"
    add(2, "2026-09-27T05:00:00Z", 600, "github.com", "focused_work")     # offline queue flushed late
    at.refresh_day(date(2026, 9, 27), TZ, NOW)
    assert at.get_daily("2026-09-27")["analysis_status"] == "waiting"


# ----------------------------------------------------------- catch-up (PC was off)
def test_catchup_processes_missed_days_oldest_first_within_cap(monkeypatch):
    monkeypatch.setattr(settings, "analysis_max_daily_ai_days_per_run", 2)
    db.set_state("activation_ts", "2026-09-22T04:00:00Z")
    for n, day in enumerate(["22", "23", "24", "25", "26"]):
        add(n, f"2026-09-{day}T05:00:00Z", 900, "github.com", "focused_work")
    order = []
    def llm(system, user, prio, mt):
        if system.startswith("You analyse"):
            order.append(json.loads(user)[0]["f"]); return "obs"
        return json.dumps({"summary": "ok", "confidence": "high"})
    r = catchup.run_catchup(TZ, llm, NOW)
    assert [d for d, s in r["days"].items() if s == "complete"] == ["2026-09-22", "2026-09-23"]   # oldest first, capped at 2
    r2 = catchup.run_catchup(TZ, llm, NOW)
    assert [d for d, s in r2["days"].items() if s == "complete"] == ["2026-09-24", "2026-09-25"]   # resumes where it stopped
    assert at.get_daily("2026-09-29")["analysis_status"] in ("in_progress", "no_data")


def test_catchup_without_api_key_keeps_deterministic_metrics_and_waits():
    db.set_state("activation_ts", "2026-09-26T04:00:00Z")
    add(1, "2026-09-27T05:00:00Z", 900, "github.com", "focused_work")
    r = catchup.run_catchup(TZ, None, NOW)                            # default llm => GroqNotConfigured
    d = at.get_daily("2026-09-27")
    assert d["total_seconds"] == 900 and d["analysis_status"] == "waiting" and d["ai_analysis"] is None


# ----------------------------------------------------------- monthly
def test_monthly_uses_daily_summaries_and_generates_once():
    for i in range(3):
        add(i, f"2026-08-{10 + i}T05:00:00Z", 3600, "github.com", "focused_work", source="history_estimated")
        at.refresh_day(date(2026, 8, 10 + i), TZ, NOW)
    seen = []
    def llm(system, user, prio, mt):
        seen.append(json.loads(user)); return json.dumps({"summary": "August was steady.", "confidence": "medium"})
    assert monthly_analysis.generate_monthly("2026-08", TZ, llm, now=NOW) == "complete"
    m = at.get_monthly("2026-08")
    assert m["total_seconds"] == 3 * 3600 and m["estimated_seconds"] == 3 * 3600 and m["measured_seconds"] == 0
    assert seen[0]["estimated_from_history"] == "3h" and seen[0]["measured_by_extension"] == "0s"
    monthly_analysis.generate_monthly("2026-08", TZ, llm, now=NOW)
    assert len(seen) == 1                                             # once
    assert monthly_analysis.generate_monthly("2026-09", TZ, llm, now=NOW) == "in_progress"   # current month: no narrative yet


def test_monthly_waits_for_unfinished_live_days():
    add(1, "2026-08-10T05:00:00Z", 600, "github.com", "focused_work")           # live day, waiting
    assert monthly_analysis.generate_monthly("2026-08", TZ, lambda *a: "{}", now=NOW) == "waiting"


def test_current_month_keeps_estimated_and_measured_separate():
    add(1, "2026-09-05T05:00:00Z", 1000, "a.io", "learning", source="history_estimated")
    add(2, "2026-09-28T05:00:00Z", 2000, "a.io", "learning")
    agg, _ = monthly_analysis.refresh_month("2026-09", TZ, NOW)
    assert agg["estimated_seconds"] == 1000 and agg["measured_seconds"] == 2000
    assert agg["by_source"]["history_estimated"]["categories"]["learning"] == 1000
    assert agg["by_source"]["extension_measured"]["categories"]["learning"] == 2000


# ----------------------------------------------------------- tools + cards
def test_tools_label_sources_and_never_expose_sensitive():
    seed_day()
    r = at.tool_get_category_time("2026-09-27..2026-09-27", "entertainment", tz=TZI, now=NOW)
    assert r["data"]["estimated_seconds"] == 600 and r["data"]["measured_seconds"] == 0 and "estimated" in r["evidence"][0]
    s = at.tool_search_activity("hsbc", "all", tz=TZI, now=NOW)
    assert s["data"]["matches"] == []
    d = at.tool_top_distractions("2026-09-27..2026-09-27", tz=TZI, now=NOW)
    assert d["data"]["wasted_by_domain"][0]["domain"] == "instagram.com"
    assert at.tool_get_category_time("today", "nonsense", tz=TZI)["error"]
    m = at.tool_measured_vs_estimated("2026-09-27..2026-09-27", tz=TZI, now=NOW)
    assert m["data"]["estimated"]["seconds"] == 600


def test_compare_and_daily_tool():
    seed_day()
    r = at.tool_compare_periods("2026-09-27", "2026-09-26", tz=TZI, now=NOW)
    assert r["data"]["a"]["total"] != r["data"]["b"]["total"] and r["data"]["category_changes_a_minus_b"]
    assert at.tool_get_daily_summary("2026-09-27", tz=TZI, now=NOW)["data"]["data_kind"].startswith("mixed")
    assert "error" in at.tool_get_daily_summary("garbage", tz=TZI)


def test_insight_cards():
    seed_day()
    ws, we, _ = timeutil.resolve_range("2026-09-27", TZI)
    cards = at.insight_cards(storage.sessions_between(ws, we), ws, we, TZI)
    assert cards["biggest_distraction"]["domain"] == "instagram.com"
    assert cards["longest_focus"]["seconds"] >= 3600 and cards["learning_to_wasted_ratio"] == round(1800 / 1500, 2)
    assert cards["most_productive_window"]["seconds"] > 0


# ----------------------------------------------------------- bootstrap end-to-end
def test_bootstrap_end_to_end_once_only_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_days", 10)
    monkeypatch.setattr(settings, "history_chunk_days", 3)
    base = dt("2026-09-25T04:00:00Z")
    visits = []
    for i in range(30):
        visits.append({"url": "https://github.com/a/b", "title": "repo", "start": base + timedelta(minutes=i * 5), "dur_s": 120})
        visits.append({"url": "https://someblog.io/p", "title": "How to bake bread", "start": base + timedelta(minutes=i * 5 + 3), "dur_s": 60})
    visits.append({"url": "https://hsbc.com/a", "title": "balance", "start": base + timedelta(minutes=200), "dur_s": 60})
    hist = make_chrome_history(tmp_path / "History", visits)
    # freeze "now" for the window: monkeypatch timeutil.utcnow used by bootstrap
    monkeypatch.setattr(timeutil, "utcnow", lambda: dt("2026-09-27T00:00:00Z"))
    db.set_state("timezone", TZ)
    calls = []
    def gem(payload):
        items = json.loads(payload); calls.append(len(items))
        return json.dumps({"r": [{"i": it["i"], "c": "learning", "f": 0.9, "w": "baking"} for it in items]})
    factory = lambda: HistoryReader(source=hist)
    bootstrap.run(reader_factory=factory, classify_call=gem)
    assert bootstrap.is_completed() and db.get_state("bootstrap_state")["status"] == "completed"
    with db.read_connection() as c:
        n1 = c.execute("select count(*) n from activity_sessions").fetchone()["n"]
        assert c.execute("select count(*) n from activity_sessions where classification_status='pending'").fetchone()["n"] == 0
        assert c.execute("select count(*) n from activity_sessions where source != 'history_estimated'").fetchone()["n"] == 0
        assert c.execute("select count(*) n from daily_summaries").fetchone()["n"] >= 2
    assert calls == [1]                                              # 30 blog visits -> ONE unique item for Gemini
    # duplicate bootstrap prevention: never re-reads or re-classifies
    bootstrap.run(reader_factory=lambda: (_ for _ in ()).throw(AssertionError("history re-read!")), classify_call=lambda p: 1 / 0)
    assert bootstrap.start_in_background()["started"] is False
    with db.read_connection() as c:
        assert c.execute("select count(*) n from activity_sessions").fetchone()["n"] == n1


def test_bootstrap_resume_after_crash_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_days", 6); monkeypatch.setattr(settings, "history_chunk_days", 2)
    base = dt("2026-09-23T04:00:00Z")
    hist = make_chrome_history(tmp_path / "History", [
        {"url": "https://github.com/a", "title": "r", "start": base + timedelta(hours=8 * i), "dur_s": 100} for i in range(9)])
    monkeypatch.setattr(timeutil, "utcnow", lambda: dt("2026-09-26T00:00:00Z"))
    class Boom(HistoryReader):
        n = 0
        def visits(self, a, b):
            Boom.n += 1
            if Boom.n == 3: raise RuntimeError("power cut")
            return super().visits(a, b)
    with pytest.raises(RuntimeError):
        bootstrap.run(reader_factory=lambda: Boom(source=hist), classify_call=lambda p: '{"r":[]}')
    assert not bootstrap.is_completed()
    with db.read_connection() as c:
        partial = c.execute("select count(*) n from activity_sessions").fetchone()["n"]
    bootstrap.run(reader_factory=lambda: HistoryReader(source=hist), classify_call=lambda p: '{"r":[]}')
    assert bootstrap.is_completed()
    with db.read_connection() as c:
        total = c.execute("select count(*) n from activity_sessions").fetchone()["n"]
        dupe = c.execute("select count(*) n from (select activity_key from activity_sessions group by activity_key having count(*)>1)").fetchone()["n"]
    assert total >= partial and dupe == 0


def test_bootstrap_disabled_in_demo(monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "demo")
    assert bootstrap.start_in_background().get("disabled")
