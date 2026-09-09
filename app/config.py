"""
Application configuration.

Values are read from the process environment, then from a `.env` file if one
exists.  A tiny dependency-free .env loader is used so the app has no hard
requirement on `python-dotenv`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def _parse_value(value: str) -> str:
    """Interpret one side of a KEY=VALUE line the way dotenv users expect."""
    value = value.strip()
    if value and value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            # Quoted: keep everything inside the quotes ('#' included),
            # drop whatever follows the closing quote (usually a comment).
            return value[1:end]
        return value[1:]
    # Unquoted: a '#' at the start or after whitespace starts a comment.
    # A '#' glued onto the value ("pass#word") is kept, so secrets survive.
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value


def _load_env_file(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE file. Existing env wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        os.environ.setdefault(key, _parse_value(value))


_load_env_file(ENV_FILE)


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _csv(name: str, default: str) -> List[int]:
    raw = _str(name, default)
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or [80, 443]


@dataclass(frozen=True)
class Settings:
    # --- App ---
    app_name: str = field(default_factory=lambda: _str("APP_NAME", "Personal Content Tracker"))
    app_env: str = field(default_factory=lambda: _str("APP_ENV", "development"))
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY", "insecure-dev-key-change-me"))
    public_base_url: str = field(default_factory=lambda: _str("PUBLIC_BASE_URL", "http://localhost:8000"))
    host: str = field(default_factory=lambda: _str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))

    # --- Database ---
    database_url: str = field(default_factory=lambda: _str("DATABASE_URL", "sqlite:///data/tracker.db"))

    # --- Security ---
    session_cookie_name: str = field(default_factory=lambda: _str("SESSION_COOKIE_NAME", "pct_session"))
    session_max_age_days: int = field(default_factory=lambda: _int("SESSION_MAX_AGE_DAYS", 30))
    session_idle_minutes: int = field(default_factory=lambda: _int("SESSION_IDLE_MINUTES", 10080))
    remember_me_days: int = field(default_factory=lambda: _int("REMEMBER_ME_DAYS", 90))
    password_min_length: int = field(default_factory=lambda: _int("PASSWORD_MIN_LENGTH", 8))
    auth_rate_limit: int = field(default_factory=lambda: _int("AUTH_RATE_LIMIT", 10))
    # Import/restore uploads are rejected above this size.
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 12))
    auth_rate_window: int = field(default_factory=lambda: _int("AUTH_RATE_WINDOW_SECONDS", 300))

    # --- Outbound network ---
    fetch_timeout: float = field(default_factory=lambda: float(_int("FETCH_TIMEOUT_SECONDS", 8)))
    fetch_max_bytes: int = field(default_factory=lambda: _int("FETCH_MAX_BYTES", 1024 * 1024))
    fetch_allowed_ports: List[int] = field(default_factory=lambda: _csv("FETCH_ALLOWED_PORTS", "80,443"))
    fetch_block_private: bool = field(default_factory=lambda: _bool("FETCH_BLOCK_PRIVATE_NETWORKS", True))
    fetch_max_redirects: int = field(default_factory=lambda: _int("FETCH_MAX_REDIRECTS", 3))
    fetch_min_host_interval: int = field(default_factory=lambda: _int("FETCH_MIN_INTERVAL_PER_HOST_SECONDS", 30))
    fetch_max_per_run: int = field(default_factory=lambda: _int("FETCH_MAX_REQUESTS_PER_RUN", 60))

    # --- Scheduler ---
    enable_scheduler: bool = field(default_factory=lambda: _bool("ENABLE_SCHEDULER", True))
    check_interval_minutes: int = field(default_factory=lambda: _int("CHECK_INTERVAL_MINUTES", 90))
    url_health_interval_hours: int = field(default_factory=lambda: _int("URL_HEALTH_INTERVAL_HOURS", 24))
    update_check_interval_hours: int = field(default_factory=lambda: _int("UPDATE_CHECK_INTERVAL_HOURS", 12))

    # --- Optional external APIs ---
    tmdb_api_key: str = field(default_factory=lambda: _str("TMDB_API_KEY", ""))
    tmdb_read_token: str = field(default_factory=lambda: _str("TMDB_API_READ_TOKEN", ""))
    jikan_enabled: bool = field(default_factory=lambda: _bool("JIKAN_ENABLED", True))
    newsapi_key: str = field(default_factory=lambda: _str("NEWSAPI_KEY", ""))
    espn_enabled: bool = field(default_factory=lambda: _bool("ESPN_ENABLED", True))
    rss_enabled: bool = field(default_factory=lambda: _bool("RSS_ENABLED", True))

    # --- Metadata scraping / thumbnails ---
    enable_url_scraping: bool = field(default_factory=lambda: _bool("ENABLE_URL_METADATA_SCRAPING", True))
    enable_thumbnail_cache: bool = field(default_factory=lambda: _bool("ENABLE_THUMBNAIL_CACHE", True))
    thumb_width: int = field(default_factory=lambda: _int("THUMBNAIL_WIDTH", 320))
    thumb_height: int = field(default_factory=lambda: _int("THUMBNAIL_HEIGHT", 460))

    # --- Paths ---
    data_dir: Path = BASE_DIR / "data"
    thumb_dir: Path = BASE_DIR / "data" / "thumbs"
    cache_dir: Path = BASE_DIR / "data" / "cache"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cookie_secure(self) -> bool:
        """Send cookies only over HTTPS when running in production."""
        return self.is_production

    @property
    def has_tmdb(self) -> bool:
        return bool(self.tmdb_api_key or self.tmdb_read_token)

    @property
    def has_newsapi(self) -> bool:
        return bool(self.newsapi_key)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.thumb_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
