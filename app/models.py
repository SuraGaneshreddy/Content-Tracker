"""
ORM models.

Schema notes
------------
* Every user-owned row carries ``user_id`` and is *always* filtered by it in
  the service layer, so one user can never read another user's library.
* Deletes are soft (``deleted_at``); a separate ``purged_at`` marks the row as
  hard-deleted after the trash is emptied.
* ``Content`` is intentionally generic (a single row per tracked item) with a
  small set of nullable progress columns plus a JSON bag.  Adding a new
  category therefore never requires a schema migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored naive-UTC by SQLite, normalised here)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Users & sessions
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Password recovery
    reset_token_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    reset_token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    contents: Mapped[List["Content"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    settings: Mapped[Optional["UserSettings"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email}>"


class SessionToken(Base):
    """Server-side session records. Only the hash of the cookie token is stored."""

    __tablename__ = "session_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped[User] = relationship()


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )

    theme: Mapped[str] = mapped_column(String(10), nullable=False, default="dark")  # dark | light
    default_view: Mapped[str] = mapped_column(String(20), nullable=False, default="grid")  # grid | list
    items_per_page: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    sidebar_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    auto_check_urls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_check_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    notify_broken_url: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_new_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    default_category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="settings")


# ---------------------------------------------------------------------------
# Reference data (seeded, but rows are real DB rows so new ones can be added)
# ---------------------------------------------------------------------------
class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="📌")
    accent: Mapped[str] = mapped_column(String(20), nullable=False, default="#6366f1")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # Which progress fields make sense for this category.  Drives both the
    # add/edit form and the detail page.
    progress_fields: Mapped[str] = mapped_column(String(200), nullable=False, default="custom")
    # Which status values are offered.
    status_options: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # How to look for updates: none | page | jikan | tmdb | rss | espn
    update_source: Mapped[str] = mapped_column(String(20), nullable=False, default="page")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def progress_field_list(self) -> List[str]:
        return [p.strip() for p in (self.progress_fields or "").split(",") if p.strip()]

    def status_option_list(self) -> List[str]:
        return [s.strip() for s in (self.status_options or "").split(",") if s.strip()]


class StatusOption(Base):
    """Canonical status vocabulary (also used to render the status pills)."""

    __tablename__ = "statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    group: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#64748b")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


# ---------------------------------------------------------------------------
# Core content
# ---------------------------------------------------------------------------
class Content(Base):
    __tablename__ = "content"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="following", index=True)

    # Progress (sparse; only the relevant columns are filled per category)
    current_chapter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_episode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_season: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    progress_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    progress_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    progress_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1..10
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Images
    cover_image_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    cover_cached_path: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    # Usage tracking
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # URL health
    url_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", index=True)
    url_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    url_last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    url_last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # Update detection
    latest_available: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    update_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    update_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unread")  # unread|read|ignored
    update_last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    update_last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    update_source: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # Soft delete / trash
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    category: Mapped[Category] = relationship(lazy="joined")
    user: Mapped["User"] = relationship(back_populates="contents")
    tags: Mapped[List["Tag"]] = relationship(
        secondary="content_tags", back_populates="contents", lazy="selectin"
    )
    url_history: Mapped[List["UrlHistory"]] = relationship(
        back_populates="content", cascade="all, delete-orphan", order_by="UrlHistory.id.desc()"
    )

    @property
    def cover_src(self) -> str:
        """Best available cover source for the templates."""
        if self.cover_cached_path:
            return f"/media/thumbs/{self.cover_cached_path}"
        return self.cover_image_url or ""


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    contents: Mapped[List[Content]] = relationship(secondary="content_tags", back_populates="tags")


class ContentTag(Base):
    __tablename__ = "content_tags"
    __table_args__ = (UniqueConstraint("content_id", "tag_id", name="uq_content_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), index=True, nullable=False
    )


class Favorite(Base):
    """Explicit favourite records (Content.is_favorite mirrors this for fast queries)."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "content_id", name="uq_favorite_user_content"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# History, notifications, activity
# ---------------------------------------------------------------------------
class UrlHistory(Base):
    """Every URL ever attached to a content row. Never auto-deleted."""

    __tablename__ = "url_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # user|suggested|import
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    replaced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    content: Mapped[Content] = relationship(back_populates="url_history")


class UpdateHistory(Base):
    """One row per detected update, so the UI can show a real timeline."""

    __tablename__ = "update_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="chapter")
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unread")  # unread|read|ignored
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    acted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="info", index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="🔔")
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("content.id", ondelete="SET NULL"), index=True, nullable=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    icon: Mapped[str] = mapped_column(String(16), nullable=False, default="•")
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class UrlCheckLog(Base):
    """Audit trail of link health checks (also powers the admin log view)."""

    __tablename__ = "url_check_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    host: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ExternalCache(Base):
    """
    Small key/value cache for outbound API responses.

    Keeps us from hammering third-party APIs and makes the app work offline
    with the last known good data.
    """

    __tablename__ = "external_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# Shared lookup helpers
# ---------------------------------------------------------------------------

def list_categories(db) -> list:
    """Every category row in sidebar order. Used by the content form and filters."""
    return list(db.scalars(select(Category).order_by(Category.sort_order, Category.id)))


def list_statuses(db) -> list:
    """Every status row in display order."""
    return list(db.scalars(select(StatusOption).order_by(StatusOption.sort_order, StatusOption.id)))
