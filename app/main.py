"""
Application entry point.

Run locally with:

    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db, init_db
from .deps import (
    base_context,
    current_user,
    current_user_optional,
    page_context,
    session_middleware,
    templates,
)
from .models import Content, User
from .routers import admin, auth, content, data, library, notifications, settings as settings_router, updates
from .services import content_service, thumbnails
from .services.activity import recent_activity
from .services.content_service import ContentError, LibraryQuery
from .services.scheduler import scheduler

logging.basicConfig(
    level=logging.INFO if settings.is_production else logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database ready at %s", settings.database_url.split("@")[-1])
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,  # no interactive API docs or schema
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

for router in (
    auth.router,
    content.router,
    library.router,
    updates.router,
    notifications.router,
    settings_router.router,
    data.router,
    admin.router,
):
    app.include_router(router)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)


class SecurityHeadersMiddleware:
    """
    Pure-ASGI middleware so the headers land on *every* response, including
    the ones produced by exception handlers (error pages are rendered inside
    the BaseHTTPMiddleware stack and would otherwise skip a BaseHTTPMiddleware
    registered above them).
    """

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                headers.setdefault("Content-Security-Policy", CSP)
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                if settings.is_production:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def attach_csrf_cookie(request: Request, call_next):
    """Ensure the CSRF cookie exists on every response (needed for AJAX)."""
    response = await call_next(request)
    from .deps import CSRF_COOKIE, issue_csrf

    try:
        # A route that just created a session (login/register) stamps its own
        # session-bound CSRF cookie. Don't overwrite it with the anonymous one.
        prefix = f"{CSRF_COOKIE}=".encode("latin-1")
        already_set = any(
            key == b"set-cookie" and value.startswith(prefix) for key, value in response.headers.raw
        )
        if not already_set:
            issue_csrf(response, request)
    except Exception:  # pragma: no cover - never break a request over a cookie
        pass
    return response


# ---------------------------------------------------------------------------
# Error handling — friendly messages, no internals
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)

    accept = (request.headers.get("accept") or "*/*").lower()
    wants_html = ("text/html" in accept or "*/*" in accept) and not request.url.path.startswith("/api/")
    if not wants_html:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    messages = {
        400: "That request wasn't quite right.",
        403: "You don't have access to that page.",
        404: "We couldn't find that page.",
        405: "That action isn't allowed here.",
        429: "Too many requests. Please wait a moment.",
        500: "Something went wrong. Please try again.",
    }
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "user": getattr(request.state, "user", None),
            "status_code": exc.status_code,
            "message": messages.get(exc.status_code, "Something went wrong. Please try again."),
            "detail": exc.detail if exc.status_code in (400, 403) else None,
        },
        status_code=exc.status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    return await http_exception_handler(request, HTTPException(status_code=exc.status_code, detail=exc.detail))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "user": getattr(request.state, "user", None),
            "status_code": 500,
            "message": "Something went wrong. Please try again.",
            "detail": None,
        },
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Cached thumbnails
# ---------------------------------------------------------------------------
@app.get("/media/thumbs/{filename}")
def serve_thumbnail(filename: str):
    path = thumbnails.thumbnail_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/media/placeholder.svg")
def serve_placeholder(title: str = Query("", max_length=60)):
    """Generated placeholder cover (initials over the category colour)."""
    from .deps import _initials

    initials = _initials(title or "?")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="460" viewBox="0 0 320 460">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#312e81"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="320" height="460" fill="url(#g)"/>
  <text x="160" y="252" font-family="system-ui, -apple-system, Segoe UI, sans-serif" font-size="88"
        font-weight="700" fill="#c7d2fe" text-anchor="middle">{initials[:2]}</text>
  <text x="160" y="300" font-family="system-ui, sans-serif" font-size="16" fill="#818cf8" text-anchor="middle">no cover</text>
</svg>"""
    return HTMLResponse(content=svg, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: Optional[User] = Depends(current_user_optional),
    db: Session = Depends(get_db),
):
    if user is None:
        # First run: send the visitor to registration so they can create an account.
        from sqlalchemy import func

        has_accounts = bool(db.scalar(select(func.count(User.id))))
        return RedirectResponse("/register" if not has_accounts else "/login", status_code=303)

    stats = content_service.get_stats(db, user.id)
    query = LibraryQuery(per_page=(getattr(user, "settings", None).items_per_page if getattr(user, "settings", None) else 24))
    recent, _, _ = content_service.list_content(db, user.id, LibraryQuery(per_page=12, sort="recent_added"))
    updated, _, _ = content_service.list_content(db, user.id, LibraryQuery(per_page=8, sort="recent_updated"))
    pending = content_service.items_with_pending_updates(db, user.id, limit=8)
    broken = [
        item
        for item in db.scalars(
            select(Content)
            .where(
                Content.user_id == user.id,
                Content.deleted_at.is_(None),
                Content.url_status.in_(["broken", "degraded"]),
            )
            .order_by(Content.url_last_checked_at.desc())
            .limit(6)
        ).all()
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        page_context(
            request,
            user,
            db,
            title="Dashboard",
            stats=stats,
            recent=recent,
            recently_updated=updated,
            pending=pending,
            broken=broken,
            activity=recent_activity(db, user.id, limit=10),
            empty=stats["total"] == 0,
        ),
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": settings.app_name, "env": settings.app_env}


# ---------------------------------------------------------------------------
# Middleware wiring (order matters)
#
# Starlette builds the stack in reverse registration order, so what is added
# LAST ends up OUTERMOST:
#
#   security_headers  (pure ASGI — also covers error pages)
#     └─ session_middleware  (resolves the caller's session)
#          └─ attach_csrf_cookie  (ensures the CSRF cookie exists)
#               └─ router → routes, dependencies (incl. csrf_required)
# ---------------------------------------------------------------------------
app.middleware("http")(attach_csrf_cookie)
app.middleware("http")(session_middleware)
app.add_middleware(SecurityHeadersMiddleware)
