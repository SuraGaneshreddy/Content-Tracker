"""
Canonical vocabularies shared by validation, the UI and the update engine.

Categories live in the DB (so users/admins can add more), but their slugs are
fixed here because service code branches on them.
"""

from __future__ import annotations

from typing import Dict, List

# --- Categories -------------------------------------------------------------
CATEGORY_MANGA = "manga"
CATEGORY_MANHWA = "manhwa"
CATEGORY_MANHUA = "manhua"
CATEGORY_ANIME = "anime"
CATEGORY_MOVIE = "movie"
CATEGORY_SPORTS = "sports"
CATEGORY_NEWS = "news"
CATEGORY_CODING = "coding"
CATEGORY_OTHER = "other"

# slug -> (name, icon, accent colour, progress fields, status options, update source)
CATEGORIES: Dict[str, dict] = {
    CATEGORY_MANGA: dict(
        name="Manga", icon="📖", accent="#ef4444", sort_order=10,
        progress_fields="chapter,volume", update_source="page",
        statuses="reading,plan_to_read,on_hold,dropped,completed,favorite",
    ),
    CATEGORY_MANHWA: dict(
        name="Manhwa", icon="📘", accent="#3b82f6", sort_order=20,
        progress_fields="chapter,volume", update_source="page",
        statuses="reading,plan_to_read,on_hold,dropped,completed,favorite",
    ),
    CATEGORY_MANHUA: dict(
        name="Manhua", icon="📗", accent="#10b981", sort_order=30,
        progress_fields="chapter,volume", update_source="page",
        statuses="reading,plan_to_read,on_hold,dropped,completed,favorite",
    ),
    CATEGORY_ANIME: dict(
        name="Anime", icon="🎬", accent="#a855f7", sort_order=40,
        progress_fields="season,episode", update_source="jikan",
        statuses="watching,plan_to_watch,on_hold,dropped,completed,favorite",
    ),
    CATEGORY_MOVIE: dict(
        name="Movie / Cinema", icon="🎥", accent="#f59e0b", sort_order=50,
        # Field names must match the form input ids (field-<name>) and app.js.
        progress_fields="progress_text,progress_percent", update_source="tmdb",
        statuses="watching,plan_to_watch,completed,dropped,favorite",
    ),
    CATEGORY_SPORTS: dict(
        name="Sports", icon="⚽", accent="#22c55e", sort_order=60,
        progress_fields="progress_text", update_source="espn",
        statuses="following,on_hold,completed,favorite",
    ),
    CATEGORY_NEWS: dict(
        name="News", icon="📰", accent="#0ea5e9", sort_order=70,
        progress_fields="progress_text", update_source="rss",
        statuses="following,on_hold,completed,favorite",
    ),
    CATEGORY_CODING: dict(
        name="Coding", icon="💻", accent="#6366f1", sort_order=80,
        progress_fields="progress_percent,progress_text", update_source="rss",
        statuses="following,on_hold,completed,dropped,favorite",
    ),
    CATEGORY_OTHER: dict(
        name="Other", icon="📌", accent="#64748b", sort_order=90,
        progress_fields="progress_text,progress_percent", update_source="page",
        statuses="following,reading,watching,plan_to_read,plan_to_watch,on_hold,dropped,completed,favorite",
    ),
}

CATEGORY_ORDER: List[str] = list(CATEGORIES.keys())

# Categories where "page diff" style update detection is used.
SERIES_CATEGORIES = {CATEGORY_MANGA, CATEGORY_MANHWA, CATEGORY_MANHUA}

# --- Statuses ---------------------------------------------------------------
# slug -> (label, group, colour, sort order)
STATUSES: Dict[str, tuple] = {
    "reading": ("Currently Reading", "active", "#22c55e", 10),
    "watching": ("Currently Watching", "active", "#10b981", 20),
    "following": ("Following", "active", "#0ea5e9", 30),
    "completed": ("Completed", "done", "#8b5cf6", 40),
    "plan_to_read": ("Plan to Read", "planned", "#f59e0b", 50),
    "plan_to_watch": ("Plan to Watch", "planned", "#f97316", 60),
    "on_hold": ("On Hold", "paused", "#64748b", 70),
    "dropped": ("Dropped", "paused", "#ef4444", 80),
    "favorite": ("Favorite", "special", "#eab308", 90),
}

STATUS_LABELS: Dict[str, str] = {k: v[0] for k, v in STATUSES.items()}
STATUS_COLORS: Dict[str, str] = {k: v[2] for k, v in STATUSES.items()}
STATUS_GROUPS: Dict[str, str] = {k: v[1] for k, v in STATUSES.items()}

# Filter tabs on the library page -> status slugs
LIBRARY_FILTERS: Dict[str, List[str]] = {
    "all": [],
    "reading": ["reading"],
    "watching": ["watching"],
    "completed": ["completed"],
    "planning": ["plan_to_read", "plan_to_watch"],
    "on_hold": ["on_hold"],
    "dropped": ["dropped"],
    "following": ["following"],
}

# --- URL health -------------------------------------------------------------
URL_STATUS_OK = "ok"
URL_STATUS_DEGRADED = "degraded"
URL_STATUS_BROKEN = "broken"
URL_STATUS_UNKNOWN = "unknown"

URL_STATUS_LABELS = {
    URL_STATUS_OK: "Working",
    URL_STATUS_DEGRADED: "Temporarily unavailable",
    URL_STATUS_BROKEN: "Broken",
    URL_STATUS_UNKNOWN: "Not checked",
}
URL_STATUS_ICONS = {
    URL_STATUS_OK: "🟢",
    URL_STATUS_DEGRADED: "🟡",
    URL_STATUS_BROKEN: "🔴",
    URL_STATUS_UNKNOWN: "⚪",
}

# --- Misc vocabularies ------------------------------------------------------
UPDATE_STATES = {"unread", "read", "ignored"}
SORT_OPTIONS = {
    "recent_added": "Recently added",
    "recent_updated": "Recently updated",
    "recent_opened": "Recently opened",
    "az": "Title A–Z",
    "za": "Title Z–A",
    "rating": "Highest rated",
}

MOVIE_COUNTRIES = [
    ("in", "India"),
    ("jp", "Japan"),
    ("kr", "South Korea"),
    ("cn", "China"),
    ("us", "USA"),
    ("gb", "UK"),
    ("other", "Other"),
]

SPORT_TYPES = [
    ("cricket", "Cricket"),
    ("football", "Football"),
    ("basketball", "Basketball"),
    ("tennis", "Tennis"),
    ("f1", "Formula 1"),
    ("other", "Other"),
]

NEWS_TOPICS = [
    ("world", "World"),
    ("technology", "Technology"),
    ("business", "Business"),
    ("entertainment", "Entertainment"),
    ("sports", "Sports"),
    ("science", "Science"),
    ("other", "Other"),
]

RATING_MIN, RATING_MAX = 1, 10
MAX_TAGS_PER_ITEM = 12
MAX_TITLE_LEN = 300
MAX_NOTES_LEN = 5000
