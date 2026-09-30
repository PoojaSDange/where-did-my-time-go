"""Central configuration.

Everything tunable (model names, rate limits, thresholds, paths, modes) comes from
environment variables (loaded from backend/.env if present). Nothing is hardcoded
in the services. Tests can mutate attributes on the `settings` singleton.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")

# Deterministic extension id (derived from the public "key" in extension/manifest.json).
DEFAULT_EXTENSION_ID = "njcikckghkmmanpggmefpbicpllfmegm"


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _str(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip().lower() for p in _str(name, default).split(",") if p.strip()]


class Settings:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        # ---- mode / network -------------------------------------------------
        self.app_mode: str = _str("APP_MODE", "local").lower()  # local | demo
        self.host: str = _str("APP_HOST", "127.0.0.1")  # never 0.0.0.0 by default
        self.port: int = _int("APP_PORT", 8000)
        self.db_path: str = _str("DB_PATH", str(BACKEND_DIR / "data" / "wdmt.sqlite3"))
        self.data_dir: str = _str("DATA_DIR", str(BACKEND_DIR / "data"))
        self.frontend_dir: str = _str("FRONTEND_DIR", str(PROJECT_DIR / "frontend"))
        self.log_level: str = _str("LOG_LEVEL", "INFO")

        origins = _list("FRONTEND_ORIGINS")
        if not origins:
            origins = [f"http://localhost:{self.port}", f"http://127.0.0.1:{self.port}"]
        self.frontend_origins: list[str] = origins
        ext = _str("EXTENSION_ID", DEFAULT_EXTENSION_ID)
        self.extension_origin: str = _str("EXTENSION_ORIGIN", f"chrome-extension://{ext}")

        hosts = _list("ALLOWED_HOSTS")
        # DNS-rebinding defence: in local mode only these Host headers are served.
        self.allowed_hosts: list[str] = hosts or ["localhost", "127.0.0.1", "[::1]", "testserver"]

        # ---- demo mode ------------------------------------------------------
        self.demo_public_token: bool = _bool("DEMO_PUBLIC_TOKEN", True)
        self.demo_token: str = _str("DEMO_API_TOKEN", "demo-token-synthetic-data-only")
        self.demo_allow_ingest: bool = _bool("DEMO_ALLOW_INGEST", False)
        self.demo_ask_per_hour: int = _int("DEMO_ASK_PER_HOUR", 20)

        # ---- Groq (shared client) ------------------------------------------
        self.groq_api_key: str = _str("GROQ_API_KEY")
        self.groq_base_url: str = _str("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        self.groq_live_model: str = _str("GROQ_LIVE_MODEL", "qwen/qwen3.8-27b")
        self.groq_analysis_model: str = _str("GROQ_ANALYSIS_MODEL", "llama-3.3-70b-versatile")
        self.groq_agent_model: str = _str("GROQ_AGENT_MODEL", "llama-3.3-70b-versatile")
        self.groq_rpm: int = _int("GROQ_RPM", 30)
        self.groq_tpm: int = _int("GROQ_TPM", 6000)
        self.groq_tpd: int = _int("GROQ_TPD", 500000)
        # Tokens per day that ONLY the supervisor agent may use.
        self.groq_supervisor_reserve_tokens: int = _int("GROQ_SUPERVISOR_RESERVE_TOKENS", 50000)
        # Share of the non-reserved daily budget each priority may consume.
        self.groq_daily_share_analysis: float = _float("GROQ_DAILY_SHARE_ANALYSIS", 0.85)
        self.groq_daily_share_monthly: float = _float("GROQ_DAILY_SHARE_MONTHLY", 0.70)
        self.groq_max_retries: int = _int("GROQ_MAX_RETRIES", 3)
        self.groq_max_wait_seconds: float = _float("GROQ_MAX_WAIT_SECONDS", 30.0)
        self.groq_timeout_seconds: float = _float("GROQ_TIMEOUT_SECONDS", 60.0)
        # Optional JSON of extra body params for the live model (e.g. {"reasoning_effort":"none"}).
        self.groq_live_extra_json: str = _str("GROQ_LIVE_EXTRA_JSON", "{}")
        self.groq_breaker_threshold: int = _int("GROQ_BREAKER_THRESHOLD", 4)
        self.groq_breaker_cooldown_seconds: float = _float("GROQ_BREAKER_COOLDOWN_SECONDS", 300.0)

        # ---- Gemini (history bootstrap only) -------------------------------
        self.gemini_api_key: str = _str("GEMINI_API_KEY")
        self.gemini_base_url: str = _str(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        )
        self.gemini_model: str = _str("GEMINI_MODEL", "gemini-2.5-flash")
        self.gemini_rpm: int = _int("GEMINI_RPM", 10)
        self.gemini_chunk_items: int = _int("GEMINI_CHUNK_ITEMS", 80)
        self.gemini_chunk_chars: int = _int("GEMINI_CHUNK_CHARS", 9000)
        self.gemini_max_retries: int = _int("GEMINI_MAX_RETRIES", 3)
        self.gemini_timeout_seconds: float = _float("GEMINI_TIMEOUT_SECONDS", 60.0)
        # When Gemini itself is unavailable (bad key, quota used up, outage) history classification pauses
        # WITHOUT burning any page's attempts. The pause doubles per consecutive failure: base -> max.
        self.gemini_pause_base_minutes: float = _float("GEMINI_PAUSE_BASE_MINUTES", 15.0)
        self.gemini_pause_max_minutes: float = _float("GEMINI_PAUSE_MAX_MINUTES", 180.0)

        # ---- classification -------------------------------------------------
        self.classify_max_attempts: int = _int("CLASSIFY_MAX_ATTEMPTS", 5)
        self.classify_worker_interval_seconds: float = _float("CLASSIFY_WORKER_INTERVAL_SECONDS", 20.0)
        self.live_batch_tokens: int = _int("LIVE_BATCH_TOKENS", 1800)
        self.live_batch_max_items: int = _int("LIVE_BATCH_MAX_ITEMS", 40)
        self.cache_min_confidence: float = _float("CACHE_MIN_CONFIDENCE", 0.7)
        # A guess below this confidence is treated as "ambiguous" (weak guesses above it are kept, never cached/wasted).
        self.classify_min_confidence: float = _float("CLASSIFY_MIN_CONFIDENCE", 0.35)
        # A site's "usual category" (hint + fallback for ambiguous results) needs this much history.
        self.prior_min_sessions: int = _int("PRIOR_MIN_SESSIONS", 5)
        self.prior_min_share: float = _float("PRIOR_MIN_SHARE", 0.85)
        self.worker_backoff_base_seconds: float = _float("WORKER_BACKOFF_BASE_SECONDS", 30.0)
        self.worker_backoff_max_seconds: float = _float("WORKER_BACKOFF_MAX_SECONDS", 900.0)

        # ---- wasted-time rules (per session, not per page) -----------------
        self.waste_min_seconds: int = _int("WASTE_MIN_SECONDS", 180)
        self.waste_min_confidence: float = _float("WASTE_MIN_CONFIDENCE", 0.7)
        self.waste_long_seconds: int = _int("WASTE_LONG_SECONDS", 1200)
        self.work_start_hour: int = _int("WORK_START_HOUR", 9)
        self.work_end_hour: int = _int("WORK_END_HOUR", 18)

        # ---- history bootstrap ---------------------------------------------
        self.history_days: int = _int("HISTORY_DAYS", 60)
        self.chrome_history_path: str = _str("CHROME_HISTORY_PATH")
        self.history_estimate_cap_seconds: int = _int("HISTORY_ESTIMATE_CAP_SECONDS", 300)
        self.history_idle_gap_seconds: int = _int("HISTORY_IDLE_GAP_SECONDS", 1800)
        self.history_default_seconds: int = _int("HISTORY_DEFAULT_SECONDS", 30)
        self.history_max_visit_seconds: int = _int("HISTORY_MAX_VISIT_SECONDS", 1800)
        self.history_merge_gap_seconds: int = _int("HISTORY_MERGE_GAP_SECONDS", 30)
        self.history_chunk_days: int = _int("HISTORY_CHUNK_DAYS", 7)

        # ---- analysis / catch-up -------------------------------------------
        self.analysis_batch_tokens: int = _int("ANALYSIS_BATCH_TOKENS", 3000)
        self.analysis_max_daily_ai_days_per_run: int = _int("ANALYSIS_MAX_DAYS_PER_RUN", 3)
        self.catchup_interval_minutes: float = _float("CATCHUP_INTERVAL_MINUTES", 45.0)

        # ---- agent ----------------------------------------------------------
        self.agent_max_steps: int = _int("AGENT_MAX_STEPS", 5)
        self.agent_tool_result_chars: int = _int("AGENT_TOOL_RESULT_CHARS", 3500)
        self.agent_max_tokens: int = _int("AGENT_MAX_TOKENS", 600)
        # Ask AI waits out per-minute limits instead of failing; this caps the TOTAL waiting for one question.
        self.ask_max_wait_seconds: float = _float("ASK_MAX_WAIT_SECONDS", 300.0)

        # ---- privacy --------------------------------------------------------
        default_sensitive = (
            "mail.google.com,outlook.live.com,outlook.office.com,mail.yahoo.com,proton.me,"
            "hsbc.com,chase.com,bankofamerica.com,wellsfargo.com,paypal.com,"
            "1password.com,bitwarden.com,lastpass.com,"
            "mychart.com,webmd.com,zocdoc.com,practo.com"
        )
        self.sensitive_domains: list[str] = _list("SENSITIVE_DOMAINS", default_sensitive)
        self.title_max_chars: int = _int("TITLE_MAX_CHARS", 120)


settings = Settings()
