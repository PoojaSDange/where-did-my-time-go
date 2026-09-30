"""Time helpers.

Rules of the project:
  * every stored timestamp is UTC, formatted 'YYYY-MM-DDTHH:MM:SSZ' (sortable as text);
  * a "day" is always the user's LOCAL day (IANA timezone kept in app_state);
  * sessions crossing local midnight are split at the boundary.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
_CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000  # 1601-01-01 -> 1970-01-01


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).strftime(ISO_FMT)


def parse_iso(s: str) -> datetime:
    """Parse ISO-8601 (with Z / offset / fractional seconds) into an aware UTC datetime."""
    s = s.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).replace(microsecond=0)


def chrome_us_to_dt(chrome_us: int) -> datetime:
    """Chrome stores time as microseconds since 1601-01-01 UTC."""
    return datetime.fromtimestamp((chrome_us - _CHROME_EPOCH_OFFSET_US) / 1_000_000, tz=UTC).replace(microsecond=0)


def dt_to_chrome_us(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1_000_000) + _CHROME_EPOCH_OFFSET_US


# ---- timezone --------------------------------------------------------------
def valid_tz(name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def get_tz(name: Optional[str]) -> ZoneInfo:
    if valid_tz(name):
        return ZoneInfo(name)  # type: ignore[arg-type]
    return ZoneInfo("UTC")


def system_timezone_name() -> str:
    """Best-effort detection of the OS IANA timezone; falls back to UTC."""
    try:
        import tzlocal  # type: ignore

        name = str(tzlocal.get_localzone_name())
        if valid_tz(name):
            return name
    except Exception:
        pass
    env = os.environ.get("TZ")
    if valid_tz(env):
        return env  # type: ignore[return-value]
    try:
        tzfile = Path("/etc/timezone")
        if tzfile.exists():
            name = tzfile.read_text().strip()
            if valid_tz(name):
                return name
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = os.readlink(link)
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
                if valid_tz(name):
                    return name
    except Exception:
        pass
    return "UTC"


# ---- local day helpers -----------------------------------------------------
def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC [start, end) of a local calendar day (DST-safe: uses next local midnight)."""
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def today_local(tz: ZoneInfo, now: Optional[datetime] = None) -> date:
    return local_date(now or utcnow(), tz)


def split_at_local_midnight(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Split [start, end) at every local-midnight boundary. Returns >=1 segments."""
    if end <= start:
        return [(start, end)]
    segments: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        day = local_date(cur, tz)
        _, day_end = local_day_bounds(day, tz)
        seg_end = min(end, day_end)
        segments.append((cur, seg_end))
        cur = seg_end
    return segments


def iter_days(first: date, last: date) -> Iterator[date]:
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_bounds(month: str, tz: ZoneInfo) -> tuple[date, date]:
    """First and last local date of 'YYYY-MM'."""
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, nxt - timedelta(days=1)


def shift_month(month: str, delta: int) -> str:
    y, m = (int(x) for x in month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


# ---- range parsing ---------------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_local_point(s: str, tz: ZoneInfo, end_of_day: bool) -> datetime:
    s = s.strip()
    if _DATE_RE.match(s):
        d = date.fromisoformat(s)
        start, end = local_day_bounds(d, tz)
        return end if end_of_day else start
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC).replace(microsecond=0)


def resolve_range(spec: str, tz: ZoneInfo, now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """Turn a range spec into a UTC [start, end) window plus a human label.

    Accepted: 6h 12h 24h 48h today yesterday 7d/last_7_days 30d/last_30_days
    this_week last_week (Mon-start) month/this_month last_month all
    and custom 'A..B' where A/B are local dates (inclusive) or ISO datetimes.
    """
    now = (now or utcnow()).astimezone(UTC)
    spec = (spec or "today").strip().lower()
    today = local_date(now, tz)

    m = re.fullmatch(r"(\d+)h", spec)
    if m:
        return now - timedelta(hours=int(m.group(1))), now, f"last {m.group(1)} hours"

    if spec == "today":
        s, e = local_day_bounds(today, tz)
        return s, e, "today"
    if spec == "yesterday":
        y = today - timedelta(days=1)
        s, e = local_day_bounds(y, tz)
        return s, e, "yesterday"
    if spec in ("7d", "last_7_days"):
        s, _ = local_day_bounds(today - timedelta(days=6), tz)
        _, e = local_day_bounds(today, tz)
        return s, e, "last 7 days"
    if spec in ("30d", "last_30_days"):
        s, _ = local_day_bounds(today - timedelta(days=29), tz)
        _, e = local_day_bounds(today, tz)
        return s, e, "last 30 days"
    if spec == "this_week":
        monday = today - timedelta(days=today.weekday())
        s, _ = local_day_bounds(monday, tz)
        _, e = local_day_bounds(today, tz)
        return s, e, "this week (Mon-today)"
    if spec == "last_week":
        monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
        s, _ = local_day_bounds(monday, tz)
        _, e = local_day_bounds(monday + timedelta(days=6), tz)
        return s, e, "last week (Mon-Sun)"
    if spec in ("month", "this_month"):
        first = today.replace(day=1)
        s, _ = local_day_bounds(first, tz)
        _, e = local_day_bounds(today, tz)
        return s, e, "this month"
    if spec == "last_month":
        cur = month_key(today)
        first, last = month_bounds(shift_month(cur, -1), tz)
        s, _ = local_day_bounds(first, tz)
        _, e = local_day_bounds(last, tz)
        return s, e, "last month"
    if spec == "all":
        return datetime(2000, 1, 1, tzinfo=UTC), now + timedelta(days=1), "all time"
    if ".." in spec:
        a, b = spec.split("..", 1)
        s = _parse_local_point(a, tz, end_of_day=False)
        e = _parse_local_point(b, tz, end_of_day=True)
        if e <= s:
            raise ValueError("range end must be after start")
        return s, e, f"{a.strip()} to {b.strip()}"
    if _DATE_RE.match(spec):
        s, e = local_day_bounds(date.fromisoformat(spec), tz)
        return s, e, spec
    raise ValueError(f"unknown range '{spec}'")


def days_in_window(start: datetime, end: datetime, tz: ZoneInfo) -> list[date]:
    first = local_date(start, tz)
    last = local_date(end - timedelta(seconds=1), tz)
    return list(iter_days(first, last))
