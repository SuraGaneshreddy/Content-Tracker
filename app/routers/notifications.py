"""Notification centre: list, mark read, mark all read, delete."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import Notification, User
from ..services import activity as activity_service

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["notifications"])


def _owned(db: Session, user_id: int, notification_id: int) -> Notification:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    return row


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    unread_only: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    items = activity_service.list_notifications(db, user.id, only_unread=bool(unread_only), limit=100)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        page_context(
            request,
            user,
            db,
            title="Notifications",
            items=items,
            unread_only=bool(unread_only),
            unread_count=activity_service.unread_count(db, user.id),
        ),
    )


@router.get("/api/notifications")
def notifications_api(user: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 30):
    items = activity_service.list_notifications(db, user.id, limit=min(limit, 100))
    return {
        "unread": activity_service.unread_count(db, user.id),
        "items": [
            {
                "id": n.id,
                "kind": n.kind,
                "icon": n.icon,
                "title": n.title,
                "message": n.message,
                "link": n.link,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in items
        ],
    }


@router.post("/api/notifications/{notification_id}/read")
def mark_read(
    request: Request,
    notification_id: int,
    is_read: bool = Body(True, embed=True),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user.id, notification_id)
    row.is_read = is_read
    db.commit()
    return {"ok": True, "unread": activity_service.unread_count(db, user.id)}


@router.post("/api/notifications/read-all")
def mark_all_read(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    db.commit()
    return {"ok": True, "unread": 0}


@router.delete("/api/notifications/{notification_id}")
def delete_notification(
    request: Request,
    notification_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user.id, notification_id)
    db.delete(row)
    db.commit()
    return {"ok": True, "unread": activity_service.unread_count(db, user.id)}


@router.post("/api/notifications/clear")
def clear_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Remove notifications the user has already read."""
    db.execute(delete(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(True)))
    db.commit()
    return {"ok": True, "unread": activity_service.unread_count(db, user.id)}
