"""Domain constants and lightweight row models (plain dataclasses, no ORM).

Pydantic is used only for FastAPI request bodies (see routes/*).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

# ---- categories ------------------------------------------------------------
CATEGORIES: tuple[str, ...] = (
    "focused_work",
    "learning",
    "research",
    "communication",
    "social_media",
    "entertainment",
    "shopping",
    "news",
    "productivity",
    "personal",
    "creative",
    "break",
    "ambiguous",
)
CATEGORY_SET = frozenset(CATEGORIES)

# "Productive" is a presentation grouping only; it is never used to define "wasted".
PRODUCTIVE_CATEGORIES = frozenset({"focused_work", "learning", "research", "productivity", "creative"})

# Groupings used by the (existing) website's four coloured segments.
UI_GROUPS: dict[str, tuple[str, ...]] = {
    "focus": ("focused_work", "productivity", "creative"),
    "learn": ("learning", "research"),
    "break": ("break",),
}

# ---- sources ---------------------------------------------------------------
SOURCE_HISTORY = "history_estimated"
SOURCE_LIVE = "extension_measured"
SOURCES = (SOURCE_HISTORY, SOURCE_LIVE)

# ---- classification status -------------------------------------------------
STATUS_PENDING = "pending"  # not yet successfully classified
STATUS_CLASSIFIED = "classified"  # has a semantic result (category may be 'ambiguous')
STATUS_FAILED = "failed"  # attempts exhausted (terminal)
STATUSES = (STATUS_PENDING, STATUS_CLASSIFIED, STATUS_FAILED)

# ---- who classified --------------------------------------------------------
BY_RULE = "rule"
BY_CACHE = "cache"
BY_LLM = "llm"
BY_OVERRIDE = "override"
BY_SENSITIVE = "sensitive"

# ---- analysis status (daily/monthly summaries) -----------------------------
AN_DETERMINISTIC = "deterministic_only"  # history-only days: never AI-analysed
AN_IN_PROGRESS = "in_progress"  # day/month not finished yet
AN_WAITING = "waiting"  # finished, but rows pending or budget exhausted
AN_PARTIAL = "partial"  # some batch observations saved, resume later
AN_COMPLETE = "complete"
AN_NO_DATA = "no_data"
AN_TERMINAL_OK = frozenset({AN_COMPLETE, AN_NO_DATA, AN_DETERMINISTIC})


@dataclass
class ActivitySession:
    id: str
    start_time: str
    end_time: str
    domain: str
    url: Optional[str]
    title: Optional[str]
    category: Optional[str]
    duration: int
    is_wasted: int
    confidence: Optional[float]
    reason: Optional[str]
    source: str
    classification_status: str
    activity_key: str
    attempt_count: int = 0
    last_attempt_at: Optional[str] = None
    signature: Optional[str] = None
    sensitive: int = 0
    classified_by: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "ActivitySession":
        keys = row.keys()
        return cls(**{f: row[f] for f in cls.__dataclass_fields__ if f in keys})

    def to_dict(self) -> dict:
        return asdict(self)


def ui_group_for(category: Optional[str], is_wasted: bool) -> str:
    """Map a session to the website's colour groups. Wasted wins; unknown -> other."""
    if is_wasted:
        return "distract"
    for group, cats in UI_GROUPS.items():
        if category in cats:
            return group
    return "other"
