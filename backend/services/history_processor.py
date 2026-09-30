"""Raw Chrome visits -> clean, normalized visits (before duration estimation).

Drops useless entries (chrome://, extension pages, new tab, non-http), excluded domains,
and strips query strings/fragments. No LLM, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from services import privacy
from services.history_reader import RawVisit


@dataclass
class CleanVisit:
    start: datetime
    chrome_duration_seconds: Optional[float]
    domain: str
    path: str
    url: str          # normalized (no query/fragment)
    title: str        # normalized title
    signature: str


def clean_visits(raw: Iterable[RawVisit], excluded_domains: Iterable[str] = ()) -> list[CleanVisit]:
    excluded = list(excluded_domains)
    out: list[CleanVisit] = []
    for v in raw:
        parts = privacy.split_url(v.url)
        if parts is None:
            continue
        domain, path = parts
        if privacy.is_excluded_domain(domain, excluded):
            continue
        title = privacy.normalize_title(v.title)
        out.append(CleanVisit(
            start=v.start,
            chrome_duration_seconds=v.chrome_duration_seconds,
            domain=domain,
            path=path,
            url=privacy.normalized_url(domain, path),
            title=title,
            signature=privacy.make_signature(domain, path, title),
        ))
    out.sort(key=lambda c: c.start)
    return out
