"""
Manual backup.

A backup is the full JSON export plus a small manifest.  It deliberately
excludes anything secret: password hashes, reset tokens, session tokens and
API keys are never written to the file.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import User, utcnow
from . import io_services


def build_backup(db: Session, user: User) -> str:
    """Return the backup document as a JSON string."""
    payload = json.loads(io_services.export_json(db, user, include_settings=True))
    payload["backup"] = {
        "type": "manual",
        "created_at": utcnow().isoformat(timespec="seconds"),
        "app_version": "1.0.0",
        "excludes": ["password_hash", "session_tokens", "reset_tokens", "api_keys"],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def backup_filename(user: User) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d")
    who = (user.display_name or user.email.split("@")[0] or "library").strip()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in who).strip("-") or "library"
    return f"content-tracker-backup-{safe}-{stamp}.json"


def restore_from_backup(db: Session, user: User, raw: bytes, *, cache_covers: bool = False) -> dict:
    """Restore a backup file (items + preferences) into the user's account."""
    rows, error = io_services.parse_import_file("backup.json", raw)
    if error:
        return {"ok": False, "error": error}

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    if isinstance(payload, dict):
        io_services.import_settings(db, user.id, payload)

    report = io_services.import_rows(db, user.id, rows, cache_covers=cache_covers)
    result = report.as_dict()
    result["ok"] = True
    return result
