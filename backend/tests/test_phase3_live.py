import json
from datetime import timedelta

import pytest

import database as db
from config import settings
from conftest import dt
from services import activity_storage as storage
from services import ai_classifier, groq_client, timeutil
from services.classification_worker import ClassificationWorker

NOW = dt("2026-09-29T06:00:00Z")
ACT = dt("2026-09-29T04:00:00Z")


def act(i, url, title, start_min, dur_s=120):
    s = ACT + timedelta(minutes=start_min)
    return {"id": f"live-{i:04d}-abcdef", "start": timeutil.to_iso(s), "end": timeutil.to_iso(s + timedelta(seconds=dur_s)),
            "url": url, "title": title}


@pytest.fixture
def activated():
    db.set_state("activation_ts", timeutil.to_iso(ACT))


def ingest(items):
    return storage.insert_live(items, "Asia/Kolkata", now=NOW)


# ---------------------------------------------------------------- ingestion
def test_ingest_idempotent_and_normalized(activated):
    a = act(1, "https://www.Example.com/a/b/?utm=1&token=SECRET#frag", "  (5) Hello   world ", 1)
    r1 = ingest([a]); r2 = ingest([a, a])
    assert r1["accepted"] == 1 and r2["accepted"] == 0 and r2["duplicates"] == 2
    with db.read_connection() as c:
        row = dict(c.execute("select * from activity_sessions").fetchone())
    assert row["url"] == "https://example.com/a/b" and row["title"] == "Hello world"
    assert row["source"] == "extension_measured" and row["classification_status"] == "pending" and row["duration"] == 120


def test_ingest_before_activation_rejected_and_clamped():
    a = act(1, "https://a.io/", "x", 1)
    assert ingest([a])["rejected"] == 1                       # no activation yet
    db.set_state("activation_ts", timeutil.to_iso(ACT + timedelta(minutes=1, seconds=30)))
    ingest([a])
    with db.read_connection() as c:
        r = c.execute("select start_time, duration from activity_sessions").fetchone()
    assert r["start_time"] == timeutil.to_iso(ACT + timedelta(minutes=1, seconds=30)) and r["duration"] == 90


def test_ingest_local_midnight_split_is_idempotent(activated):
    # IST midnight == 18:30Z. Session 18:29:00Z -> 18:32:00Z crosses it.
    db.set_state("activation_ts", "2026-09-29T00:00:00Z")
    a = {"id": "midnight-session-1", "start": "2026-09-29T18:29:00Z", "end": "2026-09-29T18:32:00Z", "url": "https://a.io/", "title": "t"}
    s = storage.insert_live([a], "Asia/Kolkata", now=dt("2026-09-30T00:00:00Z"))
    s2 = storage.insert_live([a], "Asia/Kolkata", now=dt("2026-09-30T00:00:00Z"))
    assert s["accepted"] == 2 and s2["accepted"] == 0
    with db.read_connection() as c:
        rows = c.execute("select id, start_time, end_time, duration from activity_sessions order by start_time").fetchall()
    assert [r["duration"] for r in rows] == [60, 120] and rows[0]["end_time"] == rows[1]["start_time"] == "2026-09-29T18:30:00Z"


def test_ingest_excluded_sensitive_and_useless(activated):
    db.set_state("excluded_domains", ["secret.com"])
    r = ingest([act(1, "https://x.secret.com/a", "t", 1), act(2, "chrome://settings", "t", 2),
                act(3, "https://mail.google.com/mail/u/0/#inbox/abc", "Inbox - me@x.com", 3),
                act(4, "https://github.com/a/b", "repo", 4)])
    assert r["excluded"] == 1 and r["rejected"] == 1 and r["accepted"] == 2
    with db.read_connection() as c:
        rows = {r["domain"]: dict(r) for r in c.execute("select * from activity_sessions")}
    assert rows["mail.google.com"]["sensitive"] == 1 and rows["mail.google.com"]["category"] == "communication"
    assert rows["github.com"]["classified_by"] == "rule" and rows["github.com"]["is_wasted"] == 0


def test_live_never_overlaps_history_boundary(activated):
    from services.history_estimator import HistorySession
    h = HistorySession(ACT - timedelta(minutes=2), ACT + timedelta(minutes=3), "h.io", "/", "https://h.io/", "t", "sigh")
    storage.insert_history([h], "Asia/Kolkata")
    ingest([act(1, "https://l.io/", "x", 0, 60)])
    with db.read_connection() as c:
        hist = c.execute("select max(end_time) e from activity_sessions where source='history_estimated'").fetchone()["e"]
        live = c.execute("select min(start_time) s from activity_sessions where source='extension_measured'").fetchone()["s"]
    assert hist <= timeutil.to_iso(ACT) <= live


# ---------------------------------------------------------------- worker
def fake_llm(mapping, log=None):
    def call(payload):
        items = json.loads(payload)
        if log is not None:
            log.append(items)
        out = []
        for it in items:
            c = mapping(it)
            if c is None:
                continue
            out.append({"i": it["i"], "c": c[0], "f": c[1], "w": "ok"})
        return json.dumps({"r": out})
    return call


def rows():
    with db.read_connection() as c:
        return [dict(r) for r in c.execute("select * from activity_sessions order by start_time")]


def test_worker_classifies_caches_and_dedupes(activated):
    ingest([act(i, "https://blog.io/post", "Learn asyncio", i * 5) for i in range(1, 5)] + [act(9, "https://blog.io/cats", "cute cats", 60)])
    log = []
    w = ClassificationWorker(call=fake_llm(lambda it: ("learning", 0.9) if "asyncio" in it["t"] else ("entertainment", 0.95), log))
    s = w.run_once()
    assert s["classified"] == 2 and len(log) == 1 and len(log[0]) == 2    # 5 rows -> 2 unique signatures, ONE call
    assert all(r["classification_status"] == "classified" for r in rows())
    # new visit of a known page: preclassified from cache at ingest, zero LLM calls
    ingest([act(20, "https://blog.io/post", "Learn asyncio", 120)])
    assert rows()[-1]["classified_by"] == "cache"
    assert w.run_once()["batches"] == 0


def test_ambiguous_not_cached_and_never_wasted(activated):
    ingest([act(1, "https://blog.io/x", "hmm", 1, 900)])
    ClassificationWorker(call=fake_llm(lambda it: ("ambiguous", 0.2))).run_once()
    r = rows()[0]
    assert r["category"] == "ambiguous" and r["classification_status"] == "classified" and r["is_wasted"] == 0
    with db.read_connection() as c:
        assert c.execute("select count(*) n from classification_cache").fetchone()["n"] == 0


def test_wasted_is_per_session_not_frozen_per_page(activated):
    # same page, same category: 30s glance is NOT wasted, a 25-min session IS.
    ingest([act(1, "https://videos.io/w", "funny clip", 1, 30), act(2, "https://videos.io/w", "funny clip", 10, 1500)])
    ClassificationWorker(call=fake_llm(lambda it: ("entertainment", 0.95))).run_once()
    r = rows()
    assert (r[0]["is_wasted"], r[1]["is_wasted"]) == (0, 1)
    # low confidence entertainment for a long session: still not wasted (needs evidence)
    ingest([act(3, "https://videos.io/other", "maybe fun", 100, 1500)])
    ClassificationWorker(call=fake_llm(lambda it: ("entertainment", 0.4))).run_once()
    assert rows()[-1]["is_wasted"] == 0


def test_malformed_reply_splits_and_one_bad_item_does_not_fail_batch(activated):
    ingest([act(i, f"https://s{i}.io/", f"title {i}", i * 3) for i in range(1, 5)])
    calls = []
    def call(payload):
        items = json.loads(payload); calls.append(len(items))
        if len(items) > 1 and any(it["t"] == "title 3" for it in items):
            return "sorry, I cannot do that ```"                      # malformed for any batch containing the poison item
        if items[0]["t"] == "title 3":
            return "still garbage"
        return json.dumps({"r": [{"i": it["i"], "c": "research", "f": 0.9} for it in items]})
    ClassificationWorker(call=call).run_once()
    st = {r["title"]: (r["classification_status"], r["attempt_count"]) for r in rows()}
    assert st["title 3"] == ("pending", 1)                       # only the poison item stays pending
    assert all(v == ("classified", 0) for k, v in st.items() if k != "title 3")
    assert max(calls) == 4 and 1 in calls                        # it split until the poison item stood alone


def test_item_failures_end_in_failed_status_never_fabricated(activated, monkeypatch):
    monkeypatch.setattr(settings, "classify_max_attempts", 3)
    ingest([act(1, "https://s1.io/", "one", 1)])
    w = ClassificationWorker(call=lambda p: "not json")
    for _ in range(3):
        w.run_once()
    r = rows()[0]
    assert r["classification_status"] == "failed" and r["category"] is None and r["attempt_count"] == 3
    assert w.run_once()["batches"] == 0                            # terminal: not retried forever


def test_service_outage_keeps_pending_without_burning_attempts(activated, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: (503, {}, {}))
    monkeypatch.setattr(groq_client, "_sleep", lambda s: None)
    ingest([act(1, "https://s1.io/", "one", 1)])
    w = ClassificationWorker()
    s = w.run_once()
    assert s["stopped"] and rows()[0]["attempt_count"] == 0 and rows()[0]["classification_status"] == "pending"
    assert w.next_delay() >= settings.worker_backoff_base_seconds


def test_worker_backoff_grows_exponentially_and_caps():
    w = ClassificationWorker()
    delays = []
    for n in range(1, 12):
        w.consecutive_failures = n
        delays.append(w.next_delay())
    assert delays[1] == 2 * delays[0] and delays[-1] == settings.worker_backoff_max_seconds


def test_worker_rejected_request_counts_as_attempt(activated, monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: (400, {}, {"error": {"message": "model not found"}}))
    ingest([act(1, "https://s1.io/", "one", 1)])
    ClassificationWorker().run_once()
    assert rows()[0]["attempt_count"] == 1


def test_user_override_beats_cache_and_llm_and_applies_to_future(activated):
    ingest([act(1, "https://videos.io/w", "python course", 1)])
    ClassificationWorker(call=fake_llm(lambda it: ("entertainment", 0.9))).run_once()
    sig = rows()[0]["signature"]
    out = storage.set_override("signature", sig, "learning", "Asia/Kolkata")
    assert out["updated"] == 1 and rows()[0]["category"] == "learning" and rows()[0]["classified_by"] == "override"
    ingest([act(2, "https://videos.io/w", "python course", 30)])       # future session, cache says entertainment
    assert rows()[-1]["category"] == "learning" and rows()[-1]["classified_by"] == "override"
    storage.set_override("domain", "videos.io", "break", "Asia/Kolkata")
    ingest([act(3, "https://videos.io/other", "whatever", 60)])
    assert rows()[-1]["category"] == "break"
    assert rows()[1]["category"] == "learning"                          # signature override still more specific


def test_llm_payload_never_contains_sensitive_or_raw_urls(activated):
    ingest([act(1, "https://mail.google.com/mail/u/0/", "Inbox private", 1),
            act(2, "https://blog.io/a/b/c/d?token=SECRET", "Mail to bob@example.com 555123456789", 5)])
    log = []
    ClassificationWorker(call=fake_llm(lambda it: ("research", 0.9), log)).run_once()
    blob = json.dumps(log)
    for bad in ("SECRET", "bob@example.com", "555123456789", "Inbox", "mail.google.com", "https://"):
        assert bad not in blob


# ---------------------------------------------------------------- groq client
@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "k")
    sleeps = []
    monkeypatch.setattr(groq_client, "_sleep", lambda s: sleeps.append(s))
    return sleeps


def ok_reply(tokens=100):
    return (200, {}, {"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": tokens}})


def test_groq_429_honours_retry_after_then_succeeds(groq, monkeypatch):
    seq = [(429, {"retry-after": "7"}, {}), ok_reply()]
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: seq.pop(0))
    r = groq_client.chat([{"role": "user", "content": "x"}], priority=0, model="m")
    assert groq_client.message_text(r) == "hi" and groq[0] >= 7
    assert groq_client.daily_used() == 100


def test_groq_persistent_429_raises_and_trips_breaker(groq, monkeypatch):
    monkeypatch.setattr(settings, "groq_max_retries", 2)
    monkeypatch.setattr(settings, "groq_breaker_threshold", 2)
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: (429, {"retry-after": "1"}, {}))
    for _ in range(2):
        with pytest.raises(groq_client.RateLimited):
            groq_client.chat([{"role": "user", "content": "x"}], priority=0, model="m")
    calls = []
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: calls.append(1) or ok_reply())
    with pytest.raises(groq_client.CircuitOpen):                      # fails fast: no HTTP call while open
        groq_client.chat([{"role": "user", "content": "x"}], priority=0, model="m")
    assert calls == []


def test_groq_long_retry_after_is_not_slept_through(groq, monkeypatch):
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: (429, {"retry-after": "3600"}, {}))
    with pytest.raises(groq_client.RateLimited) as e:
        groq_client.chat([{"role": "user", "content": "x"}], priority=0, model="m")
    assert e.value.retry_after >= 3600 and sum(groq) < 1                # returned immediately, work stays pending


def test_daily_budget_priorities_and_supervisor_reserve(groq, monkeypatch):
    monkeypatch.setattr(settings, "groq_tpd", 10000)
    monkeypatch.setattr(settings, "groq_supervisor_reserve_tokens", 2000)
    monkeypatch.setattr(groq_client, "_post", lambda *a, **k: ok_reply(50))
    db.set_state("groq_daily", {"date": groq_client._today_key(), "used": 7000})   # 7000 of 8000 general budget
    msg = [{"role": "user", "content": "x"}]
    # live may still run (<= 8000), monthly cap = 8000*0.7 = 5600 (already exceeded), daily = 6800 (exceeded)
    groq_client.chat(msg, priority=groq_client.PRIORITY_LIVE, model="m", max_tokens=100)
    with pytest.raises(groq_client.BudgetExhausted):
        groq_client.chat(msg, priority=groq_client.PRIORITY_DAILY, model="m", max_tokens=100)
    with pytest.raises(groq_client.BudgetExhausted):
        groq_client.chat(msg, priority=groq_client.PRIORITY_MONTHLY, model="m", max_tokens=100)
    db.set_state("groq_daily", {"date": groq_client._today_key(), "used": 8500})   # general budget gone
    with pytest.raises(groq_client.BudgetExhausted):
        groq_client.chat(msg, priority=groq_client.PRIORITY_LIVE, model="m", max_tokens=100)
    groq_client.chat(msg, priority=groq_client.PRIORITY_SUPERVISOR, model="m", max_tokens=100)  # reserve keeps Ask alive


def test_daily_counter_resets_on_new_day(groq, monkeypatch):
    db.set_state("groq_daily", {"date": "2000-01-01", "used": 999999})
    assert groq_client.daily_used() == 0


def test_tpm_limiter_waits_instead_of_failing(groq, monkeypatch):
    monkeypatch.setattr(settings, "groq_tpm", 1000)
    monkeypatch.setattr(settings, "groq_max_wait_seconds", 120)
    clock = {"t": 1000.0}
    monkeypatch.setattr(groq_client, "_now", lambda: clock["t"])
    monkeypatch.setattr(groq_client, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    groq_client.limiter.acquire(800)
    t0 = clock["t"]
    groq_client.limiter.acquire(800)                                   # must wait for the window to slide
    assert clock["t"] - t0 >= 59


def test_tpm_limiter_refuses_to_block_for_too_long(groq, monkeypatch):
    monkeypatch.setattr(settings, "groq_tpm", 1000)
    monkeypatch.setattr(settings, "groq_max_wait_seconds", 5)
    monkeypatch.setattr(groq_client, "_now", lambda: 1000.0)
    groq_client.limiter.acquire(800)
    with pytest.raises(groq_client.RateLimited):
        groq_client.limiter.acquire(800)


def test_groq_not_configured_and_bad_request(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    with pytest.raises(groq_client.GroqNotConfigured):
        groq_client.chat([{"role": "user", "content": "x"}], priority=0, model="m")


def test_parse_handles_think_tags_fences_and_garbage():
    txt = '<think>hmm</think>\n```json\n{"r":[{"i":1,"c":"learning","f":0.9,"w":"x"},{"i":2,"c":"bogus"},{"i":99,"c":"news"}]}\n```'
    out = ai_classifier.parse_response(txt, {1, 2})
    assert list(out) == [1] and out[1].category == "learning"
    with pytest.raises(ai_classifier.MalformedResponse):
        ai_classifier.parse_response("no json here", {1})
