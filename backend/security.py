"""Shared API token + origin checks.

Any webpage in any tab can fire requests at http://localhost:<port>, so every API call must carry
a secret token that only the user's own extension/website knows:

  * generated on first run (secrets.token_urlsafe), only its SHA-256 is stored in app_state
  * the plaintext is written once to data/api_token.txt and printed to the console
  * the extension popup / website ask for it once and keep it locally
  * CORS is limited to the frontend origin(s) + the extension origin (never "*"),
    and requests carrying any OTHER Origin header are rejected outright.

Reset: `python security.py reset` (prints a new token; old one stops working).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

import database as db
from config import settings

log = logging.getLogger("wdmt.security")
STATE_KEY = "api_token_hash"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_file() -> Path:
    return Path(settings.data_dir) / "api_token.txt"


def _write_token_file(token: str) -> None:
    p = token_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def ensure_token() -> Optional[str]:
    """Create the token on first run. Returns the PLAINTEXT only when newly generated."""
    if settings.app_mode == "demo":
        # Demo data is synthetic; the token is a known value the demo site may hand to its own UI.
        db.set_state(STATE_KEY, _hash(settings.demo_token))
        return None
    if db.get_state(STATE_KEY):
        return None
    return rotate_token()


def rotate_token() -> str:
    token = secrets.token_urlsafe(32)
    db.set_state(STATE_KEY, _hash(token))
    _write_token_file(token)
    return token


def verify_token(candidate: Optional[str]) -> bool:
    if not candidate:
        return False
    stored = db.get_state(STATE_KEY)
    if not stored:
        return False
    return hmac.compare_digest(_hash(candidate), str(stored))


def extract_token(request: Request) -> Optional[str]:
    tok = request.headers.get("x-api-token")
    if tok:
        return tok.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def require_token(request: Request) -> None:
    """FastAPI dependency: 401 without a valid token."""
    if not verify_token(extract_token(request)):
        raise HTTPException(status_code=401, detail="Missing or invalid API token")


def allowed_origins() -> list[str]:
    return list(settings.frontend_origins) + [settings.extension_origin]


def origin_allowed(origin: Optional[str]) -> bool:
    """No Origin header (curl, same-origin GET, server-to-server) is fine; the token still applies."""
    if not origin:
        return True
    return origin in allowed_origins()


def same_origin_ok(origin: Optional[str], host_header: Optional[str]) -> bool:
    """A page served by us talking to us (Origin == Host) is fine, but ONLY for a trusted Host:
    a DNS-rebinding page has Origin == Host == evil.com, which is not on the allow-list."""
    if not origin or not host_header or "://" not in origin:
        return False
    if origin.split("://", 1)[1] != host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
    if settings.app_mode == "demo" and settings.allowed_hosts == ["localhost", "127.0.0.1", "[::1]", "testserver"]:
        return True  # hosted demo (synthetic data only) is served from its own public host
    return hostname.lower() in settings.allowed_hosts


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        print("New API token (enter it in the extension popup and the website):")
        print(rotate_token())
    else:
        print("usage: python security.py reset")
