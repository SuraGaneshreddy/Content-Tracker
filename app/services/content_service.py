"""
Core library operations.

Every function here takes an explicit ``user_id`` and filters on it, so a
user's library is never readable or writable across accounts.  All database
access uses SQLAlchemy Core/ORM expressions (fully parameterised), so user
input never reaches SQL as text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import String, and_, asc, case, desc, func, or_, select
from sqlalchemy.orm import Session

from ..constants import (
    LIBRARY_FILTERS,
    MAX_NOTES_LEN,
    MAX_TAGS_PER_ITEM,
    MAX_TITLE_LEN,
    RATING_MAX,
    RATING_MIN,
    STATUSES,
    STATUS_LABELS,
    URL_STATUS_BROKEN,
    URL_STATUS_UNKNOWN,
)
from ..models import (
    ActivityLog,
    Category,
    Content,
    ContentTag,
    Favorite,
    Tag,
    UpdateHistory,
    UrlHistory,
    User,
    utcnow,
)
from ..utils.url_validation import UrlValidationError, display_host, validate_url_syntax
from . import thumbnails
from .activity import log_activity


class ContentError(ValueError):
    """Validation failure with a user-safe message."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_title(value: Optional[str]) -> str:
    title = (value or "").strip()
    title = " ".join(title.split())
    if not title:
        raise ContentError("Title is required.")
    if len(title) > MAX_TITLE_LEN:
        raise ContentError(f"Title must be {MAX_TITLE_LEN} characters or fewer.")
    # Strip angle brackets so a pasted title can never be interpreted as markup.
    title = title.replace("<", "‹").replace(">", "›")
    return title


def clean_status(value: Optional[str], allowed: Sequence[str] | None = None) -> str:
    status = (value or "").strip().lower()
    if status not in STATUSES:
        raise ContentError("Please choose a valid status.")
    if allowed and status not in allowed:
        raise ContentError(f"'{STATUS_LABELS[status]}' is not available for this category.")
    return status


def clean_rating(value: Any) -> Optional[int]:
    if value in (None, "", "null"):
        return None
    try:
        rating = int(float(value))
    except (TypeError, ValueError):
        raise ContentError("Rating must be a number between 1 and 10.") from None
    if not RATING_MIN <= rating <= RATING_MAX:
        raise ContentError(f"Rating must be between {RATING_MIN} and {RATING_MAX}.")
    return rating


def _clean_int(value: Any, field_name: str, maximum: int = 100_000) -> Optional[int]:
    if value in (None, "", "null"):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ContentError(f"{field_name} must be a whole number.") from None
    if number < 0:
        raise ContentError(f"{field_name} cannot be negative.")
    if number > maximum:
        raise ContentError(f"{field_name} is unrealistically large.")
    return number


def _clean_float(value: Any, field_name: str) -> Optional[float]:
    if value in (None, "", "null"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ContentError(f"{field_name} must be a number.") from None
    if number < 0 or number > 100000:
        raise ContentError(f"{field_name} must be between 0 and 100000.")
    return number


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    if not text:
        return None
    if len(text) > limit:
        raise ContentError(f"That text is too long (maximum {limit} characters).")
    return text


def normalize_tags(raw: Any) -> List[str]:
    """Accept a list or a comma/space separated string; return clean tag names."""
    items: List[str] = []
    if raw is None:
        return items
    if isinstance(raw, str):
        parts = [p for chunk in raw.replace("\n", ",").split(",") for p in chunk.split()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p) for p in raw]
    else:
        raise ContentError("Tags must be a list or comma separated text.")

    seen = set()
    for part in parts:
        name = " ".join(str(part).split())[:60].strip().strip("#")
        if not name or len(name) < 1:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(name)
        if len(items) >= MAX_TAGS_PER_ITEM:
            break
    return items


def get_category(db: Session, slug: str) -> Category:
    category = db.scalar(select(Category).where(Category.slug == slug, Category.is_active.is_(True)))
    if category is None:
        raise ContentError("Please choose a category.")
    return category


def get_owned_content(db: Session, user_id: int, content_id: int) -> Content:
    """Fetch a content row *and* verify ownership. Raises if missing."""
    item = db.get(Content, content_id)
    if item is None or item.user_id != user_id or item.purged_at is not None:
        raise ContentError("That item does not exist.")
    return item


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
def sync_tags(db: Session, user_id: int, content: Content, tag_names: Iterable[str]) -> List[Tag]:
    wanted = normalize_tags(tag_names)
    existing = {t.name.lower(): t for t in db.scalars(select(Tag).where(Tag.user_id == user_id)).all()}
    tags: List[Tag] = []
    for name in wanted:
        tag = existing.get(name.lower())
        if tag is None:
            tag = Tag(user_id=user_id, name=name)
            db.add(tag)
            db.flush()
            existing[name.lower()] = tag
        tags.append(tag)
    content.tags = tags
    return tags


def all_tags(db: Session, user_id: int) -> List[Tuple[Tag, int]]:
    """All tags with usage counts (unused tags are included so they can be pruned)."""
    rows = db.execute(
        select(Tag, func.count(ContentTag.content_id))
        .join(ContentTag, ContentTag.tag_id == Tag.id, isouter=True)
        .join(Content, and_(Content.id == ContentTag.content_id, Content.deleted_at.is_(None)), isouter=True)
        .where(Tag.user_id == user_id)
        .group_by(Tag.id)
        .order_by(asc(func.lower(Tag.name)))
    ).all()
    return [(tag, int(count or 0)) for tag, count in rows]


def delete_unused_tags(db: Session, user_id: int) -> int:
    used = set(
        db.scalars(
            select(ContentTag.tag_id)
            .join(Tag, Tag.id == ContentTag.tag_id)
            .where(Tag.user_id == user_id)
        ).all()
    )
    tags = list(db.scalars(select(Tag).where(Tag.user_id == user_id)).all())
    removed = 0
    for tag in tags:
        if tag.id not in used:
            db.delete(tag)
            removed += 1
    if removed:
        db.commit()
    return removed


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------
@dataclass
class ContentPayload:
    title: str
    url: str
    category_slug: str
    status: str
    description: Optional[str] = None
    notes: Optional[str] = None
    rating: Optional[int] = None
    tags: List[str] = None  # type: ignore[assignment]
    chapter: Optional[int] = None
    episode: Optional[int] = None
    season: Optional[int] = None
    volume: Optional[int] = None
    progress_text: Optional[str] = None
    progress_percent: Optional[float] = None
    progress_extra: Optional[dict] = None
    cover_image_url: Optional[str] = None
    external_id: Optional[str] = None
    update_source: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Dict[str, Any]) -> "ContentPayload":
        """Build a payload from form/JSON data, validating every field."""
        category = data.get("category") or ""
        progress_extra = data.get("progress_extra")
        if isinstance(progress_extra, str):
            progress_extra = None
        return cls(
            title=clean_title(data.get("title")),
            url=validate_url_syntax(data.get("url")),
            category_slug=str(category).strip().lower(),
            status=clean_status(data.get("status")),
            description=_clean_text(data.get("description"), 2000),
            notes=_clean_text(data.get("notes"), MAX_NOTES_LEN),
            rating=clean_rating(data.get("rating")),
            tags=normalize_tags(data.get("tags")),
            chapter=_clean_int(data.get("chapter"), "Chapter"),
            episode=_clean_int(data.get("episode"), "Episode"),
            season=_clean_int(data.get("season"), "Season", 200),
            volume=_clean_int(data.get("volume"), "Volume"),
            progress_text=_clean_text(data.get("progress_text"), 200),
            progress_percent=_clean_float(data.get("progress_percent"), "Progress"),
            progress_extra=progress_extra if isinstance(progress_extra, dict) else None,
            cover_image_url=(data.get("cover_image_url") or None),
            external_id=_clean_text(data.get("external_id"), 80),
            update_source=_clean_text(data.get("update_source"), 60),
        )


def create_content(
    db: Session,
    user_id: int,
    payload: ContentPayload,
    *,
    cache_cover: bool = True,
) -> Content:
    """Insert a new tracked item, its first URL history row, and log activity."""
    category = get_category(db, payload.category_slug)
    allowed = category.status_option_list() or list(STATUSES)
    status = clean_status(payload.status, allowed)

    item = Content(
        user_id=user_id,
        title=payload.title,
        description=payload.description,
        url=payload.url,
        category_id=category.id,
        status=status,
        current_chapter=payload.chapter,
        current_episode=payload.episode,
        current_season=payload.season,
        current_volume=payload.volume,
        progress_text=payload.progress_text,
        progress_percent=payload.progress_percent,
        progress_json=payload.progress_extra or None,
        rating=payload.rating,
        notes=payload.notes,
        cover_image_url=payload.cover_image_url or None,
        external_id=payload.external_id,
        update_source=payload.update_source or category.update_source,
        is_favorite=(status == "favorite"),
    )
    db.add(item)
    db.flush()

    sync_tags(db, user_id, item, payload.tags)

    db.add(
        UrlHistory(
            content_id=item.id,
            user_id=user_id,
            url=item.url,
            is_current=True,
            status=URL_STATUS_UNKNOWN,
            source="user",
        )
    )
    if status == "favorite":
        db.add(Favorite(user_id=user_id, content_id=item.id))

    if cache_cover and item.cover_image_url:
        cached = thumbnails.download_and_cache(item.cover_image_url)
        if cached:
            item.cover_cached_path = cached

    log_activity(db, user_id, "added", f"You added {item.title}.", content_id=item.id)
    db.commit()
    db.refresh(item)
    return item


def update_content(
    db: Session,
    user_id: int,
    content_id: int,
    payload: ContentPayload,
    *,
    cache_cover: bool = True,
) -> Content:
    """Edit an item. A changed URL is preserved in the URL history."""
    item = get_owned_content(db, user_id, content_id)
    category = get_category(db, payload.category_slug)
    allowed = category.status_option_list() or list(STATUSES)
    status = clean_status(payload.status, allowed)

    old_status = item.status
    url_changed = payload.url != item.url

    item.title = payload.title
    item.description = payload.description
    item.category_id = category.id
    item.status = status
    item.current_chapter = payload.chapter
    item.current_episode = payload.episode
    item.current_season = payload.season
    item.current_volume = payload.volume
    item.progress_text = payload.progress_text
    item.progress_percent = payload.progress_percent
    item.progress_json = payload.progress_extra or None
    item.rating = payload.rating
    item.notes = payload.notes
    item.external_id = payload.external_id
    item.update_source = payload.update_source or category.update_source
    sync_tags(db, user_id, item, payload.tags)

    # Cover image: refresh the cache only when the source changed.
    new_cover = payload.cover_image_url or None
    if new_cover != (item.cover_image_url or None):
        thumbnails.remove_thumbnail(item.cover_cached_path)
        item.cover_image_url = new_cover
        item.cover_cached_path = thumbnails.download_and_cache(new_cover) if (cache_cover and new_cover) else None

    if url_changed:
        apply_url_change(db, item, payload.url, source="user")

    item.is_favorite = bool(
        db.scalar(select(Favorite.id).where(Favorite.user_id == user_id, Favorite.content_id == item.id))
    ) or status == "favorite"

    if old_status != status:
        log_activity(
            db,
            user_id,
            "status",
            f"You marked {item.title} as {STATUS_LABELS.get(status, status)}.",
            content_id=item.id,
        )
    else:
        log_activity(db, user_id, "updated", f"You edited {item.title}.", content_id=item.id)

    db.commit()
    db.refresh(item)
    return item


def apply_url_change(db: Session, item: Content, new_url: str, *, source: str = "user") -> None:
    """Move the current URL into history and set the new one as current."""
    try:
        new_url = validate_url_syntax(new_url)
    except UrlValidationError:
        raise ContentError("Invalid URL.") from None

    now = utcnow()
    previous = db.scalar(
        select(UrlHistory).where(UrlHistory.content_id == item.id, UrlHistory.is_current.is_(True))
    )
    if previous is not None:
        previous.is_current = False
        previous.replaced_at = now

    item.url = new_url
    item.url_status = URL_STATUS_UNKNOWN
    item.url_status_code = None
    item.url_last_checked_at = None
    item.url_last_error = None
    db.add(
        UrlHistory(
            content_id=item.id,
            user_id=item.user_id,
            url=new_url,
            is_current=True,
            status=URL_STATUS_UNKNOWN,
            source=source,
        )
    )
    log_activity(
        db,
        item.user_id,
        "url_changed",
        f"URL changed for {item.title} ({display_host(new_url)}).",
        content_id=item.id,
        meta={"url": new_url},
    )


def soft_delete(db: Session, user_id: int, content_id: int) -> Content:
    item = get_owned_content(db, user_id, content_id)
    if item.deleted_at is None:
        item.deleted_at = utcnow()
        log_activity(db, user_id, "deleted", f"You moved {item.title} to Trash.", content_id=item.id)
        db.commit()
    return item


def restore(db: Session, user_id: int, content_id: int) -> Content:
    item = get_owned_content(db, user_id, content_id)
    item.deleted_at = None
    log_activity(db, user_id, "restored", f"You restored {item.title}.", content_id=item.id)
    db.commit()
    return item


def purge(db: Session, user_id: int, content_id: int) -> None:
    """Hard delete: removes the row and its history."""
    item = get_owned_content(db, user_id, content_id)
    title = item.title
    thumbnails.remove_thumbnail(item.cover_cached_path)
    db.delete(item)
    log_activity(db, user_id, "purged", f"You permanently deleted {title}.")
    db.commit()


def empty_trash(db: Session, user_id: int) -> int:
    items = list(
        db.scalars(
            select(Content).where(
                Content.user_id == user_id,
                Content.deleted_at.is_not(None),
                Content.purged_at.is_(None),
            )
        ).all()
    )
    for item in items:
        thumbnails.remove_thumbnail(item.cover_cached_path)
        db.delete(item)
    if items:
        log_activity(db, user_id, "purged", f"You emptied the Trash ({len(items)} items).")
        db.commit()
    return len(items)


# ---------------------------------------------------------------------------
# Quick actions
# ---------------------------------------------------------------------------
def mark_opened(db: Session, user_id: int, content_id: int) -> Content:
    item = get_owned_content(db, user_id, content_id)
    item.open_count = (item.open_count or 0) + 1
    item.last_opened_at = utcnow()
    log_activity(db, user_id, "opened", f"You opened {item.title}.", content_id=item.id)
    db.commit()
    return item


def set_favorite(db: Session, user_id: int, content_id: int, favorite: bool) -> Content:
    item = get_owned_content(db, user_id, content_id)
    existing = db.scalar(select(Favorite).where(Favorite.user_id == user_id, Favorite.content_id == item.id))
    if favorite and existing is None:
        db.add(Favorite(user_id=user_id, content_id=item.id))
        log_activity(db, user_id, "favorited", f"You starred {item.title}.", content_id=item.id)
    elif not favorite and existing is not None:
        db.delete(existing)
        log_activity(db, user_id, "unfavorited", f"You unstarred {item.title}.", content_id=item.id)
    item.is_favorite = favorite
    db.commit()
    return item


def update_progress(
    db: Session,
    user_id: int,
    content_id: int,
    *,
    chapter: Any = None,
    episode: Any = None,
    season: Any = None,
    volume: Any = None,
    progress_text: Any = None,
    progress_percent: Any = None,
    status: Any = None,
    mark_update_read: bool = True,
) -> Content:
    """Update progress fields. Returns the refreshed row."""
    item = get_owned_content(db, user_id, content_id)
    changes: List[str] = []

    if chapter is not None:
        value = _clean_int(chapter, "Chapter")
        if value != item.current_chapter:
            changes.append(f"Chapter {value}")
        item.current_chapter = value
    if episode is not None:
        value = _clean_int(episode, "Episode")
        if value != item.current_episode:
            changes.append(f"Episode {value}")
        item.current_episode = value
    if season is not None:
        value = _clean_int(season, "Season", 200)
        if value != item.current_season:
            changes.append(f"Season {value}")
        item.current_season = value
    if volume is not None:
        value = _clean_int(volume, "Volume")
        if value != item.current_volume:
            changes.append(f"Volume {value}")
        item.current_volume = value
    if progress_text is not None:
        value = _clean_text(progress_text, 200)
        item.progress_text = value
        if value:
            changes.append(value)
    if progress_percent is not None:
        value = _clean_float(progress_percent, "Progress")
        item.progress_percent = value
        if value is not None:
            changes.append(f"{value:g}%")
    if status:
        allowed = (item.category.status_option_list() if item.category else None) or list(STATUSES)
        new_status = clean_status(status, allowed)
        if new_status != item.status:
            changes.append(STATUS_LABELS.get(new_status, new_status))
            item.status = new_status

    if mark_update_read and item.update_available:
        item.update_state = "read"
        item.update_available = False
        db.execute(
            UpdateHistory.__table__.update()
            .where(
                UpdateHistory.content_id == item.id,
                UpdateHistory.user_id == user_id,
                UpdateHistory.state == "unread",
            )
            .values(state="read", acted_at=utcnow())
        )

    if changes:
        log_activity(
            db,
            user_id,
            "progress",
            f"You updated {item.title} to {', '.join(changes[:3])}.",
            content_id=item.id,
            meta={"changes": changes},
        )
        db.commit()
        db.refresh(item)
    return item


def progress_label(item: Content) -> str:
    """Human readable progress, aware of the item's category."""
    slug = item.category.slug if item.category else "other"
    parts: List[str] = []
    if slug in ("manga", "manhwa", "manhua"):
        if item.current_chapter is not None:
            parts.append(f"Chapter {item.current_chapter:g}")
        if item.current_volume is not None:
            parts.append(f"Vol. {item.current_volume:g}")
    elif slug == "anime":
        if item.current_season is not None and item.current_episode is not None:
            parts.append(f"S{item.current_season:g} E{item.current_episode:g}")
        elif item.current_episode is not None:
            parts.append(f"Episode {item.current_episode:g}")
    elif slug == "movie":
        if item.progress_text:
            parts.append(item.progress_text)
        elif item.progress_percent is not None:
            parts.append(f"{item.progress_percent:g}% watched")
    elif slug == "coding":
        if item.progress_percent is not None:
            parts.append(f"{item.progress_percent:g}% complete")
        elif item.progress_text:
            parts.append(item.progress_text)
    else:
        if item.progress_text:
            parts.append(item.progress_text)
        if item.current_chapter is not None:
            parts.append(f"Chapter {item.current_chapter:g}")
        if item.current_episode is not None:
            parts.append(f"Episode {item.current_episode:g}")

    if not parts and item.latest_available:
        parts.append(item.latest_available)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------
@dataclass
class LibraryQuery:
    category: Optional[str] = None
    status_filter: str = "all"
    tag: Optional[str] = None
    rating: Optional[int] = None
    favorites_only: bool = False
    search: Optional[str] = None
    sort: str = "recent_added"
    page: int = 1
    per_page: int = 24
    include_deleted: bool = False


def build_library_query(db: Session, user_id: int, query: LibraryQuery):
    """Return (select_statement, base_filters) for a filtered/sorted library."""
    stmt = select(Content).where(Content.user_id == user_id, Content.purged_at.is_(None))
    if query.include_deleted:
        stmt = stmt.where(Content.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Content.deleted_at.is_(None))

    if query.category:
        category = db.scalar(select(Category).where(Category.slug == query.category))
        if category:
            stmt = stmt.where(Content.category_id == category.id)

    statuses = LIBRARY_FILTERS.get(query.status_filter, [])
    if statuses:
        stmt = stmt.where(Content.status.in_(statuses))

    if query.favorites_only:
        stmt = stmt.where(Content.is_favorite.is_(True))

    if query.rating:
        stmt = stmt.where(Content.rating >= int(query.rating))

    if query.tag:
        stmt = stmt.where(
            Content.id.in_(
                select(ContentTag.content_id)
                .join(Tag, Tag.id == ContentTag.tag_id)
                .where(Tag.user_id == user_id, func.lower(Tag.name) == query.tag.strip().lower())
            )
        )

    if query.search:
        pattern = f"%{query.search.strip()}%"
        tag_ids = select(Tag.id).where(Tag.user_id == user_id, Tag.name.ilike(pattern))
        stmt = stmt.where(
            or_(
                Content.title.ilike(pattern),
                Content.notes.ilike(pattern),
                Content.description.ilike(pattern),
                Content.progress_text.ilike(pattern),
                Content.url.ilike(pattern),
                Content.status == query.search.strip().lower(),
                Content.id.in_(select(ContentTag.content_id).where(ContentTag.tag_id.in_(tag_ids))),
            )
        )

    sort_map = {
        "recent_added": desc(Content.created_at),
        "recent_updated": desc(Content.updated_at),
        "recent_opened": desc(Content.last_opened_at),
        "az": asc(func.lower(Content.title)),
        "za": desc(func.lower(Content.title)),
        "rating": desc(Content.rating),
    }
    order = sort_map.get(query.sort, desc(Content.created_at))
    stmt = stmt.order_by(order, desc(Content.id))
    return stmt


def list_content(
    db: Session, user_id: int, query: LibraryQuery
) -> Tuple[List[Content], int, int]:
    """Return (items, total_count, page_count)."""
    per_page = max(1, min(int(query.per_page or 24), 120))
    page = max(1, int(query.page or 1))

    stmt = build_library_query(db, user_id, query)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all())
    pages = max(1, -(-int(total) // per_page))
    return items, int(total), pages


def get_stats(db: Session, user_id: int) -> Dict[str, Any]:
    """Dashboard statistics: totals, status groups and category breakdown."""
    base = and_(Content.user_id == user_id, Content.deleted_at.is_(None), Content.purged_at.is_(None))

    total = int(db.scalar(select(func.count(Content.id)).where(base)) or 0)

    status_rows = db.execute(
        select(Content.status, func.count(Content.id)).where(base).group_by(Content.status)
    ).all()
    by_status = {slug: 0 for slug in STATUSES}
    for slug, count in status_rows:
        by_status[slug] = int(count)

    category_rows = db.execute(
        select(Category, func.count(Content.id))
        .join(Content, Content.category_id == Category.id)
        .where(base)
        .group_by(Category.id)
        .order_by(asc(Category.sort_order))
    ).all()
    by_category = [
        {
            "slug": category.slug,
            "name": category.name,
            "icon": category.icon,
            "accent": category.accent,
            "count": int(count),
        }
        for category, count in category_rows
    ]

    favorites = int(
        db.scalar(
            select(func.count(Content.id)).where(and_(base, Content.is_favorite.is_(True)))
        )
        or 0
    )
    broken = int(
        db.scalar(select(func.count(Content.id)).where(and_(base, Content.url_status == URL_STATUS_BROKEN)))
        or 0
    )
    pending_updates = int(
        db.scalar(
            select(func.count(Content.id)).where(
                and_(base, Content.update_available.is_(True), Content.update_state == "unread")
            )
        )
        or 0
    )
    trash_count = int(
        db.scalar(
            select(func.count(Content.id)).where(
                Content.user_id == user_id, Content.deleted_at.is_not(None), Content.purged_at.is_(None)
            )
        )
        or 0
    )
    rated_avg = db.scalar(
        select(func.avg(Content.rating)).where(and_(base, Content.rating.is_not(None)))
    )

    return {
        "total": total,
        "reading": by_status.get("reading", 0),
        "watching": by_status.get("watching", 0),
        "following": by_status.get("following", 0),
        "completed": by_status.get("completed", 0),
        "planning": by_status.get("plan_to_read", 0) + by_status.get("plan_to_watch", 0),
        "on_hold": by_status.get("on_hold", 0),
        "dropped": by_status.get("dropped", 0),
        "favorites": favorites,
        "broken_urls": broken,
        "pending_updates": pending_updates,
        "trash": trash_count,
        "average_rating": round(float(rated_avg), 1) if rated_avg else None,
        "by_status": by_status,
        "by_category": by_category,
    }


def search_everything(
    db: Session, user_id: int, term: str, limit: int = 20
) -> List[Dict[str, Any]]:
    """Global search across title, notes, tags, category and URL."""
    term = (term or "").strip()
    if not term:
        return []
    pattern = f"%{term}%"
    # Tags live in their own table, so match them through the join table.
    tag_ids = select(Tag.id).where(Tag.user_id == user_id, Tag.name.ilike(pattern))
    items = list(
        db.scalars(
            select(Content)
            .where(
                Content.user_id == user_id,
                Content.deleted_at.is_(None),
                Content.purged_at.is_(None),
                or_(
                    Content.title.ilike(pattern),
                    Content.notes.ilike(pattern),
                    Content.description.ilike(pattern),
                    Content.progress_text.ilike(pattern),
                    Content.url.ilike(pattern),
                    Content.status == term.lower(),
                    Content.id.in_(select(ContentTag.content_id).where(ContentTag.tag_id.in_(tag_ids))),
                ),
            )
            .order_by(
                case((Content.title.ilike(pattern), 0), else_=1),
                desc(Content.updated_at),
            )
            .limit(limit)
        ).all()
    )
    return [
        {
            "id": item.id,
            "title": item.title,
            "category": item.category.name if item.category else "",
            "icon": item.category.icon if item.category else "📌",
            "status": STATUS_LABELS.get(item.status, item.status),
            "progress": progress_label(item),
            "cover": item.cover_src,
            "url": f"/content/{item.id}",
        }
        for item in items
    ]


def trash_items(db: Session, user_id: int) -> List[Content]:
    return list(
        db.scalars(
            select(Content)
            .where(Content.user_id == user_id, Content.deleted_at.is_not(None), Content.purged_at.is_(None))
            .order_by(desc(Content.deleted_at))
        ).all()
    )


def duplicate_check(db: Session, user_id: int, url: str, exclude_id: Optional[int] = None) -> Optional[Content]:
    """Find an existing live item with the same URL (used to warn on duplicates)."""
    stmt = select(Content).where(
        Content.user_id == user_id, Content.url == url, Content.deleted_at.is_(None), Content.purged_at.is_(None)
    )
    if exclude_id:
        stmt = stmt.where(Content.id != exclude_id)
    return db.scalar(stmt)


def recently_added(db: Session, user_id: int, limit: int = 8) -> List[Content]:
    return list(
        db.scalars(
            select(Content)
            .where(Content.user_id == user_id, Content.deleted_at.is_(None), Content.purged_at.is_(None))
            .order_by(desc(Content.created_at))
            .limit(limit)
        ).all()
    )


def items_with_pending_updates(db: Session, user_id: int, limit: int = 50) -> List[Content]:
    return list(
        db.scalars(
            select(Content)
            .where(
                Content.user_id == user_id,
                Content.deleted_at.is_(None),
                Content.update_available.is_(True),
            )
            .order_by(case((Content.update_state == "unread", 0), else_=1), desc(Content.updated_at))
            .limit(limit)
        ).all()
    )


def set_update_state(db: Session, user_id: int, content_id: int, state: str) -> Content:
    from ..constants import UPDATE_STATES

    if state not in UPDATE_STATES:
        raise ContentError("Unknown update state.")
    item = get_owned_content(db, user_id, content_id)
    item.update_state = state
    item.update_available = state == "unread"
    db.execute(
        UpdateHistory.__table__.update()
        .where(UpdateHistory.content_id == item.id, UpdateHistory.state != state)
        .values(state=state, acted_at=utcnow())
    )
    db.commit()
    return item


def update_history_for(db: Session, user_id: int, content_id: int, limit: int = 50) -> List[UpdateHistory]:
    return list(
        db.scalars(
            select(UpdateHistory)
            .where(UpdateHistory.content_id == content_id, UpdateHistory.user_id == user_id)
            .order_by(desc(UpdateHistory.detected_at))
            .limit(limit)
        ).all()
    )


def url_history_for(db: Session, user_id: int, content_id: int) -> List[UrlHistory]:
    return list(
        db.scalars(
            select(UrlHistory)
            .where(UrlHistory.content_id == content_id, UrlHistory.user_id == user_id)
            .order_by(desc(UrlHistory.id))
        ).all()
    )


def delete_url_history_entry(db: Session, user_id: int, history_id: int) -> bool:
    """Remove one history row on explicit user request (current URL is protected)."""
    row = db.get(UrlHistory, history_id)
    if row is None or row.user_id != user_id:
        return False
    if row.is_current:
        raise ContentError("The current URL cannot be removed from history.")
    db.delete(row)
    db.commit()
    return True


def categories_for_user(db: Session) -> List[Category]:
    return list(db.scalars(select(Category).where(Category.is_active.is_(True)).order_by(asc(Category.sort_order))).all())


def activity_between(db: Session, user_id: int, days: int = 30) -> List[ActivityLog]:
    since = utcnow() - timedelta(days=days)
    return list(
        db.scalars(
            select(ActivityLog)
            .where(ActivityLog.user_id == user_id, ActivityLog.created_at >= since)
            .order_by(desc(ActivityLog.id))
            .limit(200)
        ).all()
    )


def ownership_check(db: Session, user: User, content_id: int) -> bool:
    item = db.get(Content, content_id)
    return bool(item and item.user_id == user.id)
