"""
Updates hub + per-category update pages.

The "updates" concept has two halves:

1. **Your library** — verified new chapters/episodes/releases for items you
   actually saved (see ``services/updates.py``).
2. **Category feeds** — public, reputable listings (anime season charts, movie
   release calendars, RSS headlines, sports fixtures) shown for discovery.
   These never touch your library and are clearly labelled as public data.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import MOVIE_COUNTRIES, NEWS_TOPICS, SPORT_TYPES
from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import User
from ..services import content_service, updates
from ..services.metadata import jikan_season_current, jikan_season_upcoming, tmdb_upcoming
from ..services.rss import CODING_FEEDS, NEWS_FEEDS, SPORTS_FEEDS, coding_feed, topic_feed

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["updates"])


# ---------------------------------------------------------------------------
# Your updates
# ---------------------------------------------------------------------------
@router.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = updates.pending_updates(db, user.id, limit=100)
    history = updates.update_history(db, user.id, limit=60)
    broken = [
        item
        for item in db.scalars(
            select(content_service.Content)
            .where(
                content_service.Content.user_id == user.id,
                content_service.Content.deleted_at.is_(None),
                content_service.Content.url_status.in_(["broken", "degraded"]),
            )
            .order_by(content_service.Content.url_last_checked_at.desc())
            .limit(20)
        ).all()
    ]
    return templates.TemplateResponse(
        request,
        "updates.html",
        page_context(
            request,
            user,
            db,
            title="Updates",
            items=items,
            history=history,
            broken=broken,
            scheduler_status=None,
        ),
    )


@router.post("/api/run-checks")
def run_checks(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Manual "Check now" — runs URL health + update detection for this pass."""
    from ..services import url_checker
    from ..utils.safe_http import rate_limiter

    rate_limiter.reset_run()
    url_summary = url_checker.check_many(db, url_checker.items_due_for_check(db, 0, limit=25))
    update_summary = updates.run_update_checks(db, limit=20)
    return {"ok": True, "urls": url_summary, "updates": update_summary}


@router.post("/api/updates/mark")
def mark_updates(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    payload: Dict[str, Any] = Body(...),
):
    state = str(payload.get("state") or "read")
    content_id = payload.get("content_id")
    if content_id:
        content_service.set_update_state(db, user.id, content_id, state)
        return {"ok": True, "changed": 1}
    items = updates.pending_updates(db, user.id, limit=200)
    for item in items:
        content_service.set_update_state(db, user.id, item.id, state)
    return {"ok": True, "changed": len(items)}


# ---------------------------------------------------------------------------
# Anime updates
# ---------------------------------------------------------------------------
@router.get("/updates/anime", response_class=HTMLResponse)
def anime_updates(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    airing = jikan_season_current() if settings.jikan_enabled else []
    upcoming = jikan_season_upcoming() if settings.jikan_enabled else []
    mine = _my_category_items(db, user.id, "anime")
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title="Anime updates",
            kind="anime",
            sections=[
                {"title": "Your anime with new episodes", "items": [i for i in mine if i.update_available]},
                {"title": "Your anime library", "items": mine},
            ],
            cards=airing,
            secondary=[{"title": "Upcoming anime", "cards": upcoming}],
            source_note="Public season data from MyAnimeList (Jikan API). No key required.",
            available=settings.jikan_enabled,
            unavailable_reason="Anime lookups are disabled in the configuration." if not settings.jikan_enabled else "",
        ),
    )


# ---------------------------------------------------------------------------
# Manga / manhwa / manhua updates
# ---------------------------------------------------------------------------
def _series_updates_page(request: Request, user: User, db: Session, slug: str, title: str, icon: str):
    mine = _my_category_items(db, user.id, slug)
    with_updates = [i for i in mine if i.update_available]
    recently = sorted(mine, key=lambda i: i.updated_at or i.created_at, reverse=True)[:12]
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title=title,
            kind=slug,
            sections=[
                {"title": "New chapters detected", "items": with_updates},
                {"title": f"Your {title.lower()} — recently updated", "items": recently},
            ],
            cards=[],
            secondary=[],
            source_note=(
                "Chapter availability is read from the page you saved, so it only reports "
                "changes it can actually verify. Use “Check now” on an item to refresh it."
            ),
            available=True,
            unavailable_reason="",
        ),
    )


@router.get("/updates/manga", response_class=HTMLResponse)
def manga_updates(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _series_updates_page(request, user, db, "manga", "Manga updates", "📖")


@router.get("/updates/manhwa", response_class=HTMLResponse)
def manhwa_updates(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _series_updates_page(request, user, db, "manhwa", "Manhwa updates", "📘")


@router.get("/updates/manhua", response_class=HTMLResponse)
def manhua_updates(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _series_updates_page(request, user, db, "manhua", "Manhua updates", "📗")


# ---------------------------------------------------------------------------
# Movie / cinema updates
# ---------------------------------------------------------------------------
@router.get("/updates/movies", response_class=HTMLResponse)
def movie_updates(
    request: Request,
    country: str = Query("all"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    countries = [{"code": "all", "name": "All countries"}] + [
        {"code": code, "name": name} for code, name in MOVIE_COUNTRIES
    ]
    cards: List[dict] = []
    available = settings.has_tmdb
    if available:
        cards = tmdb_upcoming(None if country in ("all", "other") else country)

    mine = _my_category_items(db, user.id, "movie")
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title="Movie / cinema updates",
            kind="movie",
            sections=[{"title": "Your movies", "items": mine}],
            cards=cards,
            secondary=[],
            filters={"name": "Country", "options": countries, "current": country, "param": "country"},
            source_note=(
                "Release data from TMDB."
                if available
                else "Add TMDB_API_KEY to .env to see upcoming releases and trailers here."
            ),
            available=available,
            unavailable_reason="" if available else "TMDB API key not configured.",
        ),
    )


# ---------------------------------------------------------------------------
# Sports updates
# ---------------------------------------------------------------------------
@router.get("/updates/sports", response_class=HTMLResponse)
def sports_updates(
    request: Request,
    sport: str = Query("cricket"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    options = [{"code": code, "name": name} for code, name in SPORT_TYPES]
    entries = topic_feed(sport) if settings.rss_enabled else []
    mine = _my_category_items(db, user.id, "sports")
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title="Sports updates",
            kind="sports",
            sections=[{"title": "Teams & events you follow", "items": mine}],
            cards=[],
            secondary=[{"title": f"{dict(SPORT_TYPES).get(sport, 'Sport')} headlines", "entries": entries}],
            filters={"name": "Sport", "options": options, "current": sport, "param": "sport"},
            source_note="Public RSS feeds from established sports desks.",
            available=settings.rss_enabled,
            unavailable_reason="RSS is disabled in the configuration.",
        ),
    )


# ---------------------------------------------------------------------------
# News updates
# ---------------------------------------------------------------------------
@router.get("/updates/news", response_class=HTMLResponse)
def news_updates(
    request: Request,
    topic: str = Query("technology"),
    country: str = Query("all"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    topics = [{"code": code, "name": name} for code, name in NEWS_TOPICS]
    countries = [{"code": "all", "name": "All countries"}] + [
        {"code": code, "name": name} for code, name in MOVIE_COUNTRIES
    ]
    entries = topic_feed(topic) if settings.rss_enabled else []
    mine = _my_category_items(db, user.id, "news")
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title="News updates",
            kind="news",
            sections=[{"title": "Sources you follow", "items": mine}],
            cards=[],
            secondary=[{"title": f"{dict(NEWS_TOPICS).get(topic, 'Topic')} headlines", "entries": entries}],
            filters={"name": "Topic", "options": topics, "current": topic, "param": "topic"},
            second_filter={"name": "Country", "options": countries, "current": country, "param": "country"},
            source_note="Public RSS feeds from major news organisations.",
            available=settings.rss_enabled,
            unavailable_reason="RSS is disabled in the configuration.",
        ),
    )


# ---------------------------------------------------------------------------
# Coding updates
# ---------------------------------------------------------------------------
@router.get("/updates/coding", response_class=HTMLResponse)
def coding_updates(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    entries = coding_feed() if settings.rss_enabled else []
    mine = _my_category_items(db, user.id, "coding")
    return templates.TemplateResponse(
        request,
        "category_updates.html",
        page_context(
            request,
            user,
            db,
            title="Coding updates",
            kind="coding",
            sections=[{"title": "Courses & projects you track", "items": mine}],
            cards=[],
            secondary=[{"title": "Developer news", "entries": entries}],
            source_note="Public feeds: " + ", ".join(name for name, _ in CODING_FEEDS),
            available=settings.rss_enabled,
            unavailable_reason="RSS is disabled in the configuration.",
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _my_category_items(db: Session, user_id: int, slug: str, limit: int = 60):
    category = db.scalar(select(content_service.Category).where(content_service.Category.slug == slug))
    if category is None:
        return []
    return list(
        db.scalars(
            select(content_service.Content)
            .where(
                content_service.Content.user_id == user_id,
                content_service.Content.category_id == category.id,
                content_service.Content.deleted_at.is_(None),
            )
            .order_by(content_service.Content.update_available.desc(), content_service.Content.updated_at.desc())
            .limit(limit)
        ).all()
    )
