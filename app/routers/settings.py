"""User preferences: theme, view density, automatic checks, cache."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings as app_settings
from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import User, UserSettings
from ..services import thumbnails
from ..services.seed import ensure_user_settings

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["settings"])


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    prefs = ensure_user_settings(db, user)
    from ..services.scheduler import scheduler

    return templates.TemplateResponse(
        request,
        "settings.html",
        page_context(
            request,
            user,
            db,
            title="Settings",
            prefs=prefs,
            cache=thumbnails.cache_stats(),
            scheduler=scheduler.status(),
            integrations={
                "tmdb": app_settings.has_tmdb,
                "jikan": app_settings.jikan_enabled,
                "newsapi": app_settings.has_newsapi,
                "rss": app_settings.rss_enabled,
                "espn": app_settings.espn_enabled,
            },
            message=request.query_params.get("msg"),
        ),
    )


@router.post("/settings")
def settings_update(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    theme: str = Form(""),
    default_view: str = Form(""),
    items_per_page: str = Form("24"),
    default_category: str = Form(""),
    auto_check_urls: str = Form(""),
    auto_check_updates: str = Form(""),
    check_interval_minutes: str = Form("90"),
    notify_broken_url: str = Form(""),
    notify_new_update: str = Form(""),
):
    prefs = ensure_user_settings(db, user)
    if theme in ("dark", "light"):
        prefs.theme = theme
    if default_view in ("grid", "list"):
        prefs.default_view = default_view
    try:
        prefs.items_per_page = max(6, min(int(items_per_page), 120))
    except ValueError:
        pass
    prefs.default_category = default_category or None

    prefs.auto_check_urls = auto_check_urls == "on"
    prefs.auto_check_updates = auto_check_updates == "on"
    prefs.notify_broken_url = notify_broken_url == "on"
    prefs.notify_new_update = notify_new_update == "on"
    try:
        prefs.check_interval_minutes = max(15, min(int(check_interval_minutes), 1440))
    except ValueError:
        pass

    db.commit()
    return RedirectResponse("/settings?msg=saved", status_code=303)


@router.post("/api/theme")
def set_theme(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    theme: str = Body("dark", embed=True),
):
    """Toggle theme from the header button (AJAX, no reload)."""
    theme = theme if theme in ("dark", "light") else "dark"
    prefs = ensure_user_settings(db, user)
    prefs.theme = theme
    db.commit()
    return {"ok": True, "theme": theme}


@router.post("/api/settings/view")
def set_view(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    view: str = Body("grid", embed=True),
):
    view = view if view in ("grid", "list") else "grid"
    prefs = ensure_user_settings(db, user)
    prefs.default_view = view
    db.commit()
    return {"ok": True, "view": view}


@router.post("/api/cache/clear")
def clear_cache(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from sqlalchemy import delete

    from ..models import ExternalCache

    removed_images = thumbnails.clear_cache()
    db.execute(delete(ExternalCache))
    db.commit()
    return {"ok": True, "images_removed": removed_images}
