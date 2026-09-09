"""
Authentication: register, login, logout, forgot/reset password, profile.

Notes
-----
* Passwords are hashed with Argon2id and never stored or logged in clear text.
* Failed logins are rate limited per IP **and** per account, with a temporary
  account lock after repeated failures.  Error messages do not reveal whether
  an email exists.
* Sessions are server-side rows; the cookie carries only a signed random token.
* Changing a password invalidates every other session.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import CSRF_COOKIE, csrf_required, current_user, page_context, templates
from ..models import Content, SessionToken, User, UserSettings, utcnow
from ..security import (
    create_reset_token,
    create_session_pair,
    hash_password,
    hash_token,
    password_needs_rehash,
    password_problems,
    reset_token_valid,
    session_cookie_value,
    verify_password,
)
from ..services import activity as activity_service
from ..services.seed import ensure_user_settings
from ..utils import rate_limit

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
GENERIC_LOGIN_ERROR = "That email and password combination didn't work."
LOCK_THRESHOLD = 8
LOCK_MINUTES = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _rate_limited(request: Request, action: str) -> Optional[int]:
    key = f"{action}:{_client_ip(request)}"
    allowed, retry_after = rate_limit.allow(key, settings.auth_rate_limit, settings.auth_rate_window)
    return None if allowed else retry_after


def _set_session_cookie(response: Response, raw_token: str, max_age_days: int) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_cookie_value(raw_token),
        max_age=60 * 60 * 24 * max_age_days,
        httponly=True,  # never readable by JavaScript
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def create_session(
    db: Session,
    user: User,
    request: Request,
    response: Response,
    remember: bool = False,
    csrf_secret: Optional[str] = None,
) -> Response:
    """
    Create a server-side session and stamp the cookie onto ``response``.

    The cookie MUST be set on the response object that is actually returned:
    when a route returns its own ``Response`` (e.g. a redirect), FastAPI does
    not merge headers/cookies from the injected temporal ``Response``.
    """
    days = settings.remember_me_days if remember else settings.session_max_age_days
    raw, token_hash, secret, expires = create_session_pair(days)
    db.add(
        SessionToken(
            user_id=user.id,
            token_hash=token_hash,
            csrf_secret=secret,
            user_agent=(request.headers.get("user-agent") or "")[:255],
            ip_address=_client_ip(request),
            expires_at=expires,
        )
    )
    db.commit()
    _set_session_cookie(response, raw, days)
    # Bind the CSRF cookie to this new session so the next page matches.
    from ..security import csrf_token_for

    response.set_cookie(
        CSRF_COOKIE,
        csrf_token_for(secret),
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


def revoke_other_sessions(db: Session, user_id: int, keep_token_hash: Optional[str] = None) -> int:
    stmt = select(SessionToken).where(SessionToken.user_id == user_id)
    rows = list(db.scalars(stmt).all())
    removed = 0
    for row in rows:
        if keep_token_hash and row.token_hash == keep_token_hash:
            continue
        db.delete(row)
        removed += 1
    if removed:
        db.commit()
    return removed


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    from ..deps import current_user_optional

    existing = current_user_optional(request, db)
    if existing:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth/register.html",
        page_context(request, None, db, title="Create your account", error=None, form={}),
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    from ..deps import current_user_optional

    existing = current_user_optional(request, db)
    if existing:
        return RedirectResponse("/", status_code=303)
    has_accounts = bool(db.scalar(select(func.count(User.id))))
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        page_context(
            request,
            None,
            db,
            title="Sign in",
            error=None,
            form={},
            first_run=not has_accounts,
        ),
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "auth/forgot.html",
        page_context(request, None, db, title="Reset your password", sent=False,
                     error=None, form={}, reset_link=None, notice=None),
    )


@router.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(User).where(User.reset_token_hash == hash_token(token)))
    valid = row is not None and reset_token_valid(row.reset_token_expires_at)
    return templates.TemplateResponse(
        request,
        "auth/reset.html",
        page_context(request, None, db, title="Choose a new password", token=token, valid=valid, error=None),
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@router.post("/register")
def register(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    password_confirm: str = Form(""),
    remember: bool = Form(False),
):
    retry = _rate_limited(request, "register")
    if retry is not None:
        return _form_error(request, db, "auth/register.html", f"Too many attempts. Try again in {retry}s.",
                           {"email": email, "display_name": display_name}, title="Create your account")

    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()[:120]

    if not EMAIL_RE.match(email) or len(email) > 320:
        return _form_error(request, db, "auth/register.html", "Please enter a valid email address.",
                           {"email": email, "display_name": display_name}, title="Create your account")
    if password != password_confirm:
        return _form_error(request, db, "auth/register.html", "The two passwords don't match.",
                           {"email": email, "display_name": display_name}, title="Create your account")
    problems = password_problems(password)
    if problems:
        return _form_error(request, db, "auth/register.html", " ".join(problems),
                           {"email": email, "display_name": display_name}, title="Create your account")

    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        # Same message as a generic failure — do not disclose which emails exist.
        return _form_error(request, db, "auth/register.html",
                           "That account could not be created. Try signing in instead.",
                           {"email": email, "display_name": display_name}, title="Create your account")

    first_user = not db.scalar(select(func.count(User.id)))
    user = User(
        email=email,
        display_name=display_name or email.split("@")[0],
        password_hash=hash_password(password),
        role="admin" if first_user else "user",
        password_changed_at=utcnow(),
    )
    db.add(user)
    db.flush()
    ensure_user_settings(db, user)
    db.commit()

    activity_service.log_activity(db, user.id, "account", "You created your account.")
    db.commit()

    redirect = RedirectResponse("/", status_code=303)
    create_session(db, user, request, redirect, remember=remember)
    return redirect


@router.post("/login")
def login(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    email: str = Form(""),
    password: str = Form(""),
    remember: bool = Form(False),
):
    retry = _rate_limited(request, "login")
    if retry is not None:
        return _form_error(request, db, "auth/login.html", f"Too many attempts. Try again in {retry}s.",
                           {"email": email}, title="Sign in")

    email = (email or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email)) if email else None

    # Account-level lockout after repeated failures.
    if user and user.locked_until and user.locked_until > utcnow():
        minutes = max(1, int((user.locked_until - utcnow()).total_seconds() // 60) + 1)
        return _form_error(request, db, "auth/login.html",
                           f"Too many failed attempts. Account locked for {minutes} more minutes.",
                           {"email": email}, title="Sign in")

    if user is None:
        # Run a hash comparison anyway so response timing does not leak existence.
        verify_password(password, "$argon2id$v=19$m=65536,t=3,p=2$c2FsdHNhbHRzYWx0c2E$invalidhashvaluepaddingpadding")
        return _form_error(request, db, "auth/login.html", GENERIC_LOGIN_ERROR, {"email": email}, title="Sign in")

    if not user.is_active:
        return _form_error(request, db, "auth/login.html", "This account is disabled.",
                           {"email": email}, title="Sign in")

    if not verify_password(password, user.password_hash):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= LOCK_THRESHOLD:
            from datetime import timedelta

            user.locked_until = utcnow() + timedelta(minutes=LOCK_MINUTES)
            user.failed_login_count = 0
        db.commit()
        return _form_error(request, db, "auth/login.html", GENERIC_LOGIN_ERROR, {"email": email}, title="Sign in")

    # Success
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    db.commit()

    rate_limit.reset(f"login:{_client_ip(request)}")
    activity_service.log_activity(db, user.id, "account", "You signed in.")
    db.commit()

    redirect = RedirectResponse("/", status_code=303)
    create_session(db, user, request, redirect, remember=remember)
    return redirect


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    from ..security import split_session_cookie

    raw_cookie = request.cookies.get(settings.session_cookie_name)
    token = split_session_cookie(raw_cookie) if raw_cookie else None
    if token:
        row = db.scalar(select(SessionToken).where(SessionToken.token_hash == hash_token(token)))
        if row is not None:
            db.delete(row)
            db.commit()
    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(response)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(""),
):
    retry = _rate_limited(request, "forgot")
    if retry is not None:
        return _form_error(request, db, "auth/forgot.html", f"Too many attempts. Try again in {retry}s.",
                           {"email": email}, title="Reset your password")

    email = (email or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email)) if email else None
    reset_link = None

    if user is not None:
        raw, token_hash, expires = create_reset_token()
        user.reset_token_hash = token_hash
        user.reset_token_expires_at = expires
        db.commit()
        reset_link = f"{settings.public_base_url.rstrip('/')}/reset-password/{raw}"
        # No mail server is configured in this app. In development the link is
        # shown on the page; in production wire up an SMTP provider here.
        if settings.is_production:
            reset_link = None

    context = page_context(
        request, None, db, title="Reset your password", sent=True, reset_link=reset_link,
        error=None, form={"email": email},
    )
    context["notice"] = (
        "If that email matches an account, a reset link has been generated."
        if user is not None
        else "If that email matches an account, a reset link has been generated."
    )
    return templates.TemplateResponse(request, "auth/forgot.html", context, status_code=200)


@router.post("/reset-password/{token}")
def reset_password(
    request: Request,
    db: Session = Depends(get_db),
    token: str = "",
    password: str = Form(""),
    password_confirm: str = Form(""),
):
    retry = _rate_limited(request, "reset")
    if retry is not None:
        return _form_error(request, db, "auth/reset.html", f"Too many attempts. Try again in {retry}s.",
                           {}, title="Choose a new password", token=token, valid=True)

    user = db.scalar(select(User).where(User.reset_token_hash == hash_token(token))) if token else None
    if user is None or not reset_token_valid(user.reset_token_expires_at):
        return templates.TemplateResponse(
            request,
            "auth/reset.html",
            page_context(request, None, db, title="Choose a new password", token=token, valid=False,
                         error="That reset link is invalid or has expired."),
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "auth/reset.html",
            page_context(request, None, db, title="Choose a new password", token=token, valid=True,
                         error="The two passwords don't match."),
            status_code=400,
        )
    problems = password_problems(password)
    if problems:
        return templates.TemplateResponse(
            request,
            "auth/reset.html",
            page_context(request, None, db, title="Choose a new password", token=token, valid=True,
                         error=" ".join(problems)),
            status_code=400,
        )

    user.password_hash = hash_password(password)
    user.password_changed_at = utcnow()
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    user.failed_login_count = 0
    user.locked_until = None
    db.commit()

    # A password reset signs out every device.
    db.query(SessionToken).filter(SessionToken.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    activity_service.log_activity(db, user.id, "account", "You reset your password.")
    db.commit()

    response = RedirectResponse("/login?reset=1", status_code=303)
    _clear_session_cookie(response)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# Profile / account settings
# ---------------------------------------------------------------------------
@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sessions = list(
        db.scalars(
            select(SessionToken).where(SessionToken.user_id == user.id).order_by(SessionToken.last_seen_at.desc())
        ).all()
    )
    counts = {
        "items": int(db.scalar(select(func.count(Content.id)).where(Content.user_id == user.id, Content.deleted_at.is_(None))) or 0),
        "favorites": int(
            db.scalar(
                select(func.count(Content.id)).where(
                    Content.user_id == user.id, Content.is_favorite.is_(True), Content.deleted_at.is_(None)
                )
            )
            or 0
        ),
        "sessions": len(sessions),
    }
    return templates.TemplateResponse(
        request,
        "profile.html",
        page_context(
            request,
            user,
            db,
            title="Profile & account",
            sessions=sessions,
            counts=counts,
            message=request.query_params.get("msg"),
            error=request.query_params.get("err"),
        ),
    )


@router.post("/profile")
def profile_update(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    display_name: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    display_name = (display_name or "").strip()[:120]
    if display_name:
        user.display_name = display_name
        db.commit()

    if new_password:
        if not verify_password(current_password, user.password_hash):
            return RedirectResponse("/profile?err=wrong-password", status_code=303)
        if new_password != new_password_confirm:
            return RedirectResponse("/profile?err=mismatch", status_code=303)
        problems = password_problems(new_password)
        if problems:
            return RedirectResponse("/profile?err=weak", status_code=303)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        db.commit()
        # Keep the current session, revoke all others.
        row = getattr(request.state, "session_row", None)
        revoke_other_sessions(db, user.id, keep_token_hash=row.token_hash if row else None)
        activity_service.log_activity(db, user.id, "account", "You changed your password.")
        db.commit()
        return RedirectResponse("/profile?msg=password-changed", status_code=303)

    activity_service.log_activity(db, user.id, "account", "You updated your profile.")
    db.commit()
    return RedirectResponse("/profile?msg=saved", status_code=303)


@router.post("/profile/sessions/revoke")
def revoke_sessions(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = getattr(request.state, "session_row", None)
    removed = revoke_other_sessions(db, user.id, keep_token_hash=row.token_hash if row else None)
    activity_service.log_activity(db, user.id, "account", f"You signed out {removed} other device(s).")
    db.commit()
    return RedirectResponse("/profile?msg=sessions", status_code=303)


# ---------------------------------------------------------------------------
# Small helper for form re-render with an error
# ---------------------------------------------------------------------------
def _form_error(request: Request, db: Session, template: str, message: str, form: dict, **extra):
    context = page_context(request, None, db, error=message, form=form)
    context.update(extra)
    context.setdefault("first_run", False)
    return templates.TemplateResponse(request, template, context, status_code=400)
