import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database as db
import security
from config import settings
from conftest import dt
from services import timeutil
from test_phase4_analytics import add, seed_day


@pytest.fixture
def client():
    import main
    with TestClient(main.app) as c:
        token = security.token_file().read_text().strip()
        c.headers.update({"X-API-Token": token})
        c.token = token
        yield c


def anon(client):
    return TestClient(client.app)


PROTECTED_GET = ["/api/ask/status/x", "/api/status", "/api/dashboard", "/api/activity", "/api/insights", "/api/trends",
                 "/api/overrides", "/api/settings", "/api/extension/config", "/api/bootstrap/status"]
PROTECTED_POST = ["/api/ask", "/api/ask/start", "/api/extension/activate", "/api/extension/activities", "/api/overrides",
                  "/api/data/delete", "/api/bootstrap/start", "/api/admin/catchup", "/api/settings/timezone"]


def test_token_generated_on_first_run_only_hash_stored(client):
    stored = db.get_state("api_token_hash")
    assert stored and client.token not in json.dumps(stored) and len(client.token) >= 32


def test_every_protected_route_rejects_missing_and_wrong_token(client):
    a = anon(client)
    for p in PROTECTED_GET:
        assert a.get(p).status_code == 401, p
        assert a.get(p, headers={"X-API-Token": "wrong"}).status_code == 401, p
    for p in PROTECTED_POST:
        assert a.post(p, json={}).status_code == 401, p
    assert a.get("/api/health").status_code == 200
    cfg = a.get("/api/public/config").json()
    assert cfg["app_mode"] == "local" and "demo_token" not in cfg and client.token not in json.dumps(cfg)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"X-API-Token": "", "Authorization": f"Bearer {client.token}"}).status_code == 200


def test_cors_restricted_to_frontend_and_extension_never_wildcard(client):
    a = anon(client)
    ok = a.options("/api/status", headers={"Origin": settings.extension_origin, "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Headers": "x-api-token"})
    assert ok.headers.get("access-control-allow-origin") == settings.extension_origin
    fe = a.options("/api/status", headers={"Origin": settings.frontend_origins[0], "Access-Control-Request-Method": "GET"})
    assert fe.headers.get("access-control-allow-origin") == settings.frontend_origins[0]
    for evil in ("https://evil.example", "http://localhost:9999", "chrome-extension://someoneelse"):
        r = a.options("/api/status", headers={"Origin": evil, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers and r.status_code in (400, 403), evil
        r = client.get("/api/status", headers={"Origin": evil})       # even WITH a valid token
        assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    assert "*" not in security.allowed_origins()


def test_dns_rebinding_host_rejected(client):
    r = client.get("/api/status", headers={"Host": "evil.example.com"})
    assert r.status_code == 400
    r = client.get("/api/status", headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"})
    assert r.status_code == 200
    r = client.get("/api/status", headers={"Host": "evil.example.com", "Origin": "http://evil.example.com"})
    assert r.status_code in (400, 403)


def test_localhost_only_binding_default():
    assert settings.host == "127.0.0.1"
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "host=settings.host" in src and "0.0.0.0" not in src


def test_frontend_served_by_same_backend(client):
    r = anon(client).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert anon(client).get("/api/docs").status_code in (404,)


def test_activation_boundary_and_idempotent_ingestion(client):
    from services.history_estimator import HistorySession
    from services import activity_storage as storage
    now = timeutil.utcnow()
    storage.insert_history([HistorySession(now - timedelta(minutes=30), now + timedelta(minutes=30), "h.io", "/", "https://h.io/", "t", "sigH")], "Asia/Kolkata")
    r = client.post("/api/extension/activate", json={"timezone": "Asia/Kolkata"}).json()
    assert r["newly_activated"] is True and r["history_rows_trimmed"] >= 1
    r2 = client.post("/api/extension/activate", json={}).json()
    assert r2["newly_activated"] is False and r2["activation_ts"] == r["activation_ts"]      # exact ts recorded ONCE
    act = {"id": "abcdef123456", "start": timeutil.to_iso(now + timedelta(seconds=5)), "end": timeutil.to_iso(now + timedelta(seconds=65)),
           "url": "https://github.com/x?y=1", "title": "repo"}
    a = client.post("/api/extension/activities", json={"activities": [act], "tz": "Asia/Kolkata"}).json()
    b = client.post("/api/extension/activities", json={"activities": [act]}).json()
    assert a["accepted"] == 1 and b["accepted"] == 0 and b["duplicates"] == 1 and a["acked"] == ["abcdef123456"]
    with db.read_connection() as c:
        h = c.execute("select max(end_time) e from activity_sessions where source='history_estimated'").fetchone()["e"]
        l = c.execute("select min(start_time) s from activity_sessions where source='extension_measured'").fetchone()["s"]
    assert h <= r["activation_ts"] <= l
    assert client.post("/api/extension/activities", json={"activities": [dict(act, id="x")]}).status_code == 422  # validated


def test_timezone_update_from_extension(client):
    client.post("/api/extension/activate", json={"timezone": "America/New_York"})
    assert db.get_state("timezone") == "America/New_York"
    client.post("/api/extension/activate", json={"timezone": "Not/AZone"})
    assert db.get_state("timezone") == "America/New_York"


def test_website_endpoints_shape_and_estimated_measured_split(client):
    seed_day()
    spec = "2026-09-27"
    d = client.get("/api/dashboard", params={"range": spec}).json()
    t = d["totals"]
    assert t["estimated"] == 600 and t["measured"] == t["total"] - 600 and t["wasted"] == 1500
    assert d["groups"]["distract"] == 1500 and d["coverage"]["pending"] == 1
    a = client.get("/api/activity", params={"range": spec}).json()
    assert a["total_sessions"] == 7 and {s["source"] for s in a["sessions"]} == {"history_estimated", "extension_measured"}
    assert sum(sum(h.values()) for h in a["hourly"]) == t["total"]
    assert client.get("/api/activity", params={"range": spec, "category": "unclassified"}).json()["total_sessions"] == 2
    assert client.get("/api/activity", params={"range": spec, "q": "github"}).json()["total_sessions"] == 1
    assert client.get("/api/dashboard", params={"range": "bogus"}).status_code == 400
    tr = client.get("/api/trends", params={"days": 400}).json()
    assert any(x["day"] == "2026-09-27" and x["estimated"] == 600 for x in tr["daily"]) and tr["monthly"]
    ins = client.get("/api/insights").json()
    assert "daily" in ins and "monthly" in ins and "cards" in ins


def test_override_endpoint_updates_rows_and_numbers(client):
    seed_day()
    a = client.get("/api/activity", params={"range": "2026-09-27", "q": "instagram"}).json()["sessions"][0]
    assert a["is_wasted"] is True
    r = client.post("/api/overrides", json={"session_id": a["id"], "scope": "domain", "category": "learning"}).json()
    assert r["updated_sessions"] == 1
    d = client.get("/api/dashboard", params={"range": "2026-09-27"}).json()
    assert d["totals"]["wasted"] == 0 and d["categories"][0]["category"] in ("focused_work", "learning")
    assert client.post("/api/overrides", json={"domain": "x.com", "category": "nonsense"}).status_code == 422
    ov = client.get("/api/overrides").json()["overrides"]
    assert ov[0]["match_value"] == "instagram.com" and client.delete(f"/api/overrides/{ov[0]['id']}").status_code == 200


def test_delete_all_data_and_excluded_domains(client):
    seed_day()
    assert client.post("/api/data/delete", json={"confirm": "no"}).status_code == 400
    client.post("/api/settings/excluded-domains", json={"domains": ["WWW.Secret.com"]})
    assert client.get("/api/settings").json()["excluded_domains"] == ["secret.com"]
    assert client.post("/api/data/delete", json={"confirm": "DELETE"}).status_code == 200
    with db.read_connection() as c:
        for t in ("activity_sessions", "daily_summaries", "monthly_summaries", "classification_cache", "user_overrides"):
            assert c.execute(f"select count(*) n from {t}").fetchone()["n"] == 0, t
    assert db.get_state("activation_ts") is None and db.get_state("excluded_domains") is None
    assert client.get("/api/status").status_code == 200                       # token survives; product back to fresh install


def test_ask_errors_are_clean(client):
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 503        # no GROQ key
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_ask_success_path(client, monkeypatch):
    from services import agent
    monkeypatch.setattr(agent, "ask", lambda q, h=None: {"answer": "ok", "evidence": ["e"], "tools_used": [], "steps": 1})
    assert client.post("/api/ask", json={"question": "hi"}).json()["answer"] == "ok"


def test_demo_mode_blocks_live_ingestion_and_history(monkeypatch, client):
    monkeypatch.setattr(settings, "app_mode", "demo")
    assert client.post("/api/extension/activate", json={}).status_code == 403
    assert client.post("/api/extension/activities", json={"activities": []}).status_code == 403
    assert client.post("/api/bootstrap/start").json().get("disabled")


# ---------------------------------------------------------------- explicit profile choice over the API
def _profiles(tmp_path, monkeypatch):
    from test_phase2_history import _fake_user_data
    return _fake_user_data(tmp_path, monkeypatch)


def test_profiles_endpoint_lists_but_start_requires_explicit_choice(client, tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch)
    r = client.get("/api/history/profiles").json()
    assert len(r["profiles"]) == 3 and r["history_source"] is None and r["history_forced"] is False
    s = client.post("/api/bootstrap/start").json()                       # no choice => nothing starts, nothing is read
    assert s["started"] is False and s["needs_profile"] is True
    assert client.get("/api/status").json()["history_source"] is None
    with db.read_connection() as c:
        assert c.execute("select count(*) n from activity_sessions").fetchone()["n"] == 0


def test_start_rejects_unknown_ids_and_arbitrary_paths(client, tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch)
    for bad in ("nope", "/etc/passwd", "C:\\Windows\\win.ini", "../../secret", ""):
        r = client.post("/api/bootstrap/start", json={"profile_id": bad})
        assert r.status_code in (400,) or r.json().get("needs_profile"), bad
    assert db.get_state("history_source") is None


def test_start_with_chosen_profile_persists_choice_and_imports_it(client, tmp_path, monkeypatch):
    import time
    from services import bootstrap
    ud = _profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "history_days", 5)
    pid = [p for p in client.get("/api/history/profiles").json()["profiles"] if p["profile_dir"] == "Profile 2"][0]["id"]
    r = client.post("/api/bootstrap/start", json={"profile_id": pid}).json()
    assert r["started"] is True and "Person 2" in client.get("/api/status").json()["history_source"]["label"]
    for _ in range(100):
        if client.get("/api/bootstrap/status").json().get("status") in ("completed", "failed"):
            break
        time.sleep(0.1)
    st = client.get("/api/bootstrap/status").json()
    assert st["status"] == "completed", st
    # the choice survives: a second start is a no-op and does not re-import
    assert client.post("/api/bootstrap/start").json()["started"] is False
    assert client.get("/api/history/profiles").json()["history_source"]["id"] == pid


def test_explicit_env_path_counts_as_a_choice(client, tmp_path, monkeypatch):
    from conftest import make_chrome_history
    from test_phase2_history import V
    f = make_chrome_history(tmp_path / "History", [V("https://github.com/a", "r", 0, 30)])
    monkeypatch.setattr(settings, "chrome_history_path", str(tmp_path))         # a profile FOLDER is accepted
    st = client.get("/api/status").json()
    assert st["history_forced"] is True
    assert client.post("/api/bootstrap/start").json().get("needs_profile") is None
