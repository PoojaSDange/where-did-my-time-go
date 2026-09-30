import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database as db
import security
from config import settings
from services import demo_seed, timeutil

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "demo")
    import main
    with TestClient(main.app) as c:
        c.headers.update({"X-API-Token": settings.demo_token})
        yield c


def test_demo_uses_only_synthetic_data_and_real_lifecycle(demo):
    with db.read_connection() as c:
        n = c.execute("select count(*) n from activity_sessions").fetchone()["n"]
        real = c.execute("select count(*) n from activity_sessions where reason not like '%synthetic%' and classification_status='classified'").fetchone()["n"]
        srcs = {r["source"] for r in c.execute("select distinct source from activity_sessions")}
        overlap = c.execute("select count(*) n from activity_sessions where source='history_estimated' and end_time > ?", (db.get_state("activation_ts"),)).fetchone()["n"]
    assert n > 300 and real == 0 and srcs == {"history_estimated", "extension_measured"} and overlap == 0
    assert db.get_state("timezone") == "Europe/London"


def test_demo_is_deterministic_and_seeding_is_idempotent(demo):
    with db.read_connection() as c:
        a = c.execute("select count(*), sum(duration) from activity_sessions").fetchone()
    assert demo_seed.seed_if_empty() is False
    with db.read_connection() as c:
        assert tuple(c.execute("select count(*), sum(duration) from activity_sessions").fetchone()) == tuple(a)


def test_demo_blocks_real_data_paths_and_reseeds_after_delete(demo):
    assert demo.post("/api/extension/activate", json={}).status_code == 403
    assert demo.post("/api/extension/activities", json={"activities": []}).status_code == 403
    assert demo.post("/api/bootstrap/start").json().get("disabled")
    from services.history_reader import HistoryDisabled, HistoryReader
    with pytest.raises(HistoryDisabled):
        HistoryReader()
    assert demo.post("/api/data/delete", json={"confirm": "DELETE"}).status_code == 200
    with db.read_connection() as c:
        assert c.execute("select count(*) n from activity_sessions").fetchone()["n"] > 300   # disposable: re-seeded


def test_demo_token_is_public_only_in_demo_mode(demo):
    anon = TestClient(demo.app)
    assert anon.get("/api/public/config").json()["demo_token"] == settings.demo_token
    assert anon.get("/api/status").status_code == 401                      # still token-protected
    assert demo.get("/api/status").status_code == 200


def test_demo_pages_show_estimated_and_measured(demo):
    d = demo.get("/api/dashboard", params={"range": "all"}).json()["totals"]
    assert d["estimated"] > 0 and d["measured"] > 0 and d["estimated"] + d["measured"] == d["total"]
    tr = demo.get("/api/trends", params={"days": 90}).json()
    assert len(tr["daily"]) == 60 and tr["activation_day"] and len(tr["monthly"]) >= 2
    pre = [x for x in tr["daily"] if x["day"] < tr["activation_day"]]
    post = [x for x in tr["daily"] if x["day"] > tr["activation_day"]]
    assert all(x["measured"] == 0 for x in pre) and all(x["estimated"] == 0 for x in post)


def test_env_example_documents_every_setting():
    cfg = (ROOT / "backend" / "config.py").read_text()
    names = set(re.findall(r'_(?:str|int|float|bool|list)\(\s*"([A-Z_]+)"', cfg))
    example = (ROOT / "backend" / ".env.example").read_text()
    missing = [n for n in names if not re.search(rf"^#?\s*{n}=", example, re.M)]
    assert not missing, missing
    assert re.search(r"^GROQ_API_KEY=$", example, re.M) and re.search(r"^GEMINI_API_KEY=$", example, re.M)  # blank, no key


def test_no_secrets_committed_and_env_ignored():
    gi = (ROOT / ".gitignore").read_text()
    assert ".env" in gi and "api_token.txt" in gi and "*.sqlite3" in gi
    pattern = re.compile(r"(gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{30,}|sk-[A-Za-z0-9]{30,})")
    for p in ROOT.rglob("*"):
        if p.is_file() and "node_modules" not in p.parts and p.suffix in {".py", ".js", ".html", ".json", ".md", ".example", ".txt", ".sh"}:
            assert not pattern.search(p.read_text(errors="ignore")), p


def test_manifest_key_matches_default_extension_id_and_is_mv3():
    m = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert m["manifest_version"] == 3 and m["background"]["service_worker"] == "background.js"
    assert set(m["permissions"]) == {"tabs", "storage", "alarms", "idle"}
    assert all("localhost" in h or "127.0.0.1" in h for h in m["host_permissions"])   # talks ONLY to the local backend
    der = base64.b64decode(m["key"])
    ext_id = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
    from config import DEFAULT_EXTENSION_ID
    assert ext_id == DEFAULT_EXTENSION_ID


def test_extension_never_posts_anywhere_but_the_configured_local_backend():
    src = (ROOT / "extension" / "background.js").read_text() + (ROOT / "extension" / "popup.js").read_text()
    urls = set(re.findall(r"https?://[^\s'\"`)]+", src))
    assert all("localhost" in u or "127.0.0.1" in u for u in urls), urls
