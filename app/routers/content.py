"""
Content CRUD + per-item actions.

Page routes render Jinja templates; the ``/api/*`` routes return JSON and are
used by the front-end for quick actions (favourite, progress, delete, open
tracking) so the UI can stay responsive without full page reloads.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import RATING_MAX, RATING_MIN, STATUSES, STATUS_LABELS
from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import Content, User, list_categories, utcnow
from ..services import alternatives as alternatives_service
from ..services import content_service, metadata, thumbnails, updates, url_checker
from ..services.content_service import ContentError, ContentPayload, LibraryQuery
from ..utils.url_validation import UrlValidationError, display_host

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["content"])


def _status_choices() -> list:
    """(slug, label, colour) for every status, ordered for display."""
    return [
        (slug, label, color)
        for slug, (label, _group, color, _order) in sorted(STATUSES.items(), key=lambda kv: kv[1][3])
    ]


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------
@router.get("/add", response_class=HTMLResponse)
def add_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    url: str = "",
    category: str = "",
    title: str = "",
):
    """The Add Content page. Accepts ?url= so bookmarklets can prefill it."""
    return templates.TemplateResponse(
        request,
        "add.html",
        page_context(
            request,
            user,
            db,
            title="Add content",
            prefill={"url": url, "category": category, "title": title, "status": "following"},
            form_error=None,
            lookup_results=None,
            all_statuses=_status_choices(),
            all_tags=content_service.all_tags(db, user.id),
        ),
    )


@router.post("/api/lookup")
def lookup_metadata(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    url: str = Body("", embed=True),
    category: str = Body("", embed=True),
    title: str = Body("", embed=True),
):
    """
    Fetch public metadata for a URL (and optionally search a catalogue API).

    Returns best-effort suggestions the user can accept or ignore.  Nothing is
    written to the database here.
    """
    result: Dict[str, Any] = {"ok": False, "meta": {}, "matches": [], "error": None}
    url = (url or "").strip()

    if url:
        try:
            from ..utils.url_validation import validate_url_syntax

            normalised = validate_url_syntax(url)
        except UrlValidationError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "meta": {}, "matches": []}, status_code=400)

        meta = metadata.scrape_page(normalised, force=True)
        result["meta"] = {
            "title": meta.title,
            "description": meta.description,
            "image_url": meta.image_url,
            "chapter": meta.chapter,
            "episode": meta.episode,
            "season": meta.season,
            "site": meta.site_name,
            "host": display_host(normalised),
        }
        result["ok"] = not meta.failed
        if meta.failed:
            result["error"] = meta.error

    search_term = (title or result["meta"].get("title") or "").strip()
    if search_term:
        if category == "anime":
            result["matches"] = [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "image_url": m["image_url"],
                    "description": m["description"],
                    "note": f"{m.get('episodes') or '?'} episodes"
                    + (f" · score {m['score']}" if m.get("score") else ""),
                    "source": "MyAnimeList",
                }
                for m in metadata.jikan_search_anime(search_term)[:6]
            ]
        elif category in ("manga", "manhwa", "manhua"):
            from ..services.catalog import cover_matches
            result["matches"] = cover_matches(category, search_term)
        elif category == "movie":
            result["matches"] = [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "image_url": m["image_url"],
                    "description": m["description"],
                    "note": m.get("release_date") or "",
                    "source": "TMDB",
                }
                for m in metadata.tmdb_search_movie(search_term)[:6]
            ]

    if not result["ok"] and not result["matches"]:
        result["error"] = "Unable to retrieve metadata. You can still fill the fields in manually."
    return result


@router.post("/add")
def add_submit(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    title: str = Form(""),
    url: str = Form(""),
    category: str = Form(""),
    status: str = Form("following"),
    chapter: str = Form(""),
    episode: str = Form(""),
    season: str = Form(""),
    volume: str = Form(""),
    progress_text: str = Form(""),
    progress_percent: str = Form(""),
    rating: str = Form(""),
    notes: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    cover_image_url: str = Form(""),
    external_id: str = Form(""),
    update_source: str = Form(""),
    auto_metadata: str = Form("1"),
):
    data = {
        "title": title,
        "url": url,
        "category": category,
        "status": status,
        "chapter": chapter,
        "episode": episode,
        "season": season,
        "volume": volume,
        "progress_text": progress_text,
        "progress_percent": progress_percent,
        "rating": rating,
        "notes": notes,
        "description": description,
        "tags": tags,
        "cover_image_url": cover_image_url or None,
        "external_id": external_id or None,
        "update_source": update_source or None,
    }

    try:
        payload = ContentPayload.from_mapping(data)
    except (ContentError, UrlValidationError) as exc:
        return templates.TemplateResponse(
            request,
            "add.html",
            page_context(
                request, user, db, title="Add content", prefill=data, form_error=str(exc), lookup_results=None,
                all_statuses=_status_choices(), all_tags=content_service.all_tags(db, user.id),
                categories=list_categories(db),
            ),
            status_code=400,
        )

    duplicate = content_service.duplicate_check(db, user.id, payload.url)

    # Best-effort metadata enrichment when the user left the cover blank.
    if auto_metadata == "1":
        scraped = metadata.scrape_page(payload.url, force=True)
        if not payload.cover_image_url and scraped.image_url:
            payload.cover_image_url = scraped.image_url
        if not payload.description and scraped.description:
            payload.description = scraped.description
        # Availability is NOT the user's reading/watching progress.
        # Ambiguous catalog matches are offered by /api/lookup for explicit selection.

    try:
        item = content_service.create_content(db, user.id, payload)
    except (ContentError, UrlValidationError) as exc:
        return templates.TemplateResponse(
            request,
            "add.html",
            page_context(
                request, user, db, title="Add content", prefill=data, form_error=str(exc), lookup_results=None,
                all_statuses=_status_choices(), all_tags=content_service.all_tags(db, user.id),
                categories=list_categories(db),
            ),
            status_code=400,
        )

    # Record a baseline so "page changed" detection has something to compare to.
    if item.category and item.category.slug not in ("manga", "manhwa", "manhua"):
        try:
            updates.record_baseline(db, item)
        except Exception:
            pass

    message = f"Saved “{item.title}” to your library."
    if duplicate is not None:
        message += " (Note: you already had this URL saved once.)"
    return RedirectResponse(f"/content/{item.id}?msg=added", status_code=303)


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
@router.get("/content/{content_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError:
        raise HTTPException(status_code=404, detail="Item not found")

    history = content_service.update_history_for(db, user.id, item.id, limit=30)
    urls = content_service.url_history_for(db, user.id, item.id)
    from ..services.activity import recent_activity

    return templates.TemplateResponse(
        request,
        "detail.html",
        page_context(
            request,
            user,
            db,
            title=item.title,
            item=item,
            progress=content_service.progress_label(item),
            update_history=history,
            url_history=urls,
            message=request.query_params.get("msg"),
            alternatives=[],
        ),
    )


@router.get("/content/{content_id}/alternatives", response_class=HTMLResponse)
def alternatives_page(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError:
        raise HTTPException(status_code=404, detail="Item not found")

    suggestions = alternatives_service.find_alternatives(db, item)
    return templates.TemplateResponse(
        request,
        "detail.html",
        page_context(
            request,
            user,
            db,
            title=f"Alternatives — {item.title}",
            item=item,
            progress=content_service.progress_label(item),
            update_history=content_service.update_history_for(db, user.id, item.id, limit=30),
            url_history=content_service.url_history_for(db, user.id, item.id),
            alternatives=[s.as_dict() for s in suggestions],
            alternatives_disclaimer=alternatives_service.DISCLAIMER,
            message=None,
        ),
    )


@router.post("/content/{content_id}/use-alternative")
def use_alternative(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    new_url: str = Form(""),
):
    """Apply a user-reviewed alternative URL (the old one stays in history)."""
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
        content_service.apply_url_change(db, item, new_url, source="suggested")
        url_checker.check_single(db, item, force=True)
        db.commit()
    except (ContentError, UrlValidationError) as exc:
        return RedirectResponse(f"/content/{content_id}/alternatives?err=1", status_code=303)
    return RedirectResponse(f"/content/{content_id}?msg=url-updated", status_code=303)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------
@router.get("/content/{content_id}/edit", response_class=HTMLResponse)
def edit_page(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError:
        raise HTTPException(status_code=404, detail="Item not found")
    return templates.TemplateResponse(
        request,
        "edit.html",
        page_context(
            request,
            user,
            db,
            title=f"Edit {item.title}",
            item=item,
            form_error=None,
            form_override=None,
            all_statuses=_status_choices(),
            all_tags=content_service.all_tags(db, user.id),
            categories=list_categories(db),
        ),
    )


@router.post("/content/{content_id}/edit")
def edit_submit(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    title: str = Form(""),
    url: str = Form(""),
    category: str = Form(""),
    status: str = Form("following"),
    chapter: str = Form(""),
    episode: str = Form(""),
    season: str = Form(""),
    volume: str = Form(""),
    progress_text: str = Form(""),
    progress_percent: str = Form(""),
    rating: str = Form(""),
    notes: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    cover_image_url: str = Form(""),
    external_id: str = Form(""),
    update_source: str = Form(""),
):
    data = {
        "title": title, "url": url, "category": category, "status": status,
        "chapter": chapter, "episode": episode, "season": season, "volume": volume,
        "progress_text": progress_text, "progress_percent": progress_percent,
        "rating": rating, "notes": notes, "description": description, "tags": tags,
        "cover_image_url": cover_image_url or None, "external_id": external_id or None,
        "update_source": update_source or None,
    }
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
        payload = ContentPayload.from_mapping(data)
        content_service.update_content(db, user.id, content_id, payload)
    except (ContentError, UrlValidationError) as exc:
        try:
            existing = content_service.get_owned_content(db, user.id, content_id)
        except ContentError:
            raise HTTPException(status_code=404, detail="Item not found")
        return templates.TemplateResponse(
            request,
            "edit.html",
            page_context(
                request, user, db, title=f"Edit {existing.title}", item=existing,
                form_error=str(exc), all_tags=content_service.all_tags(db, user.id), form_override=data,
                categories=list_categories(db),
                all_statuses=_status_choices(),
            ),
            status_code=400,
        )
    return RedirectResponse(f"/content/{content_id}?msg=updated", status_code=303)


# ---------------------------------------------------------------------------
# JSON actions
# ---------------------------------------------------------------------------
@router.post("/api/content/{content_id}/favorite")
def api_favorite(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    favorite: bool = Body(True, embed=True),
):
    try:
        item = content_service.set_favorite(db, user.id, content_id, favorite)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "favorite": item.is_favorite}


@router.post("/api/content/{content_id}/progress")
def api_progress(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    payload: Dict[str, Any] = Body(...),
):
    try:
        content_service.get_owned_content(db, user.id, content_id)  # authorisation first
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        item = content_service.update_progress(
            db,
            user.id,
            content_id,
            chapter=payload.get("chapter"),
            episode=payload.get("episode"),
            season=payload.get("season"),
            volume=payload.get("volume"),
            progress_text=payload.get("progress_text"),
            progress_percent=payload.get("progress_percent"),
            status=payload.get("status"),
        )
    except ContentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {
        "ok": True,
        "progress": content_service.progress_label(item),
        "status": item.status,
        "status_label": STATUS_LABELS.get(item.status, item.status),
    }


@router.post("/api/content/{content_id}/open")
def api_open(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Record that the user opened the link (the browser navigates separately)."""
    try:
        item = content_service.mark_opened(db, user.id, content_id)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "url": item.url, "open_count": item.open_count}


@router.post("/api/content/{content_id}/delete")
def api_delete(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    hard: bool = Body(False, embed=True),
):
    try:
        if hard:
            content_service.purge(db, user.id, content_id)
        else:
            content_service.soft_delete(db, user.id, content_id)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "hard": hard}


@router.post("/api/content/{content_id}/restore")
def api_restore(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        content_service.restore(db, user.id, content_id)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.post("/api/content/{content_id}/check-url")
def api_check_url(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    outcome = url_checker.check_single(db, item, force=True)
    db.refresh(item)
    return {
        "ok": True,
        "status": item.url_status,
        "label": item.url_status,
        "status_code": item.url_status_code,
        "error": item.url_last_error,
        "checked_at": item.url_last_checked_at.isoformat() if item.url_last_checked_at else None,
    }


@router.post("/api/content/{content_id}/check-update")
def api_check_update(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    result = updates.check_item(db, item, force=True)
    db.refresh(item)
    return {
        "ok": True,
        "found": result.found,
        "label": result.label or item.latest_available or "",
        "detail": result.detail,
        "error": result.error,
        "update_state": item.update_state,
    }


@router.post("/api/content/{content_id}/update-state")
def api_update_state(
    request: Request,
    content_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    state: str = Body("read", embed=True),
):
    try:
        content_service.set_update_state(db, user.id, content_id, state)
    except ContentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "state": state}


@router.delete("/api/url-history/{history_id}")
def api_delete_history(
    request: Request,
    history_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Explicit user request to forget one old URL."""
    try:
        ok = content_service.delete_url_history_entry(db, user.id, history_id)
    except ContentError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not ok:
        raise HTTPException(status_code=404, detail="History entry not found")
    return {"ok": True}


@router.post('/content/{content_id}/source-links', response_class=HTMLResponse)
def source_links_page(request: Request, content_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..services import source_links
    try:
        item = content_service.get_owned_content(db, user.id, content_id)
    except ContentError:
        raise HTTPException(status_code=404, detail='Item not found')
    links, error = source_links.discover(item, user.id)
    return templates.TemplateResponse(request, 'source_links.html', page_context(request,user,db,
        title='Source chapter / episode links', item=item, links=links, error=error))


@router.post('/content/{content_id}/open-numbered')
def open_numbered(request: Request, content_id: int, token: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..services import source_links
    try:
        item = content_service.get_owned_content(db,user.id,content_id)
    except ContentError:
        raise HTTPException(status_code=404, detail='Item not found')
    try:
        url,field,number = source_links.verify(token,user.id,content_id)
        if source_links.numbered(url,item.category.slug) != (field,number): raise ValueError('Invalid numbered link.')
        # Opening an older chapter never decreases the recorded high-water mark.
        current = item.current_episode if field == 'episode' else item.current_chapter
        if number > (current or 0):
            content_service.update_progress(db,user.id,content_id,**{field:number},mark_update_read=False)
        content_service.mark_opened(db,user.id,content_id)
    except (ValueError, ContentError) as exc:
        raise HTTPException(status_code=400,detail=str(exc))
    return RedirectResponse(url,status_code=303)


@router.post('/content/{content_id}/refresh-cover')
def refresh_cover(request: Request, content_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        item = content_service.get_owned_content(db,user.id,content_id)
    except ContentError:
        raise HTTPException(status_code=404,detail='Item not found')
    meta = metadata.scrape_page(item.url)
    if not meta.image_url:
        return RedirectResponse(f'/content/{content_id}?msg=cover-unavailable',status_code=303)
    item.cover_image_url = meta.image_url
    # Refreshing one item must not remove a cache file shared by another item.
    item.cover_cached_path = thumbnails.download_and_cache(meta.image_url)
    item.updated_at = utcnow()
    db.commit()
    return RedirectResponse(f'/content/{content_id}?msg=cover-refreshed',status_code=303)
