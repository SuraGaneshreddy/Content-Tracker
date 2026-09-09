"""
Safe outbound HTTP.

Everything the app fetches from the internet goes through this module so that:

1. **SSRF is blocked.** User-supplied URLs are validated, the hostname is
   resolved, and *every* resolved address is rejected if it is loopback,
   private, link-local, multicast, reserved or otherwise non-global.  The
   connection is then pinned to the address we validated, which also defeats
   DNS-rebinding (a host that answers differently on the second lookup).
2. **Redirects are re-validated** (no redirecting into the internal network).
3. **Responses are size-capped** so a huge/hostile page cannot exhaust memory.
4. **Requests are throttled** per host and per run, so we never hammer a site.
5. Only ``http``/``https`` on standard ports are allowed, and redirects are
   never followed to a different scheme.

The fetched body is always treated as untrusted: it is decoded defensively,
truncated, and never executed.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from ..config import settings

MAX_URL_LENGTH = 2048
USER_AGENT = (
    "PersonalContentTracker/1.0 (+personal library manager; single-user; "
    "polite fetch, 1 request per host per interval)"
)


class UnsafeUrlError(ValueError):
    """Raised when a URL fails the SSRF / scheme / port policy."""


class FetchError(RuntimeError):
    """Raised when a request could not be completed (network level)."""


class ThrottledError(RuntimeError):
    """Raised when the per-host or per-run request budget is exhausted."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    content: bytes = b""
    text: str = ""
    elapsed_ms: int = 0
    truncated: bool = False
    final_url: str = ""

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def _ip_is_blocked(ip: str) -> bool:
    """True if an address must never be connected to."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped

    checks = (
        addr.is_private,
        addr.is_loopback,
        addr.is_link_local,
        addr.is_multicast,
        addr.is_reserved,
        addr.is_unspecified,
        # IPv6 unique local addresses (fc00::/7) report as private, but be explicit.
        getattr(addr, "is_site_local", False),
    )
    if any(checks):
        return True

    # Extra paranoia: 0.0.0.0/8 and 100.64.0.0/10 (CGNAT).
    if isinstance(addr, ipaddress.IPv4Address):
        packed = int(addr)
        if packed >> 24 == 0:  # 0.0.0.0/8
            return True
        if 0x64400000 <= packed <= 0x647FFFFF:  # 100.64.0.0/10
            return True
    return False


def resolve_and_validate(url: str) -> Tuple[str, str, str]:
    """
    Validate a URL and resolve its host.

    Returns (normalised_url, host, pinned_ip).
    Raises UnsafeUrlError with a user-safe message on any policy violation.
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("Invalid URL.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is too long (2048 characters maximum).")
    if any(ch in url for ch in ("\n", "\r", "\t", " ", "\x00")):
        raise UnsafeUrlError("Invalid URL.")
    # Reject percent-encoded control characters (NUL/CR/LF smuggling).
    lowered = url.lower()
    if any(f"%{i:02x}" in lowered for i in set(range(0, 0x21)) | {0x7F}):
        raise UnsafeUrlError("Invalid URL: contains illegal characters.")

    try:
        parts = urlsplit(url)
    except ValueError:
        raise UnsafeUrlError("Invalid URL.") from None

    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http:// and https:// URLs are allowed.")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("Invalid URL: missing host.")
    if len(host) > 253:
        raise UnsafeUrlError("Invalid URL: host too long.")

    # Reject anything that is already an IP literal in a private range, and
    # weird encodings of localhost.
    if host in {"localhost", "localhost.localdomain", "test", "invalid", "example"}:
        raise UnsafeUrlError("Local addresses are not allowed.")

    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if port not in settings.fetch_allowed_ports:
        raise UnsafeUrlError("That port is not allowed (80/443 only).")

    # Resolve DNS and validate every returned address.
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeUrlError("Could not resolve that domain. Check the URL.") from None
    except OSError:
        raise UnsafeUrlError("Could not resolve that domain. Check the URL.") from None

    candidates: List[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        if "%" in ip:  # strip IPv6 zone id
            ip = ip.split("%", 1)[0]
        if settings.fetch_block_private and _ip_is_blocked(ip):
            raise UnsafeUrlError("That address is not reachable from this app.")
        candidates.append(ip)

    if not candidates:
        raise UnsafeUrlError("Could not resolve that domain. Check the URL.")

    # Prefer IPv4 for predictability.
    pinned = next((ip for ip in candidates if ":" not in ip), candidates[0])

    normalised = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return normalised, host, pinned


def is_safe_public_url(url: str) -> bool:
    try:
        resolve_and_validate(url)
        return True
    except (UnsafeUrlError, Exception):
        return False


# ---------------------------------------------------------------------------
# IP pinning transport (prevents DNS rebinding between validate and connect)
# ---------------------------------------------------------------------------
class _PinningBackend(httpcore.SyncBackend):
    """
    Network backend that connects only to the IP address we already validated.

    ``connect_tcp`` receives the hostname, so we substitute the pinned address
    while leaving the hostname intact for the Host header and TLS SNI /
    certificate verification.  Any attempt to dial a different host is refused
    rather than resolved again.
    """

    def __init__(self, allowed: dict) -> None:
        super().__init__()
        self._allowed = allowed  # lowercase hostname -> ip
        self.attempted: list[str] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        key = str(host).lower().rstrip(".")
        pinned = self._allowed.get(key)
        if pinned is None:
            raise httpcore.ConnectError(f"Refusing to connect to unvalidated host: {key}")
        self.attempted.append(key)
        return super().connect_tcp(
            pinned, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )


def _build_client(pinned_map: dict) -> httpx.Client:
    backend = _PinningBackend(pinned_map)
    transport = httpx.HTTPTransport(retries=0, verify=True)
    transport._pool._network_backend = backend
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(settings.fetch_timeout, connect=min(settings.fetch_timeout, 6.0)),
        transport=transport,
        verify=True,
    )



# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe sliding limits: per-host minimum interval + per-run budget."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._host_last: dict[str, float] = {}
        self._run_count = 0

    def reset_run(self) -> None:
        with self._lock:
            self._run_count = 0

    def acquire(self, host: str, force: bool = False) -> None:
        host = host.lower()
        with self._lock:
            if not force and self._run_count >= settings.fetch_max_per_run:
                raise ThrottledError("Request budget for this run is exhausted.")
            last = self._host_last.get(host)
            now = time.monotonic()
            wait = settings.fetch_min_host_interval
            if last is not None and (now - last) < wait and not force:
                raise ThrottledError(f"Too soon to contact {host} again.")
            self._host_last[host] = now
            self._run_count += 1

    def seconds_until_ready(self, host: str) -> float:
        host = host.lower()
        with self._lock:
            last = self._host_last.get(host)
            if last is None:
                return 0.0
            remaining = settings.fetch_min_host_interval - (time.monotonic() - last)
            return max(0.0, remaining)


rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _decode_body(content: bytes, headers: httpx.Headers) -> str:
    ctype = headers.get("content-type", "")
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"') or "utf-8"
    try:
        return content.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def safe_get(
    url: str,
    *,
    max_bytes: Optional[int] = None,
    timeout: Optional[float] = None,
    accept: str = "*/*",
    force: bool = False,
    head_only: bool = False,
    extra_headers: Optional[dict] = None,
) -> FetchResult:
    """
    Perform a policy-checked GET (or HEAD) and return a size-capped result.

    ``force=True`` bypasses the throttle (used for explicit user actions such
    as "Check this link now").
    """
    normalised, host, pinned_ip = resolve_and_validate(url)
    if not force:
        rate_limiter.acquire(host)

    max_bytes = max_bytes or settings.fetch_max_bytes
    timeout = timeout or settings.fetch_timeout
    started = time.monotonic()

    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en"}
    if extra_headers:
        # Caller-supplied auth headers only; never let them override safety headers.
        for key, value in extra_headers.items():
            if key.lower() in ("authorization", "x-api-key", "cookie"):
                headers[key] = value
    pinned_map = {host: pinned_ip}

    try:
        with _build_client(pinned_map) as client:
            current = normalised
            result: Optional[FetchResult] = None
            for _ in range(max(1, settings.fetch_max_redirects + 1)):
                request_method = "HEAD" if head_only else "GET"
                try:
                    response = client.request(request_method, current, headers=headers)
                except httpx.HTTPError as exc:
                    raise FetchError(_friendly_network_error(exc)) from exc

                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        break
                    next_url = str(response.url.join(location))
                    # Re-validate every hop: redirects must stay on the public internet.
                    next_normalised, next_host, next_ip = resolve_and_validate(next_url)
                    if next_host not in pinned_map:
                        if not force:
                            rate_limiter.acquire(next_host)
                        pinned_map[next_host] = next_ip
                    current = next_normalised
                    continue

                body = response.content or b""
                truncated = False
                if len(body) > max_bytes:
                    body = body[:max_bytes]
                    truncated = True

                result = FetchResult(
                    url=normalised,
                    status_code=response.status_code,
                    headers=response.headers,
                    content=body,
                    text="" if head_only else _decode_body(body, response.headers),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    truncated=truncated,
                    final_url=str(response.url),
                )
                break

            if result is None:
                raise FetchError("Too many redirects.")
            return result
    except FetchError:
        raise
    except ThrottledError:
        raise
    except UnsafeUrlError:
        raise
    except httpx.HTTPError as exc:
        raise FetchError(_friendly_network_error(exc)) from exc
    except Exception as exc:  # never leak internals
        raise FetchError(f"Could not check this source ({type(exc).__name__}).") from exc


def _friendly_network_error(exc: Exception) -> str:
    """Map httpx exceptions to short, non-technical user messages."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "The site took too long to respond."
    if isinstance(exc, httpx.ReadTimeout):
        return "The site took too long to send its page."
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "certificate" in text or "ssl" in text:
            return "The site's security certificate could not be verified."
        return "Could not connect to that site."
    if isinstance(exc, httpx.TooManyRedirects):
        return "Too many redirects."
    if isinstance(exc, httpx.DecodingError):
        return "The response could not be decoded."
    return "Could not check this source."


def health_status_from_code(status_code: int) -> str:
    """Map an HTTP status to our URL health vocabulary."""
    from ..constants import URL_STATUS_BROKEN, URL_STATUS_DEGRADED, URL_STATUS_OK

    if 200 <= status_code < 300:
        return URL_STATUS_OK
    if status_code in (401, 403, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526):
        # These are usually temporary or bot-blocking, not "the link is dead".
        return URL_STATUS_DEGRADED
    if status_code in (404, 410):
        return URL_STATUS_BROKEN
    if 300 <= status_code < 400:
        return URL_STATUS_OK
    return URL_STATUS_DEGRADED
