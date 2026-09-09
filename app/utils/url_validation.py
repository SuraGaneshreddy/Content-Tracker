"""
URL validation helpers for user-supplied links.

Two levels:

* :func:`validate_url_syntax` — structural validation used when saving.
  It never touches the network, so a user can save a link even if the site is
  down right now.  It still refuses internal/metadata addresses and anything
  that is not http(s).
* :func:`resolve_and_validate` (in ``app.utils.safe_http``) — the full policy
  check plus DNS resolution, used immediately before the app makes a request.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

FORBIDDEN_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "test",
    "invalid",
    "example",
    "onion",
}

ILLEGAL_CHARS = re.compile(r"[\x00-\x1f\x7f\s]")
LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class UrlValidationError(ValueError):
    """Raised with a message that is safe to show the user."""


def validate_url_syntax(url: Optional[str]) -> str:
    """
    Validate a URL structurally and return the normalised value.

    Raises :class:`UrlValidationError` with a user-facing message.
    """
    if url is None or not str(url).strip():
        raise UrlValidationError("URL is required.")

    value = str(url).strip()

    # Friendly: add the scheme when the user pasted "example.com/page".
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = "https://" + value

    if len(value) > MAX_URL_LENGTH:
        raise UrlValidationError("URL is too long (2048 characters maximum).")
    if ILLEGAL_CHARS.search(value):
        raise UrlValidationError("Invalid URL.")
    lowered = value.lower()
    if any(f"%{i:02x}" in lowered for i in set(range(0, 0x21)) | {0x7F}):
        raise UrlValidationError("Invalid URL: contains illegal characters.")

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise UrlValidationError("Invalid URL.") from exc

    if parts.scheme not in ("http", "https"):
        raise UrlValidationError("Only http:// and https:// links are supported.")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UrlValidationError("Invalid URL: missing domain.")
    if len(host) > 253:
        raise UrlValidationError("Invalid URL: domain too long.")

    # Port policy
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlValidationError("Invalid URL: bad port.") from exc
    if port is not None and port not in (80, 443):
        raise UrlValidationError("Only standard web ports (80/443) are allowed.")

    # Literal IP addresses must be public.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise UrlValidationError("Links to local or internal addresses are not allowed.")
        if isinstance(addr, ipaddress.IPv4Address):
            packed = int(addr)
            if packed >> 24 == 0 or 0x64400000 <= packed <= 0x647FFFFF:
                raise UrlValidationError("Links to local or internal addresses are not allowed.")
    else:
        if host in FORBIDDEN_HOSTS:
            raise UrlValidationError("Links to local or internal addresses are not allowed.")
        labels = host.split(".")
        if len(labels) < 2:
            raise UrlValidationError("Invalid URL: that doesn't look like a public domain.")
        for label in labels:
            if not label or len(label) > 63 or not LABEL_RE.match(label):
                raise UrlValidationError("Invalid URL: bad domain name.")
        if not re.search(r"[A-Za-z]", labels[-1]):
            raise UrlValidationError("Invalid URL: bad domain name.")

    # Rebuild a clean absolute URL.
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    normalised = f"{parts.scheme}://{netloc}{path}"
    if parts.query:
        normalised += f"?{parts.query}"
    return normalised[:MAX_URL_LENGTH]


def display_host(url: str) -> str:
    """Human-friendly host for the UI (never raises)."""
    try:
        host = urlsplit(url).hostname or ""
        return host.lower().removeprefix("www.") or url[:40]
    except ValueError:
        return url[:40]


def looks_like_series_page(url: str) -> bool:
    """Heuristic: does this URL look like a series/chapter page?"""
    lowered = (url or "").lower()
    return any(
        token in lowered
        for token in ("chapter", "chap-", "/manga/", "/manhwa/", "/manhua/", "episode", "/anime/", "-ep-")
    )
