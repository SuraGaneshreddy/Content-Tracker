"""
Cover image handling.

Remote covers are downloaded through the SSRF-safe fetcher, resized, converted
to WebP/JPEG and stored on disk under ``data/thumbs``.  Benefits:

* the user's browser never hotlinks a third-party image (privacy + no
  referer leaks to sites they simply bookmarked),
* images keep working if the origin blocks hotlinking or disappears,
* we do not re-download the same cover repeatedly.

If downloading fails we store nothing and the UI falls back to a generated
placeholder (see ``static/img/placeholder.svg`` / the ``initials`` filter).
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Optional

from ..config import settings
from ..utils.safe_http import FetchError, ThrottledError, UnsafeUrlError, safe_get
from .metadata import clean_url

try:  # Pillow is optional at runtime; without it we simply skip caching.
    from PIL import Image, UnidentifiedImageError

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}


def _safe_filename(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.webp" if PIL_AVAILABLE else f"{digest}.img"


def thumbnail_path(filename: str) -> Optional[Path]:
    """Resolve a stored thumbnail name to a path, refusing traversal."""
    if not filename:
        return None
    clean = SAFE_NAME_RE.sub("", Path(filename).name)
    if not clean or clean in {".", ".."}:
        return None
    path = settings.thumb_dir / clean
    try:
        path.resolve().relative_to(settings.thumb_dir.resolve())
    except ValueError:
        return None
    return path if path.exists() else None


def download_and_cache(url: Optional[str]) -> Optional[str]:
    """
    Fetch a cover image and store a resized copy.

    Returns the stored filename (relative to the thumbs dir) or None.
    """
    if not url or not settings.enable_thumbnail_cache:
        return None
    safe_url = clean_url(url)
    if not safe_url:
        return None

    # Already cached?
    name = _safe_filename(safe_url)
    if (settings.thumb_dir / name).exists():
        return name

    try:
        result = safe_get(
            safe_url,
            accept="image/*,*/*;q=0.8",
            force=True,
            max_bytes=MAX_IMAGE_BYTES,
        )
    except (UnsafeUrlError, FetchError, ThrottledError):
        return None

    if result.status_code >= 400 or not result.content:
        return None
    if result.content_type and result.content_type not in IMAGE_CONTENT_TYPES and "image" not in result.content_type:
        return None

    if not PIL_AVAILABLE:
        # No Pillow: store the raw bytes with a content-derived extension.
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(result.content_type, ".jpg")
        name = _safe_filename(safe_url).rsplit(".", 1)[0] + ext
        (settings.thumb_dir / name).write_bytes(result.content)
        return name

    try:
        with Image.open(io.BytesIO(result.content)) as image:
            image.load()
            # Guard against decompression bombs: Pillow raises on huge images.
            if image.width * image.height > 40_000_000:
                return None
            image = image.convert("RGB")
            image.thumbnail((settings.thumb_width * 2, settings.thumb_height * 2), Image.LANCZOS)
            target = settings.thumb_dir / name
            image.save(target, "WEBP", quality=82, method=4)
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    return name if (settings.thumb_dir / name).exists() else None


def remove_thumbnail(filename: Optional[str]) -> None:
    path = thumbnail_path(filename) if filename else None
    if path is None and filename:
        candidate = settings.thumb_dir / SAFE_NAME_RE.sub("", Path(filename).name)
        path = candidate if candidate.exists() else None
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def cache_stats() -> dict:
    files = list(settings.thumb_dir.glob("*")) if settings.thumb_dir.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    return {"count": len(files), "bytes": total, "mb": round(total / 1024 / 1024, 2)}


def clear_cache() -> int:
    removed = 0
    if settings.thumb_dir.exists():
        for path in settings.thumb_dir.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
    return removed
