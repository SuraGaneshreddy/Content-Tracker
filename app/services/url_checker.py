"""
URL health checking.

Checks are batched and throttled (see ``safe_http.rate_limiter``) so we never
hammer a site.  Results are written back to ``Content``, mirrored onto the
matching ``UrlHistory`` row, and appended to ``UrlCheckLog`` for the admin
view.  A transition into the "broken" state raises a notification.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..constants import (
    URL_STATUS_BROKEN,
    URL_STATUS_DEGRADED,
    URL_STATUS_OK,
    URL_STATUS_UNKNOWN,
)
from ..models import Content, Notification, UrlCheckLog, UrlHistory, utcnow
from ..utils.safe_http import (
    FetchError,
    ThrottledError,
    UnsafeUrlError,
    health_status_from_code,
    resolve_and_validate,
    safe_get,
)
from .activity import log_activity

# 5xx/timeouts are transient — only call a link broken after repeated failures.
DEGRADED_STREAK_BEFORE_BROKEN = 3


def check_single(db: Session, item: Content, *, force: bool = False) -> dict:
    """
    Check one item's current URL.

    Returns a dict with ``status``, ``status_code``, ``latency_ms``, ``error``.
    """
    result = {
        "status": URL_STATUS_UNKNOWN,
        "status_code": None,
        "latency_ms": None,
        "error": None,
    }

    try:
        response = safe_get(item.url, head_only=True, force=force, accept="*/*")
        status_code = response.status_code
        result["status_code"] = status_code
        result["latency_ms"] = response.elapsed_ms
        result["status"] = health_status_from_code(status_code)

        # Some servers answer HEAD with 405 even though GET works.
        if status_code == 405:
            retry = safe_get(item.url, force=True, max_bytes=4096)
            result["status_code"] = retry.status_code
            result["latency_ms"] = retry.elapsed_ms
            result["status"] = health_status_from_code(retry.status_code)
    except UnsafeUrlError as exc:
        result["status"] = URL_STATUS_BROKEN
        result["error"] = str(exc)
    except ThrottledError:
        # Not a verdict about the link; leave the previous status in place.
        return {"status": item.url_status, "status_code": item.url_status_code, "latency_ms": None,
                "error": None, "skipped": "throttled"}
    except FetchError as exc:
        result["status"] = URL_STATUS_DEGRADED
        result["error"] = str(exc)
    except Exception:  # pragma: no cover - defensive
        result["status"] = URL_STATUS_DEGRADED
        result["error"] = "Could not check this source."

    _persist(db, item, result)
    return result


def _persist(db: Session, item: Content, result: dict) -> None:
    now = utcnow()
    previous = item.url_status
    new_status = result["status"]

    # Require repeated failures before declaring a link broken.
    if new_status == URL_STATUS_BROKEN and previous == URL_STATUS_DEGRADED:
        pass
    if new_status == URL_STATUS_DEGRADED and previous != URL_STATUS_BROKEN:
        streak = (item.progress_json or {}).get("url_fail_streak", 0)
        streak += 1
        merged = dict(item.progress_json or {})
        merged["url_fail_streak"] = streak
        item.progress_json = merged
        if streak >= DEGRADED_STREAK_BEFORE_BROKEN:
            new_status = URL_STATUS_BROKEN

    if new_status in (URL_STATUS_OK,):
        merged = dict(item.progress_json or {})
        merged["url_fail_streak"] = 0
        item.progress_json = merged

    item.url_status = new_status
    item.url_status_code = result.get("status_code")
    item.url_last_checked_at = now
    item.url_last_error = result.get("error")

    history = db.scalar(
        select(UrlHistory).where(UrlHistory.content_id == item.id, UrlHistory.is_current.is_(True))
    )
    if history is not None:
        history.status = new_status
        history.status_code = result.get("status_code")
        history.last_checked_at = now
        history.reason = result.get("error")

    db.add(
        UrlCheckLog(
            content_id=item.id,
            user_id=item.user_id,
            url=item.url,
            host=(item.url.split("/")[2] if len(item.url.split("/")) > 2 else None),
            status=new_status,
            status_code=result.get("status_code"),
            latency_ms=result.get("latency_ms"),
            error=result.get("error"),
        )
    )

    if previous != URL_STATUS_BROKEN and new_status == URL_STATUS_BROKEN:
        db.add(
            Notification(
                user_id=item.user_id,
                content_id=item.id,
                kind="broken_url",
                title=f"Link may no longer be working: {item.title}",
                message=(result.get("error") or "This URL returned a not-found response."),
                link=f"/content/{item.id}",
                icon="⚠️",
            )
        )
        log_activity(
            db,
            item.user_id,
            "url_broken",
            f"⚠ The link for {item.title} may no longer be working.",
            content_id=item.id,
        )
    db.commit()


def check_many(db: Session, items: Iterable[Content], *, force: bool = False) -> dict:
    """Check a batch of items. Returns a small summary."""
    summary = {"checked": 0, "ok": 0, "degraded": 0, "broken": 0, "skipped": 0}
    for item in items:
        outcome = check_single(db, item, force=force)
        if outcome.get("skipped"):
            summary["skipped"] += 1
            continue
        summary["checked"] += 1
        if outcome["status"] == URL_STATUS_OK:
            summary["ok"] += 1
        elif outcome["status"] == URL_STATUS_BROKEN:
            summary["broken"] += 1
        else:
            summary["degraded"] += 1
    return summary


def items_due_for_check(db: Session, interval_hours: int, limit: int = 40) -> List[Content]:
    """Items whose URL has not been checked within the interval (oldest first)."""
    cutoff = utcnow() - timedelta(hours=interval_hours)
    return list(
        db.scalars(
            select(Content)
            .where(
                Content.deleted_at.is_(None),
                Content.purged_at.is_(None),
                or_(Content.url_last_checked_at.is_(None), Content.url_last_checked_at < cutoff),
            )
            .order_by(Content.url_last_checked_at.asc().nullsfirst(), Content.id.asc())
            .limit(limit)
        ).all()
    )


def broken_items(db: Session, user_id: Optional[int] = None, limit: int = 50) -> List[Content]:
    stmt = select(Content).where(
        Content.deleted_at.is_(None),
        Content.url_status.in_([URL_STATUS_BROKEN, URL_STATUS_DEGRADED]),
    )
    if user_id is not None:
        stmt = stmt.where(Content.user_id == user_id)
    return list(db.scalars(stmt.order_by(Content.url_last_checked_at.desc()).limit(limit)).all())


def prune_check_logs(db: Session, keep: int = 500) -> int:
    """Keep only the most recent check logs to stop the table growing forever."""
    from sqlalchemy import desc

    ids = list(
        db.scalars(select(UrlCheckLog.id).order_by(desc(UrlCheckLog.id)).offset(keep).limit(2000)).all()
    )
    if ids:
        db.query(UrlCheckLog).filter(UrlCheckLog.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    return len(ids)


def recent_check_logs(db: Session, limit: int = 50) -> List[UrlCheckLog]:
    from sqlalchemy import desc

    return list(db.scalars(select(UrlCheckLog).order_by(desc(UrlCheckLog.id)).limit(limit)).all())
