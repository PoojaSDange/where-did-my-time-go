import os
import stat
from datetime import timedelta

import pytest

import database as db
from config import settings
from conftest import dt, make_chrome_history
from services import activity_storage as storage
from services import ai_classifier, history_estimator, history_processor, privacy, rule_classifier, timeutil
from services.history_reader import HistoryDisabled, HistoryReader

T0 = dt("2026-09-20T05:00:00Z")


def V(url, title, offset_s, dur_s=0, transition=0):
    return {"url": url, "title": title, "start": T0 + timedelta(seconds=offset_s), "dur_s": dur_s, "transition": transition}


def test_chrome_timestamp_roundtrip():
    d = dt("2026-01-01T00:00:00Z")
    assert timeutil.chrome_us_to_dt(timeutil.dt_to_chrome_us(d)) == d
    # 1601 epoch sanity: Unix epoch is 11644473600 s after 1601
    assert timeutil.chrome_us_to_dt(11_644_473_600 * 1_000_000) == dt("1970-01-01T00:00:00Z")


def test_reader_copies_locked_file_and_skips_subframes(tmp_path):
    src = make_chrome_history(tmp_path / "History", [
        V("https://github.com/x/y?tab=1", "repo", 0, 30),
        V("https://ads.example.com/frame", "ad", 5, 0, transition=3),      # subframe -> dropped
    ])
    os.chmod(src, stat.S_IRUSR)  # read-only "locked-ish": reader must work on a COPY, never the original
    before = src.read_bytes()
    with HistoryReader(source=src) as r:
        visits = r.visits(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [v.url for v in visits] == ["https://github.com/x/y?tab=1"]
    assert visits[0].chrome_duration_seconds == 30
    assert src.read_bytes() == before


def test_reader_disabled_in_demo(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "app_mode", "demo")
    with pytest.raises(HistoryDisabled):
        HistoryReader(source=tmp_path / "x")


def test_processor_drops_useless_and_strips_query():
    from services.history_reader import RawVisit
    raw = [RawVisit(T0, None, u, "t") for u in [
        "chrome://settings", "chrome-extension://abc/page.html", "about:blank", "file:///c:/x.txt",
        "https://www.google.com/_/chrome/newtab", "https://www.Example.com/a/b/?utm_source=x&token=SECRET#frag"]]
    out = history_processor.clean_visits(raw)
    assert len(out) == 1
    assert out[0].url == "https://example.com/a/b" and "SECRET" not in out[0].url


def test_processor_respects_excluded_domains():
    from services.history_reader import RawVisit
    out = history_processor.clean_visits([RawVisit(T0, None, "https://news.site.com/a", "t")], ["site.com"])
    assert out == []


def test_estimator_conservative_no_overlap_and_merge():
    from services.history_reader import RawVisit
    raw = [
        RawVisit(T0, None, "https://a.com/p", "A"),                                # gap 20s -> 20s
        RawVisit(T0 + timedelta(seconds=20), None, "https://b.com/p", "B"),        # gap 4h -> default 30s
        RawVisit(T0 + timedelta(hours=4), 600, "https://c.com/p", "C"),            # chrome dur 600, last -> 600
        RawVisit(T0 + timedelta(hours=4, seconds=610), None, "https://d.com/", "D"),  # redirect hop below
        RawVisit(T0 + timedelta(hours=4, seconds=610), None, "https://e.com/", "E"),
    ]
    clean = history_processor.clean_visits(raw)
    ses = history_estimator.estimate_sessions(clean, T0 - timedelta(days=1), T0 + timedelta(days=1))
    d = {s.domain: s.duration for s in ses}
    assert d["a.com"] == 20 and d["b.com"] == settings.history_default_seconds and d["c.com"] == 600
    assert "d.com" not in d  # same-second redirect hop dropped
    for x, y in zip(ses, ses[1:]):
        assert x.end <= y.start  # never overlap


def test_estimator_cap_five_minutes():
    from services.history_reader import RawVisit
    raw = [RawVisit(T0, None, "https://a.com/p", "A"), RawVisit(T0 + timedelta(minutes=20), None, "https://b.com/p", "B")]
    ses = history_estimator.estimate_sessions(history_processor.clean_visits(raw), T0 - timedelta(hours=1), T0 + timedelta(days=1))
    assert ses[0].duration == settings.history_estimate_cap_seconds


def test_estimator_merges_reloads():
    from services.history_reader import RawVisit
    raw = [RawVisit(T0 + timedelta(seconds=i * 12), None, "https://a.com/p", "same") for i in range(4)]
    raw.append(RawVisit(T0 + timedelta(minutes=10), None, "https://z.com/", "z"))
    ses = history_estimator.estimate_sessions(history_processor.clean_visits(raw), T0 - timedelta(hours=1), T0 + timedelta(days=1))
    a = [s for s in ses if s.domain == "a.com"]
    assert len(a) == 1 and a[0].visit_count == 4


def test_rules_and_privacy():
    assert rule_classifier.classify("github.com", "/x").category == "focused_work"
    assert rule_classifier.classify("console.aws.amazon.com", "/").category == "focused_work"
    assert rule_classifier.classify("amazon.in", "/dp/1").category == "shopping"
    assert rule_classifier.classify("youtube.com", "/watch") is None           # needs the title
    assert rule_classifier.classify("youtube.com", "/shorts/abc").category == "entertainment"
    assert rule_classifier.classify("random-blog.io", "/") is None
    t = privacy.redact_title("Reset for bob@example.com token 9f8a7b6c5d4e3f2a1b0c https://x.io/a?b=1 order 1234567890")
    assert "bob@" not in t and "1234567890" not in t and "9f8a7b6c5d4e3f2a1b0c" not in t and "https://" not in t
    assert privacy.make_signature("www.a.com", "/x/", "(3) Inbox") == privacy.make_signature("a.com", "/x", "Inbox")


def _insert(sessions):
    return storage.insert_history(sessions, "Asia/Kolkata")


def _sessions(specs):
    from services.history_reader import RawVisit
    raw = [RawVisit(T0 + timedelta(seconds=o), None, u, t) for o, u, t in specs]
    return history_estimator.estimate_sessions(history_processor.clean_visits(raw), T0 - timedelta(days=1), T0 + timedelta(days=1))


def test_insert_is_idempotent_and_preclassifies():
    ses = _sessions([(0, "https://github.com/a", "repo"), (60, "https://mail.google.com/mail/u/0/", "Inbox (secret)"),
                     (120, "https://someblog.io/post", "How to bake bread")])
    r1 = _insert(ses); r2 = _insert(ses)
    assert r1["inserted"] == 3 and r2["inserted"] == 0 and r2["duplicates"] == 3
    with db.read_connection() as c:
        rows = {r["domain"]: dict(r) for r in c.execute("select * from activity_sessions")}
    assert rows["github.com"]["classified_by"] == "rule" and rows["github.com"]["source"] == "history_estimated"
    assert rows["mail.google.com"]["sensitive"] == 1 and rows["mail.google.com"]["category"] == "communication"
    assert rows["someblog.io"]["classification_status"] == "pending" and rows["someblog.io"]["category"] is None


def test_history_never_overlaps_activation():
    ses = _sessions([(0, "https://a.io/x", "a"), (100, "https://b.io/x", "b"), (300, "https://c.io/x", "c")])
    db.set_state("activation_ts", timeutil.to_iso(T0 + timedelta(seconds=110)))
    _insert(ses)
    with db.read_connection() as c:
        rows = c.execute("select domain, end_time from activity_sessions order by start_time").fetchall()
    assert [r["domain"] for r in rows] == ["a.io", "b.io"]           # c starts after activation -> dropped
    assert all(r["end_time"] <= timeutil.to_iso(T0 + timedelta(seconds=110)) for r in rows)
    # Activation AFTER rows exist: trim
    db.set_state("activation_ts", timeutil.to_iso(T0 + timedelta(seconds=50)))
    storage.trim_history_to(T0 + timedelta(seconds=50))
    with db.read_connection() as c:
        rows = c.execute("select domain, end_time, duration from activity_sessions").fetchall()
    assert [r["domain"] for r in rows] == ["a.io"] and rows[0]["duration"] == 50


def test_history_sessions_split_at_local_midnight():
    # IST midnight = 18:30Z. A 5-minute session from 18:28Z crosses it.
    from services.history_estimator import HistorySession
    s = HistorySession(dt("2026-09-20T18:28:00Z"), dt("2026-09-20T18:33:00Z"), "a.io", "/", "https://a.io/", "t", "sig1")
    _insert([s])
    with db.read_connection() as c:
        rows = c.execute("select start_time, end_time, duration from activity_sessions order by start_time").fetchall()
    assert len(rows) == 2 and rows[0]["end_time"] == "2026-09-20T18:30:00Z" and rows[1]["start_time"] == "2026-09-20T18:30:00Z"
    assert rows[0]["duration"] + rows[1]["duration"] == 300


def _fake_gemini(text_by_title):
    import json
    def call(payload):
        items = json.loads(payload)
        out = []
        for it in items:
            c, f = text_by_title(it)
            out.append({"i": it["i"], "c": c, "f": f, "w": "test"})
        return json.dumps({"r": out})
    return call


def test_gemini_pass_dedupes_signatures_and_caches_only_confident():
    specs = [(i * 40, "https://someblog.io/post", "How to bake bread") for i in range(5)]      # same signature x5
    specs += [(300, "https://otherblog.io/x", "hmm"), (400, "https://thirdblog.io/y", "Cats")]
    _insert(_sessions(specs))
    calls = []
    def cat(it):
        calls.append(it)
        return ("learning", 0.9) if "bread" in it["t"] else (("ambiguous", 0.3) if it["t"] == "hmm" else ("entertainment", 0.95))
    stats = ai_classifier.classify_history_pending("Asia/Kolkata", call=_fake_gemini(cat))
    assert stats["signatures"] == 3 and len(calls) == 3          # 5 identical visits -> ONE item
    with db.read_connection() as c:
        n_cache = c.execute("select count(*) n from classification_cache").fetchone()["n"]
        amb = c.execute("select category, classification_status, is_wasted from activity_sessions where domain='otherblog.io'").fetchone()
        pend = c.execute("select count(*) n from activity_sessions where classification_status='pending'").fetchone()["n"]
    assert n_cache == 2                                          # ambiguous NOT cached
    assert amb["category"] == "ambiguous" and amb["classification_status"] == "classified" and amb["is_wasted"] == 0
    assert pend == 0
    # second pass hits nothing new (idempotent) and never calls the LLM
    stats2 = ai_classifier.classify_history_pending("Asia/Kolkata", call=lambda p: 1 / 0)
    assert stats2["signatures"] == 0


def test_gemini_payload_is_redacted_and_never_contains_sensitive():
    _insert(_sessions([(0, "https://blog.io/a/b/c/d?token=SECRET#x", "Hi bob@example.com 99887766554"),
                       (60, "https://hsbc.com/accounts", "My balance")]))
    seen = []
    ai_classifier.classify_history_pending("Asia/Kolkata", call=lambda p: (seen.append(p), '{"r":[]}')[1])
    blob = " ".join(seen)
    assert "SECRET" not in blob and "bob@example.com" not in blob and "99887766554" not in blob
    assert "hsbc" not in blob and "balance" not in blob and "https://" not in blob


def test_malformed_gemini_reply_splits_then_fails_after_attempts(monkeypatch):
    monkeypatch.setattr(settings, "classify_max_attempts", 2)
    _insert(_sessions([(0, "https://b1.io/", "one"), (60, "https://b2.io/", "two")]))
    ai_classifier.classify_history_pending("Asia/Kolkata", call=lambda p: "totally not json")
    ai_classifier.classify_history_pending("Asia/Kolkata", call=lambda p: "totally not json")
    with db.read_connection() as c:
        rows = c.execute("select classification_status s, attempt_count a, category from activity_sessions").fetchall()
    assert all(r["s"] == "failed" and r["a"] == 2 and r["category"] is None for r in rows)   # never fabricate a category


def test_partial_reply_only_bad_item_stays_pending():
    _insert(_sessions([(0, "https://b1.io/", "one"), (60, "https://b2.io/", "two")]))
    import json
    def call(p):
        items = json.loads(p)
        return json.dumps({"r": [{"i": items[0]["i"], "c": "learning", "f": 0.9}, {"i": items[1]["i"], "c": "NOT_A_CATEGORY", "f": 1}]})
    ai_classifier.classify_history_pending("Asia/Kolkata", call=call)
    with db.read_connection() as c:
        s = sorted(r["classification_status"] for r in c.execute("select classification_status from activity_sessions"))
    assert s == ["classified", "pending"]


def test_no_gemini_key_leaves_rows_pending_without_burning_attempts():
    _insert(_sessions([(0, "https://b1.io/", "one")]))
    stats = ai_classifier.classify_history_pending("Asia/Kolkata")
    assert stats["skipped"] == "no_api_key"
    with db.read_connection() as c:
        r = c.execute("select classification_status s, attempt_count a from activity_sessions").fetchone()
    assert r["s"] == "pending" and r["a"] == 0


# ---------------------------------------------------------------- history path handling (Windows profile folders etc.)
def test_profile_folder_path_is_resolved_to_history_file(tmp_path):
    from services.history_reader import HistoryUnavailable, resolve_history_file
    prof = tmp_path / "Profile 2"; prof.mkdir()
    make_chrome_history(prof / "History", [V("https://github.com/a", "r", 0, 30)])
    assert resolve_history_file(str(prof)) == prof / "History"               # folder -> History file
    assert resolve_history_file(str(prof / "History")) == prof / "History"   # file stays a file
    assert resolve_history_file(f'"{prof / "History"}"') == prof / "History" # quoted paste from Explorer
    with HistoryReader(source=resolve_history_file(str(prof))) as r:
        assert len(r.visits(T0 - timedelta(hours=1), T0 + timedelta(hours=1))) == 1
    ud = tmp_path / "User Data"; (ud / "Profile 2").mkdir(parents=True)
    with pytest.raises(HistoryUnavailable, match="Profile 2"):
        resolve_history_file(str(ud))                                         # user-data root: helpful hint
    with pytest.raises(HistoryUnavailable, match="does not exist"):
        resolve_history_file(str(tmp_path / "nope"))


def _fake_user_data(tmp_path, monkeypatch, profiles=(("Default", 5000), ("Profile 1", 3000), ("Profile 2", 10))):
    from services import history_reader
    ud = tmp_path / "Chrome" / "User Data"
    info = {}
    for name, age in profiles:
        d = ud / name; d.mkdir(parents=True)
        f = make_chrome_history(d / "History", [V("https://github.com/a", "r", 0, 30)])
        t = os.path.getmtime(f) - age; os.utime(f, (t, t))
        info[name] = {"name": f"Person {name[-1]}", "user_name": f"{name[-1]}@example.com".lower()}
    (ud / "Local State").write_text(__import__("json").dumps({"profile": {"info_cache": info}}))
    monkeypatch.setattr(history_reader, "_user_data_dirs", lambda: [ud])
    return ud


def test_profiles_are_listed_with_names_but_never_auto_chosen(tmp_path, monkeypatch):
    from services import history_reader
    from services.history_reader import HistoryUnavailable
    _fake_user_data(tmp_path, monkeypatch)
    profs = history_reader.list_profiles()
    assert [p["profile_dir"] for p in profs] == ["Profile 2", "Profile 1", "Default"]      # display order only
    assert profs[0]["name"] == "Person 2" and profs[0]["email"] == "2@example.com" and profs[0]["browser"] == "Chrome"
    assert all(p["size_bytes"] > 0 for p in profs)
    with pytest.raises(HistoryUnavailable, match="chosen"):
        history_reader.find_history_path()                                                # NO guessing, ever
    assert history_reader.profile_by_id("nonsense") is None
    assert history_reader.profile_by_id("/etc/passwd") is None                             # client paths are not ids


def test_chosen_profile_is_used_by_the_reader(tmp_path, monkeypatch):
    from services import bootstrap, history_reader
    _fake_user_data(tmp_path, monkeypatch)
    pid = [p for p in history_reader.list_profiles() if p["profile_dir"] == "Profile 1"][0]["id"]
    bootstrap.choose_profile(pid)
    assert history_reader.find_history_path().parent.name == "Profile 1"
    assert "Profile 1" in bootstrap.source_status()["history_source"]["label"] or "Person 1" in bootstrap.source_status()["history_source"]["label"]


def test_switching_profile_discards_partial_import_of_the_old_one(tmp_path, monkeypatch):
    from services import bootstrap, history_reader
    _fake_user_data(tmp_path, monkeypatch)
    ids = {p["profile_dir"]: p["id"] for p in history_reader.list_profiles()}
    bootstrap.choose_profile(ids["Default"])
    _insert(_sessions([(0, "https://old-profile.io/", "from the first profile")]))       # partial import
    db.set_state("bootstrap_window", {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"})
    bootstrap.choose_profile(ids["Default"])                                               # same profile: kept
    with db.read_connection() as c:
        assert c.execute("select count(*) n from activity_sessions").fetchone()["n"] == 1
    bootstrap.choose_profile(ids["Profile 2"])                                             # different: wiped, clean start
    with db.read_connection() as c:
        assert c.execute("select count(*) n from activity_sessions").fetchone()["n"] == 0
    assert db.get_state("bootstrap_window") is None


def test_snapshot_falls_back_to_sqlite_backup_when_copy_is_blocked(tmp_path, monkeypatch):
    from services import history_reader
    src = make_chrome_history(tmp_path / "History", [V("https://github.com/a", "r", 0, 30)])
    monkeypatch.setattr(history_reader.shutil, "copyfile", lambda a, b: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(history_reader.time, "sleep", lambda s: None)
    with HistoryReader(source=src) as r:                                     # copy fails -> immutable read-only backup
        assert len(r.visits(T0 - timedelta(hours=1), T0 + timedelta(hours=1))) == 1
