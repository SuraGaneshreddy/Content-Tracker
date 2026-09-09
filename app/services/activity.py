"""Activity feed + notification creation helpers."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..models import ActivityLog, Content, Notification, utcnow

ACTIVITY_ICONS = {
    "added": "➕",
    "updated": "✏️",
    "progress": "📈",
    "opened": "🔗",
    "favorited": "⭐",
    "unfavorited": "☆",
    "deleted": "🗑️",
    "restored": "♻️",
    "purged": "🔥",
    "url_changed": "🔀",
    "url_broken": "⚠️",
    "update_found": "🔔",
    "imported": "📥",
    "exported": "📤",
    "status": "🏷️",
    "backup": "💾",
    "account": "👤",
}

ACTIVITY_RETENTION = 300  # keep the most recent N rows per user


def log_activity(
    db: Session,
    user_id: int,
    action: str,
    message: str,
    *,
    content_id: Optional[int] = None,
    meta: Optional[dict] = None,
) -> ActivityLog:
    row = ActivityLog(
        user_id=user_id,
        content_id=content_id,
        action=action,
        message=message[:300],
        icon=ACTIVITY_ICONS.get(action, "•"),
        meta=meta,
    )
    db.add(row)
    db.flush()
    _trim_activity(db, user_id)
    return row


def _trim_activity(db: Session, user_id: int) -> None:
    ids = list(
        db.scalars(
            select(ActivityLog.id)
            .where(ActivityLog.user_id == user_id)
            .order_by(desc(ActivityLog.id))
            .offset(ACTIVITY_RETENTION)
            .limit(500)
        ).all()
    )
    if ids:
        db.query(ActivityLog).filter(ActivityLog.id.in_(ids)).delete(synchronize_session=False)


def recent_activity(db: Session, user_id: int, limit: int = 12) -> list[ActivityLog]:
    return list(
        db.scalars(
            select(ActivityLog)
            .where(ActivityLog.user_id == user_id)
            .order_by(desc(ActivityLog.id))
            .limit(limit)
        ).all()
    )


def create_notification(
    db: Session,
    user_id: int,
    kind: str,
    title: str,
    *,
    message: str = "",
    link: Optional[str] = None,
    content_id: Optional[int] = None,
    icon: str = "🔔",
    commit: bool = True,
) -> Notification:
    note = Notification(
        user_id=user_id,
        content_id=content_id,
        kind=kind,
        title=title[:200],
        message=(message or "")[:500],
        link=link,
        icon=icon,
    )
    db.add(note)
    if commit:
        db.commit()
        db.refresh(note)
    else:
        db.flush()
    return note


def unread_count(db: Session, user_id: int) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.is_read.is_(False)
            )
        )
        or 0
    )


def list_notifications(db: Session, user_id: int, *, only_unread: bool = False, limit: int = 50):
    stmt = select(Notification).where(Notification.user_id == user_id)
    if only_unread:
        stmt = stmt.where(Notification.is_read.is_(False))
    return list(db.scalars(stmt.order_by(desc(Notification.id)).limit(limit)).all())


def content_title(db: Session, content_id: Optional[int]) -> str:
    if not content_id:
        return ""
    item = db.get(Content, content_id)
    return item.title if item else ""
