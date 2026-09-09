"""Import / export / backup endpoints."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import csrf_required, current_user, page_context, templates
from ..models import User
from ..services import backup as backup_service
from ..services import io_services

router = APIRouter(dependencies=[Depends(csrf_required)], tags=["data"])

def _max_upload_bytes() -> int:
    """Upload cap for import/restore, configurable via MAX_UPLOAD_MB."""
    return max(1, settings.max_upload_mb) * 1024 * 1024


def _download_response(content: str, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'},
    )


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    message = request.query_params.get("msg")
    error = request.query_params.get("err")
    imported = request.query_params.get("imported")
    return templates.TemplateResponse(
        request,
        "data.html",
        page_context(
            request,
            user,
            db,
            title="Import, export & backup",
            message=message,
            error=error,
            imported=int(imported) if imported else None,
            max_upload_mb=settings.max_upload_mb,
        ),
    )


@router.get("/export/json")
def export_json(user: User = Depends(current_user), db: Session = Depends(get_db)):
    content = io_services.export_json(db, user)
    return _download_response(content, f"content-tracker-{user.email.split('@')[0]}.json", "application/json")


@router.get("/export/csv")
def export_csv(user: User = Depends(current_user), db: Session = Depends(get_db)):
    content = io_services.export_csv(db, user.id)
    return _download_response(content, f"content-tracker-{user.email.split('@')[0]}.csv", "text/csv; charset=utf-8")


@router.get("/backup")
def download_backup(user: User = Depends(current_user), db: Session = Depends(get_db)):
    content = backup_service.build_backup(db, user)
    return _download_response(content, backup_service.backup_filename(user), "application/json")


@router.post("/import")
async def import_file(
    request: Request,
    file: UploadFile = File(...),
    apply_settings: bool = True,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    filename = file.filename or "import.json"
    max_bytes = _max_upload_bytes()
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return RedirectResponse("/data?err=too-large", status_code=303)
    if not raw.strip():
        return RedirectResponse("/data?err=empty", status_code=303)

    rows, parse_error = io_services.parse_import_file(filename, raw)
    if parse_error:
        return RedirectResponse(f"/data?err={quote(parse_error)}", status_code=303)

    # Preferences are applied even when the file carries no items, so a
    # settings-only backup still restores correctly.
    if apply_settings:
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
            if isinstance(payload, dict):
                io_services.import_settings(db, user.id, payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    if not rows:
        return RedirectResponse("/data?imported=0", status_code=303)

    report = io_services.import_rows(db, user.id, rows, cache_covers=False)
    return RedirectResponse(f"/data?imported={report.imported}", status_code=303)


@router.post("/restore")
async def restore_backup(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    max_bytes = _max_upload_bytes()
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return RedirectResponse("/data?err=too-large", status_code=303)
    result = backup_service.restore_from_backup(db, user, raw)
    if not result.get("ok"):
        return RedirectResponse(f"/data?err={quote(result.get('error') or 'restore-failed')}", status_code=303)
    return RedirectResponse(f"/data?imported={result.get('imported', 0)}", status_code=303)


@router.get("/api/export/preview")
def export_preview(user: User = Depends(current_user), db: Session = Depends(get_db), limit: int = 3):
    """Small JSON preview so the user can see the export shape before downloading."""
    content = json.loads(io_services.export_json(db, user))
    content["items"] = content["items"][: max(0, min(limit, 10))]
    return content
