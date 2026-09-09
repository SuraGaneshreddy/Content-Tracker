"""
FastAPI dependencies: session resolution, authorisation, CSRF, templating.

Session model
-------------
The cookie holds ``<random-token>.<hmac-signature>``.  We verify the signature,
hash the token and look the row up.  The row carries its own expiry plus a
rolling idle timeout, so a stolen cookie stops working when it is unused and
can be revoked instantly by deleting the row (logout / password change).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .constants import STATUS_COLORS, STATUS_LABELS, URL_STATUS_ICONS, URL_STATUS_LABELS
from .database import get_db
from .models import Category, Content, SessionToken, User, utcnow
from .security import csrf_token_for, csrf_token_valid, split_session_cookie
from .services import activity as activity_service
from .services.content_service import progress_label
from .services.seed import ensure_user_settings

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Jinja helpers
# ---------------------------------------------------------------------------
def _relative_time(value) -> str:
    if value is None:
        return "—"
    now = utcnow()
    try:
        delta = now - value
    except TypeError:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        return f"{days // 7} wk ago"
    if days < 365:
        return f"{days // 30} mo ago"
    return f"{days // 365} yr ago"


def _initials(title: str) -> str:
    words = [w for w in (title or "").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _status_color(slug: str) -> str:
    return STATUS_COLORS.get(slug, "#64748b")


def _status_label(slug: str) -> str:
    return STATUS_LABELS.get(slug, slug.replace("_", " ").title())


def _rating_stars(rating) -> str:
    try:
        value = int(rating)
    except (TypeError, ValueError):
        return ""
    return "★" * value + "☆" * (10 - value)


def _truncate(value, limit: int = 140) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


templates.env.filters["relativetime"] = _relative_time
templates.env.filters["initials"] = _initials
templates.env.filters["statuscolor"] = _status_color
templates.env.filters["statuslabel"] = _status_label
templates.env.filters["stars"] = _rating_stars
templates.env.filters["truncate_text"] = _truncate
templates.env.filters["progress_label"] = progress_label
templates.env.globals["url_status_icons"] = URL_STATUS_ICONS
templates.env.globals["url_status_labels"] = URL_STATUS_LABELS
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["app_env"] = settings.app_env


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def _resolve_session(request: Request, db: Session) -> Optional[User]:
    """
    Validate the session cookie and attach the session row + user to the request.

    Safe to call more than once per request: the result is cached on
    ``request.state`` so the middleware and the route dependency agree on the
    same session (and therefore the same CSRF secret).
    """
    if getattr(request.state, "session_resolved", False):
        return getattr(request.state, "user_id", None)
    request.state.session_resolved = True
    request.state.session_row = None
    request.state.user_id = None

    raw_cookie = request.cookies.get(settings.session_cookie_name)
    if not raw_cookie:
        return None
    token = split_session_cookie(raw_cookie)
    if token is None:
        return None

    from .security import hash_token

    row = db.scalar(select(SessionToken).where(SessionToken.token_hash == hash_token(token)))
    if row is None:
        return None

    now = utcnow()
    if row.expires_at and row.expires_at < now:
        db.delete(row)
        db.commit()
        return None

    idle_limit = now - timedelta(minutes=settings.session_idle_minutes)
    if row.last_seen_at and row.last_seen_at < idle_limit:
        db.delete(row)
        db.commit()
        return None

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    # Rolling session: refresh activity, extend expiry (cheap UPDATE).
    row.last_seen_at = now
    row.expires_at = max(row.expires_at, now + timedelta(minutes=min(settings.session_idle_minutes, 60 * 24)))
    db.commit()

    # Store the id, not the ORM object: the middleware's session closes
    # immediately, and a detached instance would break lazy loads in templates.
    request.state.session_row = row
    request.state.user_id = user.id
    return user.id


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Return a session-attached user for the current request (or None)."""
    user_id = _resolve_session(request, db)
    if user_id is None:
        return None
    return db.get(User, user_id)


def resolve_session_state(request: Request) -> None:
    """Open a short-lived DB session purely to resolve the caller's session."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        _resolve_session(request, db)
    finally:
        db.close()


def current_user(user: Optional[User] = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"}
        )
    return user


def current_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required.")
    return user


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
CSRF_COOKIE = "pct_csrf"
ANONYMOUS_CSRF_SECRET = "anonymous"


def issue_csrf(response: Response, request: Request) -> str:
    """Attach (or reuse) the CSRF cookie and return the expected form token."""
    row = getattr(request.state, "session_row", None)
    secret = row.csrf_secret if row is not None else ANONYMOUS_CSRF_SECRET
    token = csrf_token_for(secret)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=60 * 60 * 24 * 365,
        httponly=False,  # JS must be able to read it for fetch() calls
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return token


# Endpoints exempt from the CSRF check.  Logout is idempotent and must always
# be reachable so a user can escape a broken/expired session.
CSRF_EXEMPT_PATHS = {"/logout"}


def csrf_is_valid(request: Request, token: Optional[str]) -> bool:
    """
    Double-submit CSRF check.

    The token is bound to the server-side session secret, so an attacker who
    can set the cookie on a victim's browser still cannot produce a value that
    matches the victim's session.
    """
    row = getattr(request.state, "session_row", None)
    secret = row.csrf_secret if row is not None else ANONYMOUS_CSRF_SECRET
    presented = token or request.headers.get("X-CSRF-Token") or ""
    return csrf_token_valid(secret, presented)


async def session_middleware(request: Request, call_next):
    """
    Resolve the caller's session before routing.

    Doing it here (rather than only in the route dependency) means the CSRF
    check and the rendered pages both see the same session, and therefore the
    same CSRF secret.  The request body is never touched, so route handlers can
    still parse their own form data.
    """
    try:
        resolve_session_state(request)
    except Exception:  # pragma: no cover - never block a request on a DB hiccup
        pass
    return await call_next(request)


async def csrf_required(request: Request) -> None:
    """
    Router-level CSRF gate for every state-changing request.

    Implemented as a dependency (not middleware) so it shares the route's
    ``Request`` object: ``await request.form()`` caches the parsed body, which
    the route's own ``Form(...)`` parameters then reuse.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    if request.url.path in CSRF_EXEMPT_PATHS:
        return

    token: Optional[str] = request.headers.get("X-CSRF-Token")
    if not token:
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            try:
                form = await request.form()
                token = form.get("csrf_token")
            except Exception:
                token = None

    if not csrf_is_valid(request, token):
        raise HTTPException(status_code=403, detail="Your session expired. Please refresh the page and try again.")


# ---------------------------------------------------------------------------
# View context
# ---------------------------------------------------------------------------
def base_context(request: Request, user: Optional[User], db: Session) -> dict:
    """Common template context: nav, theme, unread badge."""
    theme = "dark"
    settings_row = None
    unread = 0
    pending = 0
    if user is not None:
        settings_row = ensure_user_settings(db, user)
        theme = settings_row.theme
        unread = activity_service.unread_count(db, user.id)
        from sqlalchemy import func

        pending = int(
            db.scalar(
                select(func.count(Content.id)).where(
                    Content.user_id == user.id,
                    Content.deleted_at.is_(None),
                    Content.update_available.is_(True),
                    Content.update_state == "unread",
                )
            )
            or 0
        )

    categories = list(
        db.scalars(select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)).all()
    )
    active_path = request.url.path

    session_row = getattr(request.state, "session_row", None)
    return {
        "request": request,
        "user": user,
        "settings": settings_row,
        # `prefs` is the name the templates use; expose it on every page so the
        # remembered view mode and theme are applied before any JS runs.
        "prefs": settings_row,
        "theme": theme,
        "categories": categories,
        "unread_count": unread,
        "pending_updates": pending,
        "active_path": active_path,
        "csrf_token": csrf_token_for(
            session_row.csrf_secret if session_row is not None else ANONYMOUS_CSRF_SECRET
        ),
        "year": utcnow().year,
    }


def page_context(request: Request, user: Optional[User], db: Session, **extra) -> dict:
    context = base_context(request, user, db)
    context.update(extra)
    return context
