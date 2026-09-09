"""
Update detection.

Design rule (from the spec): **never claim an update that cannot be
verified.**  Each checker must produce a concrete, comparable value:

===========  ==========================================================
manga-like   highest chapter number found on the saved page
anime        newest episode number reported by the public anime API
movie        a release date that has now passed
news/coding  a new entry GUID/title at the top of the item's feed
sports/other a changed content fingerprint for the saved page
===========  ==========================================================

If the source cannot be reached, or the value is unchanged/absent, the result
is ``no_update`` (or ``error``) and nothing is shown to the user.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import CATEGORY_ANIME, CATEGORY_MOVIE, SERIES_CATEGORIES, STATUSES
from ..models import Content, Notification, UpdateHistory, UserSettings, utcnow
from ..utils.safe_http import FetchError, ThrottledError, UnsafeUrlError, safe_get
from .metadata import jikan_latest_episode, scrape_page, tmdb_search_movie
from .rss import fetch_feed


@dataclass
class UpdateCheckResult:
    found: bool
    label: str = ""
    detail: str = ""
    source: str = ""
    kind: str = "chapter"
    link: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


NOT_FOUND = UpdateCheckResult(found=False)


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------
def check_series(item: Content) -> UpdateCheckResult:
    """Manga / manhwa / manhua: look for a higher chapter number on the page."""
    meta = scrape_page(item.url, force=True)
    if meta.failed:
        # The page could not be read: report the problem, claim nothing.
        return UpdateCheckResult(found=False, error=meta.error or "Could not check this source.")

    chapter = meta.chapter
    if chapter is None:
        return UpdateCheckResult(found=False, error=None)

    current = item.current_chapter
    if current is None:
        # Nothing to compare against — record the discovered value silently.
        return UpdateCheckResult(found=False, detail=f"Latest seen: Chapter {chapter:g}")

    if chapter > current:
        return UpdateCheckResult(
            found=True,
            kind="chapter",
            label=f"Chapter {chapter:g}",
            detail=f"Chapter {chapter:g} is available (you are on {current:g}).",
            source=meta.site_name or "saved page",
            link=item.url,
        )
    return UpdateCheckResult(found=False, detail=f"Latest seen: Chapter {chapter:g}")


def check_anime(item: Content) -> UpdateCheckResult:
    """Anime: newest episode from the public anime API (requires external_id)."""
    if not settings.jikan_enabled:
        return UpdateCheckResult(found=False, error="Anime API disabled.")
    if not item.external_id:
        return UpdateCheckResult(found=False, error="No linked anime ID.")

    latest = jikan_latest_episode(item.external_id)
    if not latest or latest.get("episode") is None:
        return UpdateCheckResult(found=False)

    episode = int(latest["episode"])
    current = item.current_episode
    if current is None:
        return UpdateCheckResult(found=False, detail=f"Latest aired: Episode {episode}")
    if episode > current:
        return UpdateCheckResult(
            found=True,
            kind="episode",
            label=f"Episode {episode}",
            detail=f"Episode {episode} has aired (you are on {current}).",
            source="MyAnimeList",
            link=item.url,
        )
    return UpdateCheckResult(found=False, detail=f"Latest aired: Episode {episode}")


def check_movie(item: Content) -> UpdateCheckResult:
    """Movies: a known release date that has now passed."""
    if not settings.has_tmdb:
        return UpdateCheckResult(found=False, error="TMDB key not configured.")
    results = tmdb_search_movie(item.title)
    if not results:
        return UpdateCheckResult(found=False)
    best = results[0]
    release = best.get("release_date")
    if not release:
        return UpdateCheckResult(found=False)
    try:
        release_date = datetime.strptime(release[:10], "%Y-%m-%d")
    except ValueError:
        return UpdateCheckResult(found=False)

    extra = dict(item.progress_json or {})
    if release_date.date() <= utcnow().date() and extra.get("released_notified") != release:
        return UpdateCheckResult(
            found=True,
            kind="release",
            label=f"Released {release_date:%d %b %Y}",
            detail=f"{item.title} is now released.",
            source="TMDB",
            link=best.get("url") or item.url,
        )
    return UpdateCheckResult(found=False, detail=f"Release date: {release_date:%d %b %Y}")


def check_feed(item: Content) -> UpdateCheckResult:
    """News / coding: a new entry at the top of the item's feed."""
    entries = fetch_feed(item.url, force=True)
    if not entries:
        # Not a feed — fall back to page fingerprinting.
        return check_page_fingerprint(item)
    top = entries[0]
    fingerprint = hashlib.sha256(f"{top.title}|{top.link or ''}".encode("utf-8")).hexdigest()
    extra = dict(item.progress_json or {})
    previous = extra.get("feed_top")
    if previous is None:
        return UpdateCheckResult(found=False, detail=f"Latest: {top.title[:80]}")
    if previous != fingerprint:
        return UpdateCheckResult(
            found=True,
            kind="article",
            label=top.title[:120],
            detail=f"New post: {top.title[:120]}",
            source=top.source or "feed",
            link=top.link or item.url,
        )
    return UpdateCheckResult(found=False, detail=f"Latest: {top.title[:80]}")


def check_page_fingerprint(item: Content) -> UpdateCheckResult:
    """
    Generic "this page changed" detector.

    Only reports an update when the *previously stored* fingerprint differs,
    so it can never claim a change it has not actually observed.
    """
    try:
        result = safe_get(item.url, force=True, max_bytes=256 * 1024)
    except (UnsafeUrlError, FetchError, ThrottledError) as exc:
        return UpdateCheckResult(found=False, error=str(exc))
    if result.status_code >= 400:
        return UpdateCheckResult(found=False, error=f"HTTP {result.status_code}")

    fingerprint = hashlib.sha256(result.text[:200_000].encode("utf-8", "replace")).hexdigest()
    extra = dict(item.progress_json or {})
    previous = extra.get("page_fingerprint")
    if previous is None:
        return UpdateCheckResult(found=False, detail="Baseline recorded.")
    if previous != fingerprint:
        return UpdateCheckResult(
            found=True,
            kind="change",
            label="Page changed",
            detail="The saved page has new content since your last check.",
            source="saved page",
            link=item.url,
        )
    return UpdateCheckResult(found=False)


CHECKERS: Dict[str, Callable[[Content], UpdateCheckResult]] = {
    "page": check_page_fingerprint,
    "series": check_series,
    "jikan": check_anime,
    "tmdb": check_movie,
    "rss": check_feed,
    "espn": check_page_fingerprint,
    "none": lambda item: UpdateCheckResult(found=False, error="Update checking disabled for this category."),
}


def checker_for(item: Content) -> Callable[[Content], UpdateCheckResult]:
    """Pick the right checker for an item (category default, per-item override)."""
    slug = item.category.slug if item.category else "other"
    if slug in SERIES_CATEGORIES:
        return check_series
    source = (item.update_source or (item.category.update_source if item.category else "none")).lower()
    return CHECKERS.get(source, check_page_fingerprint)


# ---------------------------------------------------------------------------
# Apply + record
# ---------------------------------------------------------------------------
def apply_result(db: Session, item: Content, result: UpdateCheckResult) -> bool:
    """Write the outcome back to the item; create history + notification if new."""
    now = utcnow()
    item.update_last_checked_at = now
    item.update_last_error = result.error
    if result.source:
        item.update_source = result.source

    extra = dict(item.progress_json or {})
    if result.kind == "chapter" and result.label.startswith("Chapter "):
        try:
            extra["latest_seen_chapter"] = float(result.label.split()[1])
        except (IndexError, ValueError):
            pass
    if result.detail.startswith("Latest seen: Chapter "):
        try:
            extra["latest_seen_chapter"] = float(result.detail.split()[-1])
        except ValueError:
            pass
    if result.detail.startswith("Latest aired: Episode "):
        try:
            extra["latest_seen_episode"] = int(result.detail.split()[-1])
        except ValueError:
            pass
    item.progress_json = extra

    if not result.found:
        if result.detail:
            item.latest_available = result.detail[:200]
        db.commit()
        return False

    item.latest_available = result.label[:200]
    item.update_available = True
    item.update_state = "unread"

    db.add(
        UpdateHistory(
            content_id=item.id,
            user_id=item.user_id,
            kind=result.kind,
            label=result.label[:200],
            detail=result.detail[:500],
            source=result.source,
            url=result.link,
            state="unread",
        )
    )

    settings_row = db.scalar(select(UserSettings).where(UserSettings.user_id == item.user_id))
    notify = True if settings_row is None else bool(settings_row.notify_new_update)
    if notify:
        db.add(
            Notification(
                user_id=item.user_id,
                content_id=item.id,
                kind="new_update",
                title=f"New update: {item.title}",
                message=result.detail or result.label,
                link=f"/content/{item.id}",
                icon="🔔",
            )
        )
    db.commit()
    return True


def record_baseline(db: Session, item: Content) -> None:
    """Store the current page fingerprint without reporting an update."""
    if item.category and item.category.slug in SERIES_CATEGORIES:
        return
    try:
        result = safe_get(item.url, force=True, max_bytes=256 * 1024)
    except (UnsafeUrlError, FetchError, ThrottledError):
        return
    if result.status_code >= 400:
        return
    extra = dict(item.progress_json or {})
    extra["page_fingerprint"] = hashlib.sha256(result.text[:200_000].encode("utf-8", "replace")).hexdigest()
    entries = fetch_feed(item.url, force=True)
    if entries:
        top = entries[0]
        extra["feed_top"] = hashlib.sha256(f"{top.title}|{top.link or ''}".encode("utf-8")).hexdigest()
    item.progress_json = extra
    db.commit()


def check_item(db: Session, item: Content, *, force: bool = False) -> UpdateCheckResult:
    checker = checker_for(item)
    try:
        result = checker(item)
    except (UnsafeUrlError, FetchError, ThrottledError) as exc:
        result = UpdateCheckResult(found=False, error=str(exc))
    except Exception as exc:  # never leak internals
        result = UpdateCheckResult(found=False, error="Could not check this source.")
    apply_result(db, item, result)
    return result


def due_items_query(db: Session, interval_hours: int, limit: int = 30) -> List[Content]:
    cutoff = utcnow() - timedelta(hours=interval_hours)
    return list(
        db.scalars(
            select(Content)
            .where(
                Content.deleted_at.is_(None),
                Content.purged_at.is_(None),
                Content.update_source != "none",
                or_(
                    Content.update_last_checked_at.is_(None),
                    Content.update_last_checked_at < cutoff,
                ),
            )
            .order_by(Content.update_last_checked_at.asc().nullsfirst(), Content.id.asc())
            .limit(limit)
        ).all()
    )


def run_update_checks(db: Session, limit: int = 30) -> dict:
    """Check the items that are due. Returns a summary."""
    summary = {"checked": 0, "found": 0, "errors": 0}
    for item in due_items_query(db, settings.update_check_interval_hours, limit=limit):
        result = check_item(db, item)
        summary["checked"] += 1
        if result.found:
            summary["found"] += 1
        if result.error:
            summary["errors"] += 1
    return summary


def pending_updates(db: Session, user_id: int, limit: int = 50) -> List[Content]:
    return list(
        db.scalars(
            select(Content)
            .where(
                Content.user_id == user_id,
                Content.deleted_at.is_(None),
                Content.update_available.is_(True),
            )
            .order_by(Content.update_state != "unread", desc(Content.updated_at))
            .limit(limit)
        ).all()
    )


def update_history(db: Session, user_id: int, limit: int = 50) -> List[UpdateHistory]:
    return list(
        db.scalars(
            select(UpdateHistory)
            .where(UpdateHistory.user_id == user_id)
            .order_by(desc(UpdateHistory.detected_at))
            .limit(limit)
        ).all()
    )


def prune_history(db: Session, keep_per_user: int = 200) -> None:
    """Trim old update history rows."""
    from sqlalchemy import func

    rows = db.execute(
        select(UpdateHistory.user_id, func.min(UpdateHistory.id))
        .group_by(UpdateHistory.user_id)
    ).all()
    for user_id, _min_id in rows:
        ids = list(
            db.scalars(
                select(UpdateHistory.id)
                .where(UpdateHistory.user_id == user_id)
                .order_by(desc(UpdateHistory.id))
                .offset(keep_per_user)
                .limit(1000)
            ).all()
        )
        if ids:
            db.query(UpdateHistory).filter(UpdateHistory.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
