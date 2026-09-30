"""Normalization, redaction and sensitive-domain handling.

Nothing leaves the machine (to Gemini/Groq) unless it went through this module:
  * query strings + fragments are always stripped;
  * titles are redacted (emails, URLs, long numbers, long tokens);
  * sensitive domains never have their title/URL sent anywhere;
  * only a coarse "path shape" (max 2 segments, IDs masked) is sent instead of paths.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from config import settings

# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------
_USELESS_HOSTS = {"newtab", "new-tab-page", "extensions", "history", "settings", "downloads", "bookmarks", "version"}
_ID_SEGMENT = re.compile(
    r"^(?:\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|"
    r"(?=[A-Za-z_-]*\d)(?=[0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,})$",
    re.I,
)


def is_trackable_url(url: str) -> bool:
    """False for chrome://, extension pages, about:, file:, devtools, new tab, etc."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if host == "www.google.com" and parts.path.startswith("/_/chrome/newtab"):
        return False
    if host in ("newtab",) or (host in _USELESS_HOSTS and "." not in host):
        return False
    return True


def normalize_domain(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def split_url(url: str) -> Optional[tuple[str, str]]:
    """-> (domain, path) with query/fragment removed, or None if not trackable."""
    if not is_trackable_url(url):
        return None
    parts = urlsplit(url.strip())
    domain = normalize_domain(parts.hostname or "")
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return domain, path


def normalized_url(domain: str, path: str) -> str:
    return f"https://{domain}{path if path else '/'}"[:500]


def mask_ids(path: str) -> str:
    segs = [("{id}" if _ID_SEGMENT.match(s) else s) for s in path.split("/")]
    return "/".join(segs)


def path_shape(path: str, max_segments: int = 2) -> str:
    """Coarse, non-identifying path used for signatures and LLM prompts."""
    segs = [s for s in mask_ids(path).split("/") if s]
    return "/" + "/".join(s.lower()[:40] for s in segs[:max_segments])


# --------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------
_COUNT_PREFIX = re.compile(r"^\s*[\(\[]\d{1,4}\+?[\)\]]\s*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_CARD = re.compile(r"\b(?:\d[ -]?){12,19}\b")
_LONG_NUM = re.compile(r"\b\d{6,}\b")
_TOKEN_MIXED = re.compile(r"\b(?=[A-Za-z_-]*\d)(?=[0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}\b")
_TOKEN_LONG = re.compile(r"\b[A-Za-z0-9_-]{28,}\b")


def normalize_title(title: Optional[str]) -> str:
    t = re.sub(r"\s+", " ", (title or "")).strip()
    t = _COUNT_PREFIX.sub("", t)
    return t[: settings.title_max_chars]


def redact_title(title: Optional[str]) -> str:
    """Redact obvious sensitive patterns. Applied before any LLM sees a title."""
    t = normalize_title(title)
    t = _EMAIL.sub("[email]", t)
    t = _URL.sub("[url]", t)
    t = _CARD.sub("[num]", t)
    t = _LONG_NUM.sub("[num]", t)
    t = _TOKEN_MIXED.sub("[token]", t)
    t = _TOKEN_LONG.sub("[token]", t)
    return t


def make_signature(domain: str, path: str, title: Optional[str]) -> str:
    """Stable id for 'the same page': domain + normalized path shape + normalized title."""
    base = f"{normalize_domain(domain)}|{path_shape(path)}|{normalize_title(title).lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Sensitive / excluded domains
# --------------------------------------------------------------------------
# Generic category assigned to sensitive domains (no LLM ever sees them).
_SENSITIVE_HINTS = (
    (("mail.", "outlook.", "proton.me"), "communication"),
    (("1password", "bitwarden", "lastpass"), "productivity"),
)


def domain_matches(domain: str, entries: Iterable[str]) -> bool:
    d = normalize_domain(domain)
    for e in entries:
        e = normalize_domain(e)
        if e and (d == e or d.endswith("." + e)):
            return True
    return False


def is_sensitive_domain(domain: str, extra: Iterable[str] = ()) -> bool:
    return domain_matches(domain, list(settings.sensitive_domains) + list(extra))


def sensitive_category(domain: str) -> str:
    d = normalize_domain(domain)
    for needles, cat in _SENSITIVE_HINTS:
        if any(n in d for n in needles):
            return cat
    return "personal"  # banking, health, unknown sensitive


def is_excluded_domain(domain: str, excluded: Iterable[str]) -> bool:
    return domain_matches(domain, excluded)


def llm_item_fields(domain: str, path: str, title: Optional[str]) -> dict:
    """The ONLY fields ever sent to an LLM for a non-sensitive item."""
    return {"d": normalize_domain(domain), "p": path_shape(path), "t": redact_title(title)}
