"""Library, category pages, favourites, tags, search and trash."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import LIBRARY_FILTERS, SORT_OPTIONS, STATUSES, STATUS_LABELS
from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import Category, Content, Tag, User
from ..services import content_service
from ..services.content_service import ContentError, LibraryQuery

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["library"])


def _int_or(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _common_query_params(request: Request) -> LibraryQuery:
    params = request.query_params
    return LibraryQuery(
        category=params.get("category") or None,
        status_filter=params.get("status") or "all",
        tag=params.get("tag") or None,
        rating=_int_or(params.get("rating"), 0) or None,
        favorites_only=params.get("favorites") == "1",
        search=params.get("q") or None,
        sort=params.get("sort") or "recent_added",
        page=max(1, _int_or(params.get("page"), 1)),
        per_page=max(6, min(_int_or(params.get("per_page"), 24), 120)),
    )


def _render_library(request: Request, user: User, db: Session, template: str, title: str, **extra):
    query = _common_query_params(request)
    if extra.get("force_favorites"):
        query.favorites_only = True
    if extra.get("force_category"):
        query.category = extra.pop("force_category")
    if extra.get("include_deleted"):
        query.include_deleted = True
        extra.pop("include_deleted")

    items, total, pages = content_service.list_content(db, user.id, query)
    context = page_context(
        request,
        user,
        db,
        title=title,
        items=items,
        total=total,
        pages=pages,
        query=query,
        sort_options=SORT_OPTIONS,
        status_filters=list(LIBRARY_FILTERS.keys()),
        status_labels={
            "all": "All",
            "reading": "Reading",
            "watching": "Watching",
            "following": "Following",
            "completed": "Completed",
            "planning": "Planning",
            "on_hold": "On Hold",
            "dropped": "Dropped",
        },
        all_tags=content_service.all_tags(db, user.id),
    )
    context.update(extra)
    return templates.TemplateResponse(request, template, context)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
@router.get("/library", response_class=HTMLResponse)
def library(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _render_library(request, user, db, "library.html", "My Library")


@router.get("/favorites", response_class=HTMLResponse)
def favorites(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _render_library(request, user, db, "library.html", "Favorites", force_favorites=True, show_favorites=True)


@router.get("/category/{slug}", response_class=HTMLResponse)
def category_page(request: Request, slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    category = db.scalar(select(Category).where(Category.slug == slug, Category.is_active.is_(True)))
    if category is None:
        raise HTTPException(status_code=404, detail="Unknown category")
    return _render_library(
        request, user, db, "library.html", f"{category.icon} {category.name}",
        force_category=slug, active_category=category,
    )


@router.get("/trash", response_class=HTMLResponse)
def trash(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = content_service.trash_items(db, user.id)
    return templates.TemplateResponse(
        request, "trash.html", page_context(request, user, db, title="Trash", items=items)
    )


@router.post("/trash/empty")
def empty_trash(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    removed = content_service.empty_trash(db, user.id)
    return RedirectResponse(f"/trash?emptied={removed}", status_code=303)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "tags.html", page_context(request, user, db, title="Tags", all_tags=content_service.all_tags(db, user.id))
    )


@router.post("/api/tags/prune")
def prune_tags(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    removed = content_service.delete_unused_tags(db, user.id)
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    term = (request.query_params.get("q") or "").strip()
    results = content_service.search_everything(db, user.id, term, limit=60) if term else []
    return templates.TemplateResponse(
        request,
        "search.html",
        page_context(request, user, db, title="Search", term=term, results=results),
    )


@router.get("/api/search")
def search_api(request: Request, q: str = Query("", max_length=120), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Type-ahead search for the header box."""
    return {"results": content_service.search_everything(db, user.id, q, limit=8)}


# ---------------------------------------------------------------------------
# Library JSON (used by the mobile/infinite scroll view)
# ---------------------------------------------------------------------------
@router.get("/api/library")
def library_api(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = _common_query_params(request)
    items, total, pages = content_service.list_content(db, user.id, query)
    return {
        "ok": True,
        "total": total,
        "page": query.page,
        "pages": pages,
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "cover": item.cover_src,
                "category": item.category.name if item.category else "",
                "icon": item.category.icon if item.category else "📌",
                "accent": item.category.accent if item.category else "#64748b",
                "status": item.status,
                "status_label": STATUS_LABELS.get(item.status, item.status),
                "progress": content_service.progress_label(item),
                "rating": item.rating,
                "favorite": item.is_favorite,
                "url": item.url,
                "updated": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ],
    }
