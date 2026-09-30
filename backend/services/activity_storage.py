"""All reads/writes of activity_sessions + classification cache + user overrides.

Guarantees:
  * idempotent inserts (unique id / activity_key, INSERT OR IGNORE)
  * sessions crossing LOCAL midnight are split at the boundary
  * history_estimated rows never extend past the activation timestamp
  * live rows never start before the activation timestamp
  * pre-classification order: sensitive -> user override -> rules -> cache -> pending
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import database as db
from config import settings
from models import SOURCE_HISTORY, SOURCE_LIVE, STATUS_CLASSIFIED, STATUS_FAILED, STATUS_PENDING
from services import privacy, rule_classifier, timeutil
from services.waste import decide_is_wasted

log = logging.getLogger("wdmt.storage")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
MAX_SESSION_SECONDS = 24 * 3600


# --------------------------------------------------------------------------
# state shortcuts
# --------------------------------------------------------------------------
def get_tz_name() -> str:
    name = db.get_state("timezone")
    if not timeutil.valid_tz(name):
        name = timeutil.system_timezone_name()
        db.set_state("timezone", name)
    return name


def get_tz() -> ZoneInfo:
    return timeutil.get_tz(get_tz_name())


def get_activation_ts() -> Optional[datetime]:
    v = db.get_state("activation_ts")
    return timeutil.parse_iso(v) if v else None


def excluded_domains() -> list[str]:
    return list(db.get_state("excluded_domains", []) or [])


def extra_sensitive_domains() -> list[str]:
    return list(db.get_state("sensitive_domains_extra", []) or [])


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def cache_get(signature: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    def _q(c: sqlite3.Connection):
        r = c.execute("SELECT * FROM classification_cache WHERE signature=?", (signature,)).fetchone()
        return dict(r) if r else None

    if conn is not None:
        return _q(conn)
    with db.read_connection() as c:
        return _q(c)


def cache_put(signature: str, domain: str, path: str, title: str, category: str,
              confidence: float, reason: str, origin: str) -> bool:
    """Cache ONLY confident, non-ambiguous results. Returns True if stored."""
    if category == "ambiguous" or confidence < settings.cache_min_confidence:
        return False
    with db.connection() as c:
        c.execute(
            """INSERT INTO classification_cache(signature, domain, path, title, category, confidence, reason, origin)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(signature) DO UPDATE SET category=excluded.category, confidence=excluded.confidence,
                    reason=excluded.reason, origin=excluded.origin, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
            (signature, domain, path, (title or "")[: settings.title_max_chars], category,
             float(confidence), reason, origin),
        )
    return True


def cache_touch(signature: str) -> None:
    with db.connection() as c:
        c.execute("UPDATE classification_cache SET hits = hits + 1 WHERE signature=?", (signature,))


# --------------------------------------------------------------------------
# overrides
# --------------------------------------------------------------------------
def override_lookup(conn: sqlite3.Connection, signature: str, domain: str) -> Optional[str]:
    """Signature override beats domain override."""
    r = conn.execute(
        "SELECT category FROM user_overrides WHERE match_type='signature' AND match_value=?", (signature,)
    ).fetchone()
    if r:
        return r["category"]
    r = conn.execute(
        "SELECT category FROM user_overrides WHERE match_type='domain' AND match_value=?",
        (privacy.normalize_domain(domain),),
    ).fetchone()
    return r["category"] if r else None


def list_overrides() -> list[dict]:
    with db.read_connection() as c:
        return [dict(r) for r in c.execute("SELECT * FROM user_overrides ORDER BY id DESC")]


def delete_override(override_id: int) -> bool:
    with db.connection() as c:
        return c.execute("DELETE FROM user_overrides WHERE id=?", (override_id,)).rowcount > 0


def set_override(match_type: str, match_value: str, category: str, tz_name: Optional[str] = None) -> dict:
    """Store an override and apply it to EXISTING matching rows (and, via preclassify, to future ones).

    Returns {"updated": n, "days": [local dates whose deterministic summaries must be recomputed]}.
    """
    tz = timeutil.get_tz(tz_name or get_tz_name())
    if match_type == "domain":
        match_value = privacy.normalize_domain(match_value)
        where, arg = "domain = ?", match_value
    else:
        where, arg = "signature = ?", match_value
    with db.connection() as c:
        c.execute(
            """INSERT INTO user_overrides(match_type, match_value, category) VALUES (?,?,?)
               ON CONFLICT(match_type, match_value) DO UPDATE SET category=excluded.category""",
            (match_type, match_value, category),
        )
        rows = c.execute(
            f"SELECT id, start_time, duration, signature, domain FROM activity_sessions WHERE {where}", (arg,)
        ).fetchall()
        days: set[str] = set()
        updated = 0
        for r in rows:
            # a signature override is more specific than a domain override; keep it authoritative
            if match_type == "domain" and override_lookup(c, r["signature"] or "", r["domain"]) != category:
                continue
            start = timeutil.parse_iso(r["start_time"])
            wasted = decide_is_wasted(category, r["duration"], 1.0, start, tz)
            c.execute(
                """UPDATE activity_sessions SET category=?, confidence=1.0, reason='user override',
                          classification_status='classified', classified_by='override', is_wasted=?
                   WHERE id=?""",
                (category, int(wasted), r["id"]),
            )
            days.add(timeutil.local_date(start, tz).isoformat())
            updated += 1
    return {"updated": updated, "days": sorted(days)}


# --------------------------------------------------------------------------
# pre-classification
# --------------------------------------------------------------------------
@dataclass
class PreClass:
    category: Optional[str]
    confidence: Optional[float]
    reason: Optional[str]
    status: str
    by: Optional[str]
    sensitive: int


def preclassify(conn: sqlite3.Connection, domain: str, path: str, title: str, signature: str,
                extra_sensitive: Iterable[str] = ()) -> PreClass:
    if privacy.is_sensitive_domain(domain, extra_sensitive):
        cat = privacy.sensitive_category(domain)
        return PreClass(cat, 0.9, "sensitive domain (generic category, never sent to an LLM)",
                        STATUS_CLASSIFIED, "sensitive", 1)
    ov = override_lookup(conn, signature, domain)
    if ov:
        return PreClass(ov, 1.0, "user override", STATUS_CLASSIFIED, "override", 0)
    rule = rule_classifier.classify(domain, path, title)
    if rule:
        return PreClass(rule.category, rule.confidence, rule.reason, STATUS_CLASSIFIED, "rule", 0)
    cached = cache_get(signature, conn)
    if cached:
        conn.execute("UPDATE classification_cache SET hits = hits + 1 WHERE signature=?", (signature,))
        return PreClass(cached["category"], cached["confidence"], cached["reason"], STATUS_CLASSIFIED, "cache", 0)
    return PreClass(None, None, None, STATUS_PENDING, None, 0)


# --------------------------------------------------------------------------
# inserts
# --------------------------------------------------------------------------
def _iso(dt: datetime) -> str:
    return timeutil.to_iso(dt)


def history_key(signature: str, start: datetime) -> str:
    return hashlib.sha1(f"h|{signature}|{_iso(start)}".encode()).hexdigest()[:24]


def _exists_live(conn: sqlite3.Connection, client_id: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM activity_sessions WHERE id = ? OR id GLOB ? LIMIT 1", (client_id, client_id + "#*")
    ).fetchone()
    return r is not None


def insert_live(activities: list[dict], tz_name: Optional[str] = None,
                now: Optional[datetime] = None) -> dict:
    """Idempotent ingestion of extension_measured sessions.

    Each dict: id, start (ISO or epoch-ms), end, url, title. Returns counts.
    """
    tz = timeutil.get_tz(tz_name or get_tz_name())
    now = now or timeutil.utcnow()
    activation = get_activation_ts()
    excluded = excluded_domains()
    extra_sens = extra_sensitive_domains()
    stats = {"accepted": 0, "duplicates": 0, "rejected": 0, "excluded": 0}

    with db.connection() as c:
        for a in activities:
            try:
                cid = str(a["id"])
                if not _ID_RE.match(cid):
                    raise ValueError("bad id")
                start = _parse_time(a["start"])
                end = _parse_time(a["end"])
            except (KeyError, ValueError, TypeError):
                stats["rejected"] += 1
                continue
            if activation is None:
                stats["rejected"] += 1  # live data only counts after activation
                continue
            parts = privacy.split_url(str(a.get("url") or ""))
            if parts is None:
                stats["rejected"] += 1
                continue
            domain, path = parts
            if privacy.is_excluded_domain(domain, excluded):
                stats["excluded"] += 1
                continue
            start = max(start, activation)
            end = min(end, now + timedelta(seconds=60))
            if end <= start or (end - start).total_seconds() > MAX_SESSION_SECONDS:
                stats["rejected"] += 1
                continue
            if _exists_live(c, cid):
                stats["duplicates"] += 1
                continue

            title = privacy.normalize_title(a.get("title"))
            signature = privacy.make_signature(domain, path, title)
            pc = preclassify(c, domain, path, title, signature, extra_sens)
            segs = timeutil.split_at_local_midnight(start, end, tz)
            for idx, (s, e) in enumerate(segs):
                rid = cid if len(segs) == 1 else f"{cid}#{idx}"
                dur = int((e - s).total_seconds())
                if dur < 1:
                    continue
                wasted = int(decide_is_wasted(pc.category, dur, pc.confidence, s, tz)) if pc.category else 0
                cur = c.execute(
                    """INSERT OR IGNORE INTO activity_sessions
                       (id, start_time, end_time, domain, url, title, category, duration, is_wasted, confidence,
                        reason, source, classification_status, activity_key, signature, sensitive, classified_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, _iso(s), _iso(e), domain, privacy.normalized_url(domain, path), title, pc.category,
                     dur, wasted, pc.confidence, pc.reason, SOURCE_LIVE, pc.status, rid, signature,
                     pc.sensitive, pc.by),
                )
                if cur.rowcount:
                    stats["accepted"] += 1
                else:
                    stats["duplicates"] += 1
    return stats


def _parse_time(v: Any) -> datetime:
    if isinstance(v, (int, float)):
        return timeutil.epoch_ms_to_dt(float(v))
    return timeutil.parse_iso(str(v))


def insert_history(sessions: list, tz_name: Optional[str] = None) -> dict:
    """Idempotent insert of estimated history sessions (HistorySession objects).

    Trims/drops anything that would extend past the activation timestamp (no overlap with measured data).
    """
    tz = timeutil.get_tz(tz_name or get_tz_name())
    activation = get_activation_ts()
    extra_sens = extra_sensitive_domains()
    stats = {"inserted": 0, "duplicates": 0, "trimmed_or_dropped": 0}
    with db.connection() as c:
        for s in sessions:
            start, end = s.start, s.end
            if activation is not None:
                if start >= activation:
                    stats["trimmed_or_dropped"] += 1
                    continue
                if end > activation:
                    end = activation
                    stats["trimmed_or_dropped"] += 1
            if end <= start:
                continue
            pc = preclassify(c, s.domain, s.path, s.title, s.signature, extra_sens)
            base = history_key(s.signature, s.start)
            segs = timeutil.split_at_local_midnight(start, end, tz)
            for idx, (a, b) in enumerate(segs):
                key = base if len(segs) == 1 else f"{base}#{idx}"
                dur = int((b - a).total_seconds())
                if dur < 1:
                    continue
                wasted = int(decide_is_wasted(pc.category, dur, pc.confidence, a, tz)) if pc.category else 0
                cur = c.execute(
                    """INSERT OR IGNORE INTO activity_sessions
                       (id, start_time, end_time, domain, url, title, category, duration, is_wasted, confidence,
                        reason, source, classification_status, activity_key, signature, sensitive, classified_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"hist-{key}", _iso(a), _iso(b), s.domain, s.url, s.title, pc.category, dur, wasted,
                     pc.confidence, pc.reason, SOURCE_HISTORY, pc.status, key, s.signature, pc.sensitive, pc.by),
                )
                stats["inserted" if cur.rowcount else "duplicates"] += 1
    return stats


def trim_history_to(ts: datetime) -> int:
    """Enforce the boundary rule after activation: history must end at/before `ts`."""
    iso = _iso(ts)
    changed = 0
    with db.connection() as c:
        changed += c.execute(
            "DELETE FROM activity_sessions WHERE source=? AND start_time >= ?", (SOURCE_HISTORY, iso)
        ).rowcount
        rows = c.execute(
            "SELECT id, start_time FROM activity_sessions WHERE source=? AND end_time > ?", (SOURCE_HISTORY, iso)
        ).fetchall()
        for r in rows:
            dur = int((ts - timeutil.parse_iso(r["start_time"])).total_seconds())
            c.execute("UPDATE activity_sessions SET end_time=?, duration=? WHERE id=?", (iso, max(dur, 0), r["id"]))
            changed += 1
    return changed


# --------------------------------------------------------------------------
# classification updates
# --------------------------------------------------------------------------
def _apply(c: sqlite3.Connection, rows: list[sqlite3.Row], category: str, confidence: float,
           reason: str, by: str, tz: ZoneInfo) -> int:
    n = 0
    for r in rows:
        start = timeutil.parse_iso(r["start_time"])
        wasted = int(decide_is_wasted(category, r["duration"], confidence, start, tz))
        c.execute(
            """UPDATE activity_sessions SET category=?, confidence=?, reason=?, classification_status='classified',
                      classified_by=?, is_wasted=?, last_attempt_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
               WHERE id=? AND classification_status != 'classified'""",
            (category, confidence, (reason or "")[:120], by, wasted, r["id"]),
        )
        n += 1
    return n


def apply_classification_to_signature(signature: str, source: str, category: str, confidence: float,
                                      reason: str, by: str, tz_name: str) -> int:
    tz = timeutil.get_tz(tz_name)
    with db.connection() as c:
        rows = c.execute(
            """SELECT id, start_time, duration FROM activity_sessions
               WHERE signature=? AND source=? AND classification_status='pending' AND sensitive=0""",
            (signature, source),
        ).fetchall()
        return _apply(c, rows, category, confidence, reason, by, tz)


def apply_classification_to_ids(ids: list[str], category: str, confidence: float, reason: str,
                                by: str, tz_name: str) -> int:
    if not ids:
        return 0
    tz = timeutil.get_tz(tz_name)
    with db.connection() as c:
        q = ",".join("?" for _ in ids)
        rows = c.execute(
            f"SELECT id, start_time, duration FROM activity_sessions WHERE id IN ({q}) "
            f"AND classification_status='pending'", ids
        ).fetchall()
        return _apply(c, rows, category, confidence, reason, by, tz)


def _bump_attempts(c: sqlite3.Connection, where: str, args: tuple) -> int:
    n = c.execute(
        f"""UPDATE activity_sessions SET attempt_count = attempt_count + 1,
                   last_attempt_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            WHERE classification_status='pending' AND {where}""", args
    ).rowcount
    c.execute(
        "UPDATE activity_sessions SET classification_status='failed' "
        "WHERE classification_status='pending' AND attempt_count >= ?",
        (settings.classify_max_attempts,),
    )
    return n


def record_failed_attempt(signature: str, source: str) -> int:
    with db.connection() as c:
        return _bump_attempts(c, "signature=? AND source=? AND sensitive=0", (signature, source))


def record_failed_attempt_ids(ids: list[str]) -> int:
    if not ids:
        return 0
    with db.connection() as c:
        q = ",".join("?" for _ in ids)
        return _bump_attempts(c, f"id IN ({q})", tuple(ids))


def fail_exhausted() -> int:
    with db.connection() as c:
        return c.execute(
            "UPDATE activity_sessions SET classification_status='failed' "
            "WHERE classification_status='pending' AND attempt_count >= ?",
            (settings.classify_max_attempts,),
        ).rowcount

def domain_priors(domains: list[str]) -> dict[str, tuple[str, float, int]]:
    """A site's usual category, from sessions ALREADY classified (never ambiguous, sensitive or prior-derived,
    so the fallback cannot reinforce itself). Needs PRIOR_MIN_SESSIONS sessions and PRIOR_MIN_SHARE agreement."""
    doms = sorted({privacy.normalize_domain(d) for d in domains if d})
    out: dict[str, tuple[str, float, int]] = {}
    if not doms:
        return out
    with db.read_connection() as c:
        for i in range(0, len(doms), 400):
            chunk = doms[i : i + 400]
            q = ",".join("?" for _ in chunk)
            rows = c.execute(
                f"""SELECT domain, category, COUNT(*) AS n FROM activity_sessions
                    WHERE classification_status='classified' AND category IS NOT NULL AND category != 'ambiguous'
                      AND sensitive=0 AND COALESCE(classified_by,'') != 'prior' AND domain IN ({q})
                    GROUP BY domain, category""", chunk).fetchall()
            per: dict[str, list[tuple[str, int]]] = {}
            for r in rows:
                per.setdefault(r["domain"], []).append((r["category"], r["n"]))
            for d, lst in per.items():
                total = sum(n for _, n in lst)
                cat, n = max(lst, key=lambda x: x[1])
                if total >= settings.prior_min_sessions and n / total >= settings.prior_min_share:
                    out[d] = (cat, n / total, total)
    return out


def reset_ambiguous(sources: list[str], tz_name: str) -> dict:
    """Send sessions the LLM marked 'ambiguous' back to 'pending' for another pass.
    Only LLM verdicts are reset (never your overrides, rules, cache hits or sensitive rows)."""
    tz = timeutil.get_tz(tz_name)
    q = ",".join("?" for _ in sources)
    where = (f"classification_status='classified' AND category='ambiguous' AND classified_by='llm' "
             f"AND sensitive=0 AND source IN ({q})")
    with db.connection() as c:
        rows = c.execute(f"SELECT start_time FROM activity_sessions WHERE {where}", sources).fetchall()
        days = {timeutil.local_date(timeutil.parse_iso(r["start_time"]), tz).isoformat() for r in rows}
        c.execute(
            f"""UPDATE activity_sessions SET classification_status='pending', category=NULL, confidence=NULL,
                       reason=NULL, is_wasted=0, classified_by=NULL, attempt_count=0, last_attempt_at=NULL
                WHERE {where}""", sources)
    return {"reset": len(rows), "days": sorted(days)}

# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
def pending_signature_groups(source: str) -> list[dict]:
    """Unique unclassified signatures (non-sensitive, attempts left) with representative fields."""
    with db.read_connection() as c:
        rows = c.execute(
            """SELECT signature, MIN(domain) AS domain, MIN(url) AS url, MIN(title) AS title,
                      SUM(duration) AS seconds, COUNT(*) AS visits
               FROM activity_sessions
               WHERE classification_status='pending' AND source=? AND sensitive=0 AND attempt_count < ?
               GROUP BY signature ORDER BY MIN(start_time)""",
            (source, settings.classify_max_attempts),
        ).fetchall()
    out = []
    for r in rows:
        parts = privacy.split_url(r["url"] or "")
        path = parts[1] if parts else "/"
        out.append({"signature": r["signature"], "domain": r["domain"], "path": path,
                    "title": r["title"] or "", "seconds": r["seconds"], "visits": r["visits"]})
    return out


def fetch_pending_live(limit: int) -> list[sqlite3.Row]:
    with db.read_connection() as c:
        return c.execute(
            """SELECT * FROM activity_sessions
               WHERE classification_status='pending' AND source=? AND sensitive=0 AND attempt_count < ?
               ORDER BY start_time LIMIT ?""",
            (SOURCE_LIVE, settings.classify_max_attempts, limit),
        ).fetchall()


def sessions_between(start: datetime, end: datetime, source: Optional[str] = None) -> list[sqlite3.Row]:
    """Rows overlapping [start, end)."""
    sql = "SELECT * FROM activity_sessions WHERE end_time > ? AND start_time < ?"
    args: list[Any] = [_iso(start), _iso(end)]
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY start_time"
    with db.read_connection() as c:
        return c.execute(sql, args).fetchall()


def count_by_status(source: Optional[str] = None) -> dict[str, int]:
    sql = "SELECT classification_status AS s, COUNT(*) AS n FROM activity_sessions"
    args: list[Any] = []
    if source:
        sql += " WHERE source=?"
        args.append(source)
    sql += " GROUP BY classification_status"
    with db.read_connection() as c:
        counts = {r["s"]: r["n"] for r in c.execute(sql, args)}
    return {STATUS_PENDING: 0, STATUS_CLASSIFIED: 0, STATUS_FAILED: 0, **counts}


def total_rows() -> int:
    with db.read_connection() as c:
        return c.execute("SELECT COUNT(*) AS n FROM activity_sessions").fetchone()["n"]
