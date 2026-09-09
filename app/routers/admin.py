"""
Admin / system page.

Optional by design: the first registered account is the admin.  Everything
here is read-only diagnostics plus a few maintenance actions (run checks,
clear cache, prune logs).  No secrets are ever rendered.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings as app_settings
from ..database import engine, get_db
from ..deps import csrf_required, current_admin, page_context, templates
from ..models import (
    ActivityLog,
    Content,
    ExternalCache,
    Notification,
    SessionToken,
    UpdateHistory,
    UrlCheckLog,
    UrlHistory,
    User,
)
from ..services import thumbnails, url_checker, updates
from ..services.scheduler import scheduler

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["admin"])


def _table_counts(db: Session) -> list[dict]:
    tables = [
        ("Users", User),
        ("Sessions", SessionToken),
        ("Content", Content),
        ("URL history", UrlHistory),
        ("URL check log", UrlCheckLog),
        ("Update history", UpdateHistory),
        ("Notifications", Notification),
        ("Activity log", ActivityLog),
        ("External cache", ExternalCache),
    ]
    return [{"name": name, "rows": int(db.scalar(select(func.count(model.id))) or 0)} for name, model in tables]


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.id)).all())
    for row in users:
        row.item_count = int(
            db.scalar(select(func.count(Content.id)).where(Content.user_id == row.id)) or 0
        )

    return templates.TemplateResponse(
        request,
        "admin.html",
        page_context(
            request,
            admin,
            db,
            title="System",
            users=users,
            tables=_table_counts(db),
            logs=url_checker.recent_check_logs(db, limit=40),
            scheduler=scheduler.status(),
            cache=thumbnails.cache_stats(),
            integrations={
                "tmdb": app_settings.has_tmdb,
                "jikan": app_settings.jikan_enabled,
                "newsapi": app_settings.has_newsapi,
                "rss": app_settings.rss_enabled,
                "espn": app_settings.espn_enabled,
            },
            db_url=_safe_db_url(),
            message=request.query_params.get("msg"),
        ),
    )


def _safe_db_url() -> str:
    """Show the DB location without leaking credentials."""
    url = app_settings.database_url
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***:***@{rest.split('@', 1)[1]}"
    return url


@router.post("/admin/run-checks")
def admin_run_checks(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    summary = scheduler.run_once()
    return RedirectResponse(f"/admin?msg=checks-run", status_code=303)


@router.post("/admin/clear-cache")
def admin_clear_cache(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    from sqlalchemy import delete

    removed = thumbnails.clear_cache()
    db.execute(delete(ExternalCache))
    db.commit()
    return RedirectResponse("/admin?msg=cache-cleared", status_code=303)


@router.post("/admin/prune-logs")
def admin_prune(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    url_checker.prune_check_logs(db, keep=200)
    updates.prune_history(db, keep_per_user=100)
    return RedirectResponse("/admin?msg=logs-pruned", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
def toggle_user(
    request: Request,
    user_id: int,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse("/admin?err=user-not-found", status_code=303)
    if target.id == admin.id:
        return RedirectResponse("/admin?err=cannot-disable-self", status_code=303)
    target.is_active = not target.is_active
    if not target.is_active:
        db.query(SessionToken).filter(SessionToken.user_id == target.id).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse("/admin?msg=user-updated", status_code=303)


@router.get("/api/system/status")
def system_status(admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    return {
        "scheduler": scheduler.status(),
        "cache": thumbnails.cache_stats(),
        "tables": _table_counts(db),
        "env": app_settings.app_env,
    }
