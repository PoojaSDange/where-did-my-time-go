"""Deterministic domain rules. Only UNAMBIGUOUS domains are listed.

Deliberately absent (need title context, so they go to an LLM): youtube.com, google.com,
reddit.com, linkedin.com, AI chat tools, generic blogs. Category != wasted; these rules
only assign a category. `is_wasted` is decided per session in services/waste.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.privacy import normalize_domain


@dataclass(frozen=True)
class RuleResult:
    category: str
    confidence: float
    reason: str


# (domain suffix, category). Longest / most specific suffix wins.
_DOMAIN_RULES: dict[str, str] = {}


def _add(category: str, *domains: str) -> None:
    for d in domains:
        _DOMAIN_RULES[d] = category


_add("communication", "slack.com", "teams.microsoft.com", "zoom.us", "meet.google.com", "web.whatsapp.com",
     "web.telegram.org", "discord.com", "messenger.com", "chat.google.com", "webex.com", "mail.google.com",
     "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com", "proton.me")
_add("focused_work", "github.com", "gitlab.com", "bitbucket.org", "vscode.dev", "github.dev", "replit.com",
     "codesandbox.io", "stackblitz.com", "colab.research.google.com", "console.cloud.google.com",
     "console.aws.amazon.com", "portal.azure.com", "vercel.com", "netlify.com", "render.com",
     "docs.google.com", "sheets.google.com", "slides.google.com", "overleaf.com", "atlassian.net",
     "jira.com", "linear.app", "office.com", "sharepoint.com", "onedrive.live.com", "jupyter.org")
_add("productivity", "notion.so", "calendar.google.com", "trello.com", "asana.com", "todoist.com", "airtable.com",
     "drive.google.com", "dropbox.com", "evernote.com", "clickup.com", "monday.com", "miro.com", "obsidian.md",
     "1password.com", "bitwarden.com", "lastpass.com")
_add("creative", "figma.com", "canva.com", "behance.net", "dribbble.com", "adobe.com", "photopea.com",
     "soundtrap.com", "bandlab.com", "unsplash.com")
_add("learning", "coursera.org", "udemy.com", "edx.org", "khanacademy.org", "leetcode.com", "hackerrank.com",
     "geeksforgeeks.org", "w3schools.com", "developer.mozilla.org", "realpython.com", "docs.python.org",
     "freecodecamp.org", "kaggle.com", "pluralsight.com", "codecademy.com", "brilliant.org", "udacity.com",
     "stackoverflow.com", "stackexchange.com", "javatpoint.com", "tutorialspoint.com", "codewars.com",
     "nptel.ac.in", "swayam.gov.in", "skillshare.com", "datacamp.com", "roadmap.sh")
_add("research", "scholar.google.com", "arxiv.org", "wikipedia.org", "pubmed.ncbi.nlm.nih.gov", "jstor.org",
     "researchgate.net", "semanticscholar.org", "sciencedirect.com", "ieee.org", "acm.org", "nature.com",
     "wikimedia.org", "britannica.com")
_add("social_media", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "snapchat.com",
     "pinterest.com", "tumblr.com", "threads.net", "quora.com", "9gag.com", "bsky.app")
_add("entertainment", "netflix.com", "primevideo.com", "hotstar.com", "disneyplus.com", "twitch.tv", "hulu.com",
     "crunchyroll.com", "imdb.com", "spotify.com", "soundcloud.com", "jiocinema.com", "sonyliv.com",
     "zee5.com", "max.com", "hbomax.com", "vimeo.com", "steampowered.com", "epicgames.com", "miniclip.com",
     "chess.com", "lichess.org", "netflix.net")
_add("shopping", "amazon.com", "amazon.in", "amazon.co.uk", "flipkart.com", "ebay.com", "etsy.com",
     "aliexpress.com", "myntra.com", "walmart.com", "target.com", "bestbuy.com", "meesho.com", "ajio.com",
     "nykaa.com", "swiggy.com", "zomato.com", "bigbasket.com", "zeptonow.com", "blinkit.com")
_add("news", "nytimes.com", "bbc.com", "bbc.co.uk", "cnn.com", "theguardian.com", "reuters.com",
     "news.ycombinator.com", "timesofindia.indiatimes.com", "ndtv.com", "thehindu.com", "news.google.com",
     "washingtonpost.com", "bloomberg.com", "economictimes.indiatimes.com", "hindustantimes.com",
     "indianexpress.com", "apnews.com", "aljazeera.com", "ft.com", "wsj.com", "theverge.com", "techcrunch.com")
# Sensitive-by-nature (banking/health) that people commonly hit: generic personal.
_add("personal", "paypal.com", "hsbc.com", "chase.com", "bankofamerica.com", "wellsfargo.com", "webmd.com")

# More specific host/path overrides evaluated BEFORE the suffix table.
_SPECIFIC_HOSTS: dict[str, str] = {
    "aws.amazon.com": "focused_work",
    "console.aws.amazon.com": "focused_work",
    "docs.aws.amazon.com": "learning",
    "aws.amazon.com/training": "learning",
    "music.youtube.com": "entertainment",
    "studio.youtube.com": "creative",
    "developers.google.com": "learning",
    "cloud.google.com": "focused_work",
    "docs.github.com": "learning",
    "learn.microsoft.com": "learning",
    "docs.microsoft.com": "learning",
    "medium.com": None,  # type: ignore[dict-item]  # ambiguous: intentionally not classified
}
# (domain, path prefix) -> category
_PATH_RULES: list[tuple[str, str, str]] = [
    ("youtube.com", "/shorts", "entertainment"),
    ("youtube.com", "/gaming", "entertainment"),
    ("google.com", "/maps", "personal"),
    ("google.com", "/travel", "personal"),
    ("google.com", "/flights", "personal"),
    ("google.com", "/finance", "news"),
    ("github.com", "/marketplace", "research"),
]

RULE_CONFIDENCE = 0.9


def classify(domain: str, path: str = "/", title: Optional[str] = None) -> Optional[RuleResult]:
    """Return a RuleResult for known unambiguous domains, else None (needs an LLM)."""
    d = normalize_domain(domain)
    path = path or "/"

    for host, prefix, cat in _PATH_RULES:
        if (d == host or d.endswith("." + host)) and path.startswith(prefix):
            return RuleResult(cat, RULE_CONFIDENCE, f"rule: {host}{prefix}")

    if d in _SPECIFIC_HOSTS:
        cat = _SPECIFIC_HOSTS[d]
        return RuleResult(cat, RULE_CONFIDENCE, f"rule: {d}") if cat else None

    # Walk from most specific suffix to least: a.b.c.com -> b.c.com -> c.com
    labels = d.split(".")
    for i in range(len(labels) - 1):
        suffix = ".".join(labels[i:])
        if suffix in _SPECIFIC_HOSTS:
            cat = _SPECIFIC_HOSTS[suffix]
            return RuleResult(cat, RULE_CONFIDENCE, f"rule: {suffix}") if cat else None
        cat = _DOMAIN_RULES.get(suffix)
        if cat:
            return RuleResult(cat, RULE_CONFIDENCE, f"rule: {suffix}")
    return None
