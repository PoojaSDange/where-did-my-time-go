"""Synthetic data for the optional hosted DEMO (APP_MODE=demo).

100% invented: fixed RNG seed, made-up titles, no real browsing data. The demo DB is disposable
(re-seeded on startup when empty). It reproduces the real lifecycle so every page has something
true to show: ~46 days of *estimated* history (conservative, fewer/shorter sessions), then ~14
days of *measured* live data after a synthetic activation time, with deterministic daily/monthly
summaries and template narratives (no LLM needed) clearly labelled as synthetic.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

import database as db
from models import SOURCE_HISTORY, SOURCE_LIVE
from services import activity_storage as storage
from services import analytics_tools as at
from services import monthly_analysis, privacy, timeutil
from services.waste import decide_is_wasted

DEMO_TZ = "Europe/London"
HISTORY_DAYS = 46
LIVE_DAYS = 14

# (domain, path, title, category, typical minutes range)
WORK = [
    ("github.com", "/acme/payments-service/pull/482", "Review: add retry policy to settlement job", "focused_work", (18, 55)),
    ("docs.google.com", "/document/d/synthetic", "Q3 reconciliation design notes", "focused_work", (12, 40)),
    ("notion.so", "/team/sprint-planning", "Sprint planning board", "productivity", (10, 35)),
    ("stackoverflow.com", "/questions/synthetic", "How to batch idempotent writes in SQLite", "learning", (5, 20)),
    ("mail.google.com", "/mail/u/0/", "Inbox", "communication", (4, 14)),
    ("hsbc.com", "/personal/accounts", "Account overview", "personal", (2, 6)),
    ("figma.com", "/file/synthetic", "Onboarding flow v3", "creative", (10, 30)),
]
LEARN = [
    ("coursera.org", "/learn/ml-foundations", "Machine learning foundations - week 3", "learning", (20, 55)),
    ("youtube.com", "/watch", "Distributed systems lecture (synthetic)", "learning", (15, 45)),
    ("developer.mozilla.org", "/en-US/docs/Web/API", "Web APIs reference", "learning", (8, 25)),
    ("arxiv.org", "/abs/synthetic", "A survey of retrieval-augmented generation", "research", (10, 30)),
]
DISTRACT = [
    ("instagram.com", "/", "Instagram", "social_media", (6, 30)),
    ("youtube.com", "/shorts", "Shorts feed", "entertainment", (10, 45)),
    ("reddit.com", "/r/all", "Front page (synthetic)", "social_media", (8, 35)),
    ("netflix.com", "/browse", "Browse", "entertainment", (20, 60)),
]
NEUTRAL = [
    ("bbc.com", "/news", "Top stories", "news", (4, 15)),
    ("amazon.com", "/dp/synthetic", "Desk lamp (synthetic listing)", "shopping", (5, 18)),
    ("music.youtube.com", "/", "Focus playlist", "break", (10, 25)),
]


def _plan_day(rng: random.Random, weekday: int) -> list[tuple]:
    """Ordered (minute_of_day, spec) blocks for one local day."""
    blocks: list[tuple] = []
    if weekday < 5:
        t = rng.randint(9, 10) * 60 + rng.randint(0, 40)
        for _ in range(rng.randint(5, 8)):
            spec = rng.choice(WORK)
            blocks.append((t, spec))
            t += rng.randint(*spec[4]) + rng.randint(2, 12)
        t = 13 * 60 + rng.randint(0, 30)
        blocks.append((t, rng.choice(NEUTRAL)))
        t += rng.randint(15, 35)
        for _ in range(rng.randint(1, 3)):
            blocks.append((t, rng.choice(WORK)))
            t += rng.randint(15, 45)
        if rng.random() < 0.75:  # the recurring late-afternoon dip
            t = 16 * 60 + rng.randint(0, 40)
            for _ in range(rng.randint(1, 3)):
                spec = rng.choice(DISTRACT)
                blocks.append((t, spec))
                t += rng.randint(*spec[4]) + rng.randint(1, 8)
        t = 19 * 60 + rng.randint(0, 60)
        for _ in range(rng.randint(1, 3)):
            spec = rng.choice(LEARN)
            blocks.append((t, spec))
            t += rng.randint(*spec[4]) + rng.randint(3, 10)
    else:
        t = 11 * 60 + rng.randint(0, 90)
        for _ in range(rng.randint(3, 6)):
            spec = rng.choice(DISTRACT + NEUTRAL + LEARN[:2])
            blocks.append((t, spec))
            t += rng.randint(*spec[4]) + rng.randint(5, 40)
    return sorted(blocks, key=lambda b: b[0])


def _insert(c, rid: str, start: datetime, dur: int, spec: tuple, source: str, tz) -> None:
    domain, path, title, cat, _ = spec
    sig = privacy.make_signature(domain, path, title)
    sensitive = 1 if privacy.is_sensitive_domain(domain) else 0
    wasted = int(decide_is_wasted(cat, dur, 0.9, start, tz))
    c.execute(
        """INSERT OR IGNORE INTO activity_sessions
           (id,start_time,end_time,domain,url,title,category,duration,is_wasted,confidence,reason,source,
            classification_status,activity_key,signature,sensitive,classified_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'classified', ?,?,?,?)""",
        (rid, timeutil.to_iso(start), timeutil.to_iso(start + timedelta(seconds=dur)), domain,
         privacy.normalized_url(domain, path), title, cat, dur, wasted, 0.9, "synthetic demo data", source,
         rid, sig, sensitive, "sensitive" if sensitive else "rule"),
    )


def seed_if_empty() -> bool:
    """Seed the disposable demo DB. Returns True if it seeded."""
    db.init_db()
    if storage.total_rows() > 0:
        return False
    rng = random.Random(20260929)
    db.set_state("timezone", DEMO_TZ)
    tz = timeutil.get_tz(DEMO_TZ)
    now = timeutil.utcnow()
    today = timeutil.today_local(tz, now)
    first = today - timedelta(days=HISTORY_DAYS + LIVE_DAYS - 1)
    activation_day = first + timedelta(days=HISTORY_DAYS)
    activation = datetime.combine(activation_day, datetime.min.time(), tzinfo=tz).replace(hour=9, minute=30).astimezone(timeutil.UTC)

    n = 0
    with db.connection() as c:
        for day in timeutil.iter_days(first, today):
            local_midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            for minute, spec in _plan_day(rng, day.weekday()):
                start = (local_midnight + timedelta(minutes=minute)).astimezone(timeutil.UTC)
                if start >= now:
                    continue
                if start < activation:
                    source = SOURCE_HISTORY
                    # history is ESTIMATED: conservative caps, some visits dropped as noise
                    dur = min(rng.randint(*spec[4]) * 60, 300 + rng.choice([0, 0, 120, 600]))
                    if rng.random() < 0.12:
                        continue
                    if start + timedelta(seconds=dur) > activation:
                        dur = int((activation - start).total_seconds())
                        if dur < 1:
                            continue
                else:
                    source = SOURCE_LIVE
                    dur = rng.randint(*spec[4]) * 60 + rng.randint(0, 59)
                    dur = min(dur, int((now - start).total_seconds()))
                    if dur < 5:
                        continue
                n += 1
                _insert(c, f"demo-{n:06d}", start, dur, spec, source, tz)
        # a few rows the classifier "gave up on", so the coverage UI has something honest to show
        c.execute("""UPDATE activity_sessions SET classification_status='failed', category=NULL, is_wasted=0,
                     confidence=NULL, reason=NULL, classified_by=NULL WHERE id IN
                     (SELECT id FROM activity_sessions WHERE source='history_estimated' ORDER BY id LIMIT 3)""")

    db.set_state("activation_ts", timeutil.to_iso(activation))
    db.set_state("bootstrap_completed", True)
    db.set_state("bootstrap_state", {"status": "completed", "stage": "done", "message": "Synthetic demo data",
                                     "done": 1, "total": 1})
    db.set_state("demo_seeded", True)

    # deterministic summaries for every day (same code path as the real product)
    for day in timeutil.iter_days(first, today):
        at.refresh_day(day, DEMO_TZ, now)
    _template_narratives(first, today, now)
    return True


def _template_narratives(first: date, today: date, now: datetime) -> None:
    """Template (non-LLM) narratives for completed live days and completed months. Clearly synthetic."""
    fd = at.fmt_duration
    for d in at.list_daily(first, today - timedelta(days=1)):
        if d["analysis_status"] != "waiting":
            continue
        cats = d["category_seconds"]
        focus = sum(cats.get(k, 0) for k in ("focused_work", "productivity", "creative"))
        top = d["top_distractions"][0] if d["top_distractions"] else None
        ai = {
            "summary": f"You spent {fd(focus)} in focused work and {fd(cats.get('learning', 0) + cats.get('research', 0))} learning. "
                       + (f"The main pull was {top['domain']} ({fd(top['seconds'])} flagged distracting)." if top
                          else "Nothing was flagged as distracting.")
                       + " (Synthetic demo narrative.)",
            "highlights": [f"{fd(d['measured_seconds'])} measured live"],
            "distractions": [f"{top['domain']} - {fd(top['seconds'])}"] if top else [],
            "suggestion": "Protect one 45-minute block in the late afternoon, when distractions usually creep in.",
            "confidence": "medium",
        }
        with db.connection() as c:
            c.execute("UPDATE daily_summaries SET ai_analysis=?, ai_model='synthetic-demo', analysis_status='complete', "
                      "analyzed_at=? WHERE day=?", (json.dumps(ai), timeutil.to_iso(now), d["day"]))
    cur = timeutil.month_key(today)
    for m in monthly_analysis.data_months(DEMO_TZ, now):
        agg, _ = monthly_analysis.refresh_month(m, DEMO_TZ, now)
        if m >= cur:
            continue
        cats = agg["category_seconds"]
        ai = {
            "summary": f"{m}: {fd(agg['total_seconds'])} of browsing, {fd(agg['estimated_seconds'])} of it estimated from history "
                       f"and {fd(agg['measured_seconds'])} measured live. Focused work led ({fd(cats.get('focused_work', 0))}); "
                       f"{fd(agg['wasted_seconds'])} was flagged as distracting. (Synthetic demo narrative.)",
            "patterns": ["Late-afternoon dips recur on most weekdays."], "wins": ["Consistent evening learning."],
            "watch_outs": ["Weekend evenings drift toward entertainment."],
            "suggestion": "Try a 45-minute protected block around 4 PM.", "confidence": "medium",
        }
        with db.connection() as c:
            c.execute("UPDATE monthly_summaries SET ai_analysis=?, ai_model='synthetic-demo', analysis_status='complete', "
                      "analyzed_at=? WHERE month=?", (json.dumps(ai), timeutil.to_iso(now), m))
