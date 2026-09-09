"""
Import / export.

Export formats: JSON (lossless, the backup format) and CSV (flat, spreadsheet
friendly).  Import accepts either, validates every row before inserting, and
reports per-row results so a partially broken file still imports the good rows.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import CATEGORIES, STATUSES, STATUS_LABELS
from ..models import Content, ContentTag, Favorite, Tag, UpdateHistory, UrlHistory, User, UserSettings, utcnow
from ..utils.url_validation import UrlValidationError, validate_url_syntax
from .content_service import (
    ContentError,
    ContentPayload,
    clean_rating,
    clean_status,
    clean_title,
    create_content,
    get_category,
    normalize_tags,
)

EXPORT_VERSION = 1

CSV_COLUMNS = [
    "title",
    "url",
    "category",
    "status",
    "chapter",
    "episode",
    "season",
    "volume",
    "progress_text",
    "progress_percent",
    "rating",
    "tags",
    "notes",
    "description",
    "cover_image_url",
    "is_favorite",
    "created_at",
]


@dataclass
class ImportReport:
    total: int = 0
    imported: int = 0
    skipped: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors[:50],
            "ok": not self.errors,
        }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _item_to_dict(item: Content) -> dict:
    return {
        "title": item.title,
        "url": item.url,
        "category": item.category.slug if item.category else "other",
        "category_name": item.category.name if item.category else "Other",
        "status": item.status,
        "status_label": STATUS_LABELS.get(item.status, item.status),
        "chapter": item.current_chapter,
        "episode": item.current_episode,
        "season": item.current_season,
        "volume": item.current_volume,
        "progress_text": item.progress_text,
        "progress_percent": item.progress_percent,
        "rating": item.rating,
        "tags": [t.name for t in (item.tags or [])],
        "notes": item.notes,
        "description": item.description,
        "cover_image_url": item.cover_image_url,
        "is_favorite": bool(item.is_favorite),
        "open_count": item.open_count,
        "url_status": item.url_status,
        "latest_available": item.latest_available,
        "external_id": item.external_id,
        "update_source": item.update_source,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "last_opened_at": _iso(item.last_opened_at),
    }


def export_json(db: Session, user: User, *, include_settings: bool = True) -> str:
    """Lossless export. Never contains the password hash."""
    items = list(
        db.scalars(
            select(Content)
            .where(Content.user_id == user.id, Content.purged_at.is_(None))
            .order_by(Content.id)
        ).all()
    )
    settings_row = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    payload = {
        "app": "Personal Content Tracker",
        "version": EXPORT_VERSION,
        "exported_at": _iso(utcnow()),
        "account": {
            "email": user.email,
            "display_name": user.display_name,
            # Explicitly excluded: password hash, reset tokens, session tokens.
        },
        "item_count": len(items),
        "items": [_item_to_dict(item) for item in items],
    }
    if include_settings and settings_row is not None:
        payload["settings"] = {
            "theme": settings_row.theme,
            "default_view": settings_row.default_view,
            "items_per_page": settings_row.items_per_page,
            "default_category": settings_row.default_category,
            "auto_check_urls": settings_row.auto_check_urls,
            "auto_check_updates": settings_row.auto_check_updates,
            "check_interval_minutes": settings_row.check_interval_minutes,
            "notify_broken_url": settings_row.notify_broken_url,
            "notify_new_update": settings_row.notify_new_update,
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def export_csv(db: Session, user_id: int) -> str:
    items = list(
        db.scalars(
            select(Content)
            .where(Content.user_id == user_id, Content.purged_at.is_(None))
            .order_by(Content.id)
        ).all()
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = _item_to_dict(item)
        row["tags"] = ", ".join(row["tags"])
        row["is_favorite"] = "yes" if item.is_favorite else "no"
        writer.writerow(row)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalise_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either the JSON export shape or the CSV shape."""
    row = {(k or "").strip().lower(): v for k, v in raw.items()}
    tags = row.get("tags")
    if isinstance(tags, str):
        tags = normalize_tags(tags)
    elif tags is None:
        tags = []

    raw_category = str(row.get("category") or row.get("category_name") or "other").strip()
    # Compare ignoring spaces, underscores and slashes so "Movie / Cinema",
    # "movie_cinema" and "MovieCinema" all resolve to the same slug.
    def _squash(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    squashed = _squash(raw_category)
    aliases = {"movies": "movie", "cinema": "movie", "moviecinema": "movie", "film": "movie"}
    category = next(
        (slug for slug, meta in CATEGORIES.items() if _squash(meta["name"]) == squashed or slug == squashed),
        aliases.get(squashed, "other"),
    )

    status = str(row.get("status") or "following").strip().lower()
    if status not in STATUSES:  # maybe a display label was exported
        for slug, label in STATUS_LABELS.items():
            if label.lower() == status:
                status = slug
                break

    def _num(key: str) -> Any:
        value = row.get(key)
        if value in (None, "", "None"):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if float(number).is_integer() else number

    return {
        "title": row.get("title"),
        "url": row.get("url"),
        "category": category,
        "status": status,
        "description": row.get("description"),
        "notes": row.get("notes"),
        "rating": row.get("rating"),
        "tags": tags,
        "chapter": _num("chapter"),
        "episode": _num("episode"),
        "season": _num("season"),
        "volume": _num("volume"),
        "progress_text": row.get("progress_text"),
        "progress_percent": _num("progress_percent"),
        "cover_image_url": row.get("cover_image_url"),
        "is_favorite": _coerce_bool(row.get("is_favorite")),
        "external_id": row.get("external_id"),
        "update_source": row.get("update_source"),
    }


def parse_import_file(filename: str, raw: bytes) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return (rows, error). Supports .json and .csv."""
    name = (filename or "").lower()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], "That file is not UTF-8 text."

    if name.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], "That JSON file could not be parsed."
        if isinstance(data, dict):
            rows = data.get("items") or data.get("library") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list):
            return [], "Expected a list of items in the JSON file."
        return [r for r in rows if isinstance(r, dict)], None

    if name.endswith(".csv") or "," in text[:400]:
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = [dict(r) for r in reader if any((v or "").strip() for v in r.values())]
        except csv.Error:
            return [], "That CSV file could not be parsed."
        return rows, None

    return [], "Unsupported file type. Please upload a .json or .csv export."


def import_rows(
    db: Session,
    user_id: int,
    rows: List[Dict[str, Any]],
    *,
    skip_duplicates: bool = True,
    cache_covers: bool = False,
    max_rows: int = 5000,
) -> ImportReport:
    """Validate and insert rows. Bad rows are reported, not raised."""
    report = ImportReport(total=len(rows))
    if len(rows) > max_rows:
        report.errors.append({"row": 0, "error": f"Too many rows (maximum {max_rows})."})
        return report

    existing_urls = set(
        db.scalars(select(Content.url).where(Content.user_id == user_id, Content.purged_at.is_(None))).all()
    )

    for index, raw in enumerate(rows, start=1):
        try:
            row = _normalise_row(raw)
            payload = ContentPayload.from_mapping(row)
        except (ContentError, UrlValidationError) as exc:
            report.skipped += 1
            report.errors.append({"row": index, "title": str(raw.get("title"))[:80], "error": str(exc)})
            continue

        if skip_duplicates and payload.url in existing_urls:
            report.skipped += 1
            report.errors.append({"row": index, "title": payload.title, "error": "Duplicate URL (skipped)."})
            continue

        try:
            item = create_content(db, user_id, payload, cache_cover=cache_covers)
        except (ContentError, UrlValidationError) as exc:
            db.rollback()
            report.skipped += 1
            report.errors.append({"row": index, "title": payload.title, "error": str(exc)})
            continue

        if _coerce_bool(raw.get("is_favorite")):
            from .content_service import set_favorite

            set_favorite(db, user_id, item.id, True)

        existing_urls.add(payload.url)
        report.imported += 1

    if report.imported:
        from .activity import log_activity

        log_activity(db, user_id, "imported", f"You imported {report.imported} items.")
        db.commit()
    return report


def import_settings(db: Session, user_id: int, payload: dict) -> bool:
    """Apply preferences from a backup file (safe subset only)."""
    data = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False
    row = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if row is None:
        return False
    if data.get("theme") in ("dark", "light"):
        row.theme = data["theme"]
    if data.get("default_view") in ("grid", "list"):
        row.default_view = data["default_view"]
    try:
        per_page = int(data.get("items_per_page") or row.items_per_page)
        row.items_per_page = max(6, min(per_page, 120))
    except (TypeError, ValueError):
        pass
    for flag in ("auto_check_urls", "auto_check_updates", "notify_broken_url", "notify_new_update"):
        if flag in data:
            setattr(row, flag, _coerce_bool(data[flag]))
    db.commit()
    return True
