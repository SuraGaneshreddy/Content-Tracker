"""
Metadata extraction.

Two responsibilities:

1. ``scrape_page`` — fetch a user-supplied URL through the SSRF-safe fetcher
   and pull out whatever public metadata the page itself exposes (title,
   og:image, description, and the highest chapter/episode number mentioned).
2. ``fetch_metadata`` — ask a category-appropriate public API (TMDB, Jikan)
   for cover art / synopsis when the page scraping cannot provide it.

Everything here is best-effort: any failure returns an empty result and the
UI falls back to manual entry.  All remote text is treated as untrusted and
is sanitised before it is stored.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from ..config import settings
from ..constants import CATEGORY_ANIME, CATEGORY_MOVIE, SERIES_CATEGORIES
from ..utils.safe_http import FetchError, ThrottledError, UnsafeUrlError, safe_get

TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
WS_RE = re.compile(r"\s+")

# "Chapter 125", "Ch. 12", "chap 125.5", "第125话", "화 125", "EP 8", "Episode 8", "S02E04"
CHAPTER_PATTERNS = [
    re.compile(r"chapter\s*\.?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bchap\.?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bch\.?\s*(\d+(?:\.\d+)?)\b", re.I),
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话話章]", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*화", re.I),
]
EPISODE_PATTERNS = [
    re.compile(r"episode\s*\.?\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bep\.?\s*(\d+(?:\.\d+)?)\b", re.I),
    re.compile(r"\be(\d{1,4})\b", re.I),
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[集集话]", re.I),
]
SEASON_PATTERN = re.compile(r"season\s*\.?\s*(\d{1,2})|s(\d{1,2})e\d{1,4}", re.I)


@dataclass
class Metadata:
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    chapter: Optional[float] = None
    episode: Optional[float] = None
    season: Optional[int] = None
    site_name: Optional[str] = None
    source: str = ""
    error: Optional[str] = None  # set when the page could not be read at all
    raw: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error is not None

    def merge(self, other: "Metadata") -> "Metadata":
        """Fill gaps in self from other (self wins)."""
        for name in ("title", "description", "image_url", "chapter", "episode", "season", "site_name"):
            if getattr(self, name) in (None, "") and getattr(other, name) not in (None, ""):
                setattr(self, name, getattr(other, name))
        if not self.source:
            self.source = other.source
        return self


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------
def clean_text(value: Optional[str], limit: int = 600) -> Optional[str]:
    """Strip tags/entities/whitespace. External text is never trusted."""
    if not value:
        return None
    text = TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    # Remove control characters.
    text = "".join(ch for ch in text if ch >= " " or ch in "\n")
    return text[:limit].strip() or None


def clean_url(value: Optional[str], base: Optional[str] = None) -> Optional[str]:
    """Absolute-ise and validate an image/link URL. Returns None if unusable."""
    if not value:
        return None
    value = str(value).strip()
    if base:
        try:
            value = urljoin(base, value)
        except ValueError:
            return None
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    # Only ever *link* to public hosts; never to internal addresses.
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]") or host.endswith(".local"):
        return None
    return value[:2048]


def _first(patterns: list[re.Pattern], text: str) -> Optional[float]:
    """Highest number matched by any pattern (the newest chapter mentioned)."""
    best: Optional[float] = None
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = next((g for g in match.groups() if g), None)
            if raw is None:
                continue
            try:
                number = float(raw)
            except ValueError:
                continue
            if number > 100000:  # not a chapter number
                continue
            if best is None or number > best:
                best = number
    return best


# ---------------------------------------------------------------------------
# Page scraping
# ---------------------------------------------------------------------------
def _extract_meta_tags(text: str) -> dict:
    """Very small <meta> parser (no HTML library dependency, tolerant of junk)."""
    out: dict[str, str] = {}
    for match in re.finditer(r"<meta\b[^>]*>", text[:200_000], re.I | re.S):
        tag = match.group(0)
        name_m = re.search(r'(?:name|property|itemprop)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        content_m = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if not name_m or not content_m:
            continue
        out[name_m.group(1).strip().lower()] = content_m.group(1).strip()
    return out


def _extract_title(text: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", text[:200_000], re.I | re.S)
    return clean_text(m.group(1), 200) if m else None


def scrape_page(url: str, *, force: bool = False) -> Metadata:
    """
    Fetch a page and pull public metadata out of it.

    Only the first ``FETCH_MAX_BYTES`` are read, only ``text/html`` is parsed,
    and only the head section is inspected for meta tags.
    """
    meta = Metadata(source="page")
    if not settings.enable_url_scraping:
        return meta

    try:
        result = safe_get(
            url,
            accept="text/html,application/xhtml+xml",
            force=force,
            max_bytes=min(settings.fetch_max_bytes, 512 * 1024),
        )
    except (UnsafeUrlError, FetchError, ThrottledError) as exc:
        # Report the failure so callers never treat "could not read the page"
        # as "no update available".
        meta.error = str(exc) or "Could not check this source."
        return meta

    if result.status_code >= 400:
        meta.error = f"The page returned HTTP {result.status_code}."
        return meta
    if "html" not in result.content_type:
        meta.error = "That URL does not return a web page."
        return meta

    text = result.text
    tags = _extract_meta_tags(text)

    meta.title = clean_text(
        tags.get("og:title") or tags.get("twitter:title") or tags.get("title") or _extract_title(text),
        200,
    )
    meta.description = clean_text(
        tags.get("og:description") or tags.get("description") or tags.get("twitter:description"),
        600,
    )
    meta.image_url = clean_url(
        tags.get("og:image") or tags.get("og:image:secure_url") or tags.get("twitter:image") or tags.get("twitter:image:src") or tags.get("image"),
        base=result.final_url or url,
    )
    meta.site_name = clean_text(tags.get("og:site_name"), 80)

    # Chapter / episode hints: look in the title first, then the page text.
    haystack = " ".join(
        [meta.title or "", _extract_title(text) or "", text[:120_000]]
    )
    chapter = _first(CHAPTER_PATTERNS, haystack)
    episode = _first(EPISODE_PATTERNS, haystack)
    if chapter is not None:
        meta.chapter = chapter
    if episode is not None:
        meta.episode = episode
    season_match = SEASON_PATTERN.search(haystack)
    if season_match:
        raw = next((g for g in season_match.groups() if g), None)
        if raw and int(raw) <= 60:
            meta.season = int(raw)
    return meta


# ---------------------------------------------------------------------------
# Public APIs (optional)
# ---------------------------------------------------------------------------
def _cache_get(db, key: str) -> Optional[dict]:
    from datetime import datetime

    from sqlalchemy import select

    from ..models import ExternalCache

    row = db.scalar(select(ExternalCache).where(ExternalCache.cache_key == key))
    if row is None:
        return None
    if row.expires_at and row.expires_at < datetime.now():
        return None
    return row.payload


def _cache_set(db, key: str, payload: dict, ttl_seconds: int) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from ..models import ExternalCache, utcnow

    row = db.scalar(select(ExternalCache).where(ExternalCache.cache_key == key))
    if row is None:
        row = ExternalCache(cache_key=key)
        db.add(row)
    row.payload = payload
    row.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    db.commit()


def jikan_search_anime(query: str) -> list[dict]:
    """
    Search Jikan (public, keyless MyAnimeList mirror) for anime titles.

    Returns a normalised list of
    ``{id, title, image_url, description, episodes, season, year, score, genres, url}``.
    """
    """Real Jikan search (kept separate so it is easy to mock in tests)."""
    if not settings.jikan_enabled or not query.strip():
        return []
    from urllib.parse import urlencode

    params = urlencode({"q": query, "limit": 8, "sfw": "true"})
    try:
        result = safe_get(
            f"https://api.jikan.moe/v4/anime?{params}",
            accept="application/json",
            force=True,
            max_bytes=768 * 1024,
        )
    except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
        # Any lookup failure is a "no data" answer, never an error page.
        return []
    if result.status_code != 200:
        return []
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return []

    items: list[dict] = []
    for entry in (data.get("data") or [])[:8]:
        images = (entry.get("images") or {}).get("jpg") or {}
        image = (images.get("large_image_url") or images.get("image_url") or "")
        items.append(
            {
                "id": str(entry.get("mal_id") or ""),
                "title": clean_text(entry.get("title"), 200) or "",
                "image_url": clean_url(image),
                "description": clean_text(entry.get("synopsis"), 600),
                "episodes": entry.get("episodes"),
                "season": (entry.get("season") or "").capitalize() or None,
                "year": entry.get("year"),
                "score": entry.get("score"),
                "genres": [g.get("name") for g in (entry.get("genres") or []) if g.get("name")],
                "url": entry.get("url"),
            }
        )
    return items


def jikan_latest_episode(mal_id: str) -> Optional[dict]:
    """Return the newest known episode number for a MAL id, or None."""
    if not settings.jikan_enabled or not mal_id:
        return None
    try:
        result = safe_get(
            f"https://api.jikan.moe/v4/anime/{mal_id}/episodes",
            accept="application/json",
            force=True,
            max_bytes=512 * 1024,
        )
    except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
        # Any lookup failure is a "no data" answer, never an error page.
        return None
    if result.status_code != 200:
        return None
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return None
    episodes = data.get("data") or []
    if not episodes:
        return None
    last = episodes[-1]
    return {
        "episode": last.get("mal_id"),
        "title": clean_text(last.get("title"), 120),
        "aired": last.get("aired"),
    }


def jikan_season_current() -> list[dict]:
    """Currently airing anime (used by the Anime updates page)."""
    if not settings.jikan_enabled:
        return []
    try:
        result = safe_get(
            "https://api.jikan.moe/v4/seasons/now?limit=24",
            accept="application/json",
            force=True,
            max_bytes=1024 * 1024,
        )
    except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
        # Any lookup failure is a "no data" answer, never an error page.
        return []
    if result.status_code != 200:
        return []
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return []
    out = []
    for entry in (data.get("data") or [])[:24]:
        images = (entry.get("images") or {}).get("jpg") or {}
        out.append(
            {
                "id": str(entry.get("mal_id") or ""),
                "title": clean_text(entry.get("title"), 200) or "",
                "image_url": clean_url(images.get("large_image_url") or images.get("image_url")),
                "episodes": entry.get("episodes"),
                "score": entry.get("score"),
                "type": entry.get("type"),
                "genres": [g.get("name") for g in (entry.get("genres") or []) if g.get("name")][:4],
                "url": entry.get("url"),
                "year": entry.get("year"),
            }
        )
    return out


def jikan_season_upcoming() -> list[dict]:
    """Upcoming anime (used by the Anime updates page)."""
    if not settings.jikan_enabled:
        return []
    try:
        result = safe_get(
            "https://api.jikan.moe/v4/seasons/upcoming?limit=24",
            accept="application/json",
            force=True,
            max_bytes=1024 * 1024,
        )
    except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
        # Any lookup failure is a "no data" answer, never an error page.
        return []
    if result.status_code != 200:
        return []
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return []
    out = []
    for entry in (data.get("data") or [])[:24]:
        images = (entry.get("images") or {}).get("jpg") or {}
        out.append(
            {
                "id": str(entry.get("mal_id") or ""),
                "title": clean_text(entry.get("title"), 200) or "",
                "image_url": clean_url(images.get("large_image_url") or images.get("image_url")),
                "type": entry.get("type"),
                "season": entry.get("season"),
                "year": entry.get("year"),
                "genres": [g.get("name") for g in (entry.get("genres") or []) if g.get("name")][:4],
                "url": entry.get("url"),
            }
        )
    return out


def tmdb_headers() -> dict:
    """Auth headers for TMDB. Empty when only the query-string key is configured."""
    if settings.tmdb_read_token:
        return {"Authorization": f"Bearer {settings.tmdb_read_token}"}
    return {}


def tmdb_search_movie(query: str) -> list[dict]:
    """Search TMDB for movies. Requires TMDB_API_KEY (optional integration)."""
    if not settings.has_tmdb or not query.strip():
        return []
    from urllib.parse import urlencode

    qs = urlencode({"api_key": settings.tmdb_api_key, "query": query, "include_adult": "false", "page": 1})
    try:
        result = safe_get(
            f"https://api.themoviedb.org/3/search/movie?{qs}",
            accept="application/json",
            force=True,
            max_bytes=512 * 1024,
            extra_headers=tmdb_headers(),
        )
    except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
        # Any lookup failure is a "no data" answer, never an error page.
        return []
    if result.status_code != 200:
        return []
    return _tmdb_parse_search(result.text)


def tmdb_upcoming(country: Optional[str] = None) -> list[dict]:
    """Upcoming / now-playing movies, optionally filtered by release country."""
    if not settings.has_tmdb:
        return []
    from urllib.parse import urlencode

    qs = urlencode({"api_key": settings.tmdb_api_key, "include_adult": "false", "page": 1})
    urls = [
        f"https://api.themoviedb.org/3/movie/upcoming?{qs}",
        f"https://api.themoviedb.org/3/movie/now_playing?{qs}",
    ]
    items: list[dict] = []
    for endpoint in urls:
        try:
            result = safe_get(
                endpoint,
                accept="application/json",
                force=True,
                max_bytes=768 * 1024,
                extra_headers=tmdb_headers(),
            )
        except (UnsafeUrlError, FetchError, ThrottledError, ValueError, OSError):
            continue
        items.extend(_tmdb_parse_search(result.text, upcoming=True))
    if country and country != "other":
        iso = country.upper()
        items = [m for m in items if not m.get("countries") or iso in m["countries"]]
    return items[:40]


def _tmdb_parse_search(text: str, upcoming: bool = False) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for entry in (data.get("results") or [])[:20]:
        poster = entry.get("poster_path")
        out.append(
            {
                "id": str(entry.get("id") or ""),
                "title": clean_text(entry.get("title"), 200) or "",
                "image_url": clean_url(f"https://image.tmdb.org/t/p/w342{poster}") if poster else None,
                "description": clean_text(entry.get("overview"), 600),
                "release_date": entry.get("release_date"),
                "rating": round(float(entry.get("vote_average") or 0), 1),
                "url": f"https://www.themoviedb.org/movie/{entry.get('id')}",
                "upcoming": upcoming,
            }
        )
    return out
