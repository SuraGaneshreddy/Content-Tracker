"""
"Find Alternative" suggestions.

When a saved link breaks we offer the user a shortlist of replacement
candidates.  The list is deliberately built from **reputable, public
catalogues** — official databases and metadata sites — never from scraped
aggregators or mirror lists.

Nothing is applied automatically: the UI shows the candidates and the user
must explicitly pick one, and can edit the URL before saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import CATEGORY_ANIME, CATEGORY_CODING, CATEGORY_MOVIE, CATEGORY_NEWS, CATEGORY_SPORTS
from ..models import Content
from .metadata import jikan_search_anime, tmdb_search_movie


@dataclass
class Suggestion:
    title: str
    url: str
    source: str
    reason: str = ""
    image_url: Optional[str] = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "reason": self.reason,
            "image_url": self.image_url,
            "note": self.note,
        }


# Curated, legitimate destinations per category.
FALLBACK_SOURCES = {
    CATEGORY_ANIME: [
        ("MyAnimeList", "https://myanimelist.net/anime.php?q={query}", "Official anime database"),
        ("AniList", "https://anilist.co/search/anime/{query}", "Official anime database"),
        ("Crunchyroll", "https://www.crunchyroll.com/search?q={query}", "Licensed streaming service"),
    ],
    CATEGORY_MOVIE: [
        ("TMDB", "https://www.themoviedb.org/search?query={query}", "Open movie database"),
        ("IMDb", "https://www.imdb.com/find/?q={query}", "Official movie database"),
        ("Letterboxd", "https://letterboxd.com/search/{query}/", "Film catalogue"),
    ],
    CATEGORY_SPORTS: [
        ("ESPN", "https://www.espn.com/search/_/q/{query}", "Sports schedules and results"),
        ("BBC Sport", "https://www.bbc.com/sport/{query}", "Sports news and fixtures"),
    ],
    CATEGORY_NEWS: [
        ("Google News", "https://news.google.com/search?q={query}", "News search"),
        ("Reuters", "https://www.reuters.com/site-search/?query={query}", "Wire service"),
    ],
    CATEGORY_CODING: [
        ("GitHub", "https://github.com/search?q={query}", "Source code and docs"),
        ("MDN", "https://developer.mozilla.org/en-US/search?q={query}", "Web documentation"),
        ("Stack Overflow", "https://stackoverflow.com/search?q={query}", "Developer Q&A"),
    ],
}

GENERIC_SOURCES = [
    ("Wikipedia", "https://en.wikipedia.org/w/index.php?search={query}", "Encyclopaedia entry"),
    ("DuckDuckGo", "https://duckduckgo.com/?q={query}", "Web search"),
]


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value.strip())


def find_alternatives(db: Session, item: Content, limit: int = 8) -> List[Suggestion]:
    """Build a reviewed shortlist of legitimate alternative sources."""
    query = _quote(item.title)
    slug = item.category.slug if item.category else "other"
    suggestions: List[Suggestion] = []

    if slug == CATEGORY_ANIME and settings.jikan_enabled:
        for entry in jikan_search_anime(item.title)[:3]:
            if entry.get("url"):
                suggestions.append(
                    Suggestion(
                        title=entry.get("title") or item.title,
                        url=entry["url"],
                        source="MyAnimeList",
                        reason="Matched in the official anime database",
                        image_url=entry.get("image_url"),
                        note=f"{entry.get('episodes') or '?'} episodes"
                        + (f" · score {entry['score']}" if entry.get("score") else ""),
                    )
                )

    if slug == CATEGORY_MOVIE and settings.has_tmdb:
        for entry in tmdb_search_movie(item.title)[:3]:
            if entry.get("url"):
                suggestions.append(
                    Suggestion(
                        title=entry.get("title") or item.title,
                        url=entry["url"],
                        source="TMDB",
                        reason="Matched in the movie database",
                        image_url=entry.get("image_url"),
                        note=entry.get("release_date") or "",
                    )
                )

    for name, template, reason in FALLBACK_SOURCES.get(slug, []):
        suggestions.append(
            Suggestion(
                title=f"{item.title} on {name}",
                url=template.format(query=query),
                source=name,
                reason=reason,
            )
        )

    for name, template, reason in GENERIC_SOURCES:
        suggestions.append(
            Suggestion(
                title=f"{item.title} on {name}",
                url=template.format(query=query),
                source=name,
                reason=reason,
            )
        )

    # De-duplicate by URL while preserving order.
    seen: set[str] = set()
    unique: List[Suggestion] = []
    for suggestion in suggestions:
        if suggestion.url in seen:
            continue
        seen.add(suggestion.url)
        unique.append(suggestion)
        if len(unique) >= limit:
            break
    return unique


DISCLAIMER = (
    "Suggestions come from public, reputable catalogues and search engines. "
    "Review the destination before saving it — nothing is changed automatically, "
    "and this app will never point you at unauthorised copies of content."
)
