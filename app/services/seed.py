"""Seed reference data (categories + statuses). Idempotent."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import CATEGORIES, STATUSES
from ..models import Category, StatusOption, User, UserSettings


def seed_reference_data(db: Session) -> None:
    """Insert any missing category / status rows. Never overwrites edits."""
    existing = {row.slug: row for row in db.scalars(select(Category)).all()}
    for slug, meta in CATEGORIES.items():
        row = existing.get(slug)
        if row is None:
            db.add(
                Category(
                    slug=slug,
                    name=meta["name"],
                    icon=meta["icon"],
                    accent=meta["accent"],
                    sort_order=meta["sort_order"],
                    progress_fields=meta["progress_fields"],
                    status_options=meta["statuses"],
                    update_source=meta["update_source"],
                )
            )
            continue
        # progress_fields / status_options / update_source are app configuration
        # rather than user data, so keep them aligned with CATEGORIES. Otherwise
        # an older row would keep field names the form no longer recognises and
        # every progress input would stay hidden.
        if row.progress_fields != meta["progress_fields"]:
            row.progress_fields = meta["progress_fields"]
        if row.status_options != meta["statuses"]:
            row.status_options = meta["statuses"]
        if row.update_source != meta["update_source"]:
            row.update_source = meta["update_source"]

    existing_statuses = set(db.scalars(select(StatusOption.slug)).all())
    for slug, (label, group, color, order) in STATUSES.items():
        if slug in existing_statuses:
            continue
        db.add(StatusOption(slug=slug, label=label, group=group, color=color, sort_order=order))

    db.commit()


def ensure_user_settings(db: Session, user: User) -> UserSettings:
    """Return the user's settings row, creating it on first use."""
    row = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if row is None:
        row = UserSettings(user_id=user.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row
