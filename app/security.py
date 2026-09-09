"""
Security primitives.

* Argon2id password hashing (memory-hard, current OWASP recommendation).
* Signed, random session tokens stored **hashed** in the DB.
* HMAC-based CSRF tokens (double-submit cookie pattern).
* Password reset tokens (single use, time limited, stored hashed).
* Constant-time comparison helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import settings
from .models import utcnow

# Argon2id with the OWASP-recommended "low memory" profile.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

_PEPPER_ENV = "PCT_PASSWORD_PEPPER"


def _pepper() -> bytes:
    """Optional extra key mixed into the hash input (defence in depth)."""
    import os

    return os.environ.get(_PEPPER_ENV, "").encode("utf-8")


# --- Passwords --------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password.encode("utf-8") + _pepper())


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password.encode("utf-8") + _pepper())
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return True


def password_problems(password: str) -> list[str]:
    """Human-readable password policy failures (empty list == OK)."""
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"Password must be at least {settings.password_min_length} characters.")
    if password.strip() != password:
        problems.append("Password cannot start or end with a space.")
    if len(password) > 256:
        problems.append("Password is too long (max 256 characters).")
    lowered = password.lower()
    if lowered in {"password", "password1", "12345678", "qwerty123", "letmein1", "changeme"}:
        problems.append("That password is too common. Please choose a stronger one.")
    if password.isdigit():
        problems.append("Password cannot be only digits.")
    return problems


# --- Tokens -----------------------------------------------------------------
def generate_token(nbytes: int = 32) -> str:
    """URL-safe random token. The raw value goes to the client; we store a hash."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Deterministic hash for storing/looking up a secret token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- Session cookie ---------------------------------------------------------
def create_session_pair(max_age_days: int) -> Tuple[str, str, str, datetime]:
    """Return (raw_token, token_hash, csrf_secret, expires_at)."""
    raw = generate_token(32)
    expires = utcnow() + timedelta(days=max_age_days)
    return raw, hash_token(raw), generate_token(16), expires


def session_cookie_value(raw_token: str) -> str:
    """
    Cookie payload is ``token.signature`` so a token cannot be forged even if
    the DB row were somehow created.
    """
    sig = hmac.new(settings.secret_key.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256)
    return f"{raw_token}.{sig.hexdigest()}"


def split_session_cookie(value: str) -> Optional[str]:
    """Validate signature and return the raw token, or None if invalid."""
    if not value or "." not in value:
        return None
    raw, _, sig = value.rpartition(".")
    if not raw or not sig:
        return None
    expected = hmac.new(settings.secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    if not constant_time_equals(sig, expected):
        return None
    return raw


# --- CSRF -------------------------------------------------------------------
def csrf_token_for(csrf_secret: str) -> str:
    """Deterministic per-session CSRF token (double-submit cookie pattern)."""
    mac = hmac.new(settings.secret_key.encode("utf-8"), (csrf_secret or "").encode("utf-8"), hashlib.sha256)
    return f"{csrf_secret}.{mac.hexdigest()}"


def csrf_token_valid(csrf_secret: str, presented: str) -> bool:
    if not csrf_secret or not presented:
        return False
    return constant_time_equals(csrf_token_for(csrf_secret), presented)


# --- Password reset ---------------------------------------------------------
RESET_TOKEN_TTL_MINUTES = 30


def create_reset_token() -> Tuple[str, str, datetime]:
    """Return (raw_token, token_hash, expires_at)."""
    raw = generate_token(32)
    expires = utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    return raw, hash_token(raw), expires


def reset_token_valid(expires_at: Optional[datetime]) -> bool:
    if expires_at is None:
        return False
    return expires_at > utcnow()
