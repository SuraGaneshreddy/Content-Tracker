"""Import / export / backup round-trips and validation."""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Content, Tag
from app.services import io_services
from tests.conftest import add_item, body, csrf_token, register


def _seed(client):
    add_item(client, title="Solo Leveling", url="https://example.org/solo", category="manhwa",
             status="reading", chapter=125, volume=12, rating=9, tags="Action, Fantasy",
             notes="Best fight arc.", progress_text="")
    add_item(client, title="Frieren", url="https://example.org/frieren", category="anime",
             status="watching", season=1, episode=12, rating=10)
    add_item(client, title="Rust Book", url="https://example.org/rust", category="coding",
             status="following", progress_percent=45, progress_text="Chapter 9")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_json_export_contains_everything_but_no_secrets(auth_client):
    _seed(auth_client)
    response = auth_client.get("/export/json")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]

    payload = json.loads(response.text)
    assert payload["app"] == "Personal Content Tracker"
    assert payload["item_count"] == 3
    assert payload["version"] == io_services.EXPORT_VERSION

    solo = next(item for item in payload["items"] if item["title"] == "Solo Leveling")
    assert solo["url"] == "https://example.org/solo"
    assert solo["category"] == "manhwa"
    assert solo["status"] == "reading"
    assert solo["chapter"] == 125
    assert solo["volume"] == 12
    assert solo["rating"] == 9
    assert sorted(solo["tags"]) == ["Action", "Fantasy"]
    assert solo["notes"] == "Best fight arc."

    # Secrets must never leave the server.
    assert "password" not in response.text.lower().replace("password_hash", "")
    assert "$argon2" not in response.text
    assert "token" not in json.dumps(payload["account"]).lower()

    assert payload["settings"]["theme"] in ("dark", "light")


def test_csv_export_is_valid_and_flat(auth_client):
    _seed(auth_client)
    response = auth_client.get("/export/csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 3
    assert set(rows[0]) == set(io_services.CSV_COLUMNS)

    solo = next(row for row in rows if row["title"] == "Solo Leveling")
    assert solo["chapter"] == "125"
    assert "Action" in solo["tags"] and "Fantasy" in solo["tags"]


def test_backup_download(auth_client):
    _seed(auth_client)
    response = auth_client.get("/backup")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert "content-tracker-backup-" in response.headers["content-disposition"]

    payload = json.loads(response.text)
    assert payload["backup"]["type"] == "manual"
    assert "password_hash" in payload["backup"]["excludes"]
    assert payload["item_count"] == 3


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def _upload(client, filename, content, endpoint="/import", extra=None):
    data = {"csrf_token": csrf_token(client)}
    if extra:
        data.update(extra)
    return client.post(
        endpoint,
        data=data,
        files={"file": (filename, content.encode("utf-8"), "application/octet-stream")},
        follow_redirects=False,
    )


def test_json_import_round_trip(client):
    register(client, email="source@example.com")
    _seed(client)
    exported = client.get("/export/json").text
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    register(client, email="target@example.com")
    response = _upload(client, "library.json", exported)
    assert response.status_code == 303
    assert "imported=3" in response.headers["location"]

    with SessionLocal() as db:
        count = db.scalar(select(func.count(Content.id)).where(Content.user_id == 2))
    assert count == 3

    assert "Solo Leveling" in body(client.get("/library"))


def test_csv_import(client):
    register(client)
    csv_text = (
        "title,url,category,status,chapter,episode,season,volume,progress_text,progress_percent,"
        "rating,tags,notes,description,cover_image_url,is_favorite,created_at\n"
        "Berserk,https://example.org/berserk,manga,reading,364,,,13,,,"
        "10,\"Dark, Fantasy\",Guts,,\n"
        "Dune,https://example.org/dune,movie,plan_to_watch,,,,,,,,,,,\n"
    )
    response = _upload(client, "library.csv", csv_text)
    assert response.status_code == 303
    assert "imported=2" in response.headers["location"]

    with SessionLocal() as db:
        titles = sorted(db.scalars(select(Content.title)).all())
        berserk = db.scalar(select(Content).where(Content.title == "Berserk"))
    assert titles == ["Berserk", "Dune"]
    assert berserk.current_chapter == 364
    assert berserk.rating == 10
    assert sorted(tag.name for tag in berserk.tags) == ["Dark", "Fantasy"]


def test_import_skips_duplicate_urls(client):
    register(client)
    _seed(client)
    exported = client.get("/export/json").text

    response = _upload(client, "again.json", exported)
    assert "imported=0" in response.headers["location"]

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Content.id))) == 3


def test_import_reports_invalid_rows_and_keeps_good_ones(client):
    register(client)
    payload = {
        "items": [
            {"title": "Good", "url": "https://example.org/good", "category": "manga", "status": "reading"},
            {"title": "", "url": "https://example.org/no-title", "category": "manga", "status": "reading"},
            {"title": "Bad URL", "url": "http://127.0.0.1/x", "category": "manga", "status": "reading"},
            {"title": "Bad rating", "url": "https://example.org/bad-rating", "category": "manga",
             "status": "reading", "rating": 99},
            {"title": "Unknown category", "url": "https://example.org/unknown-cat",
             "category": "pottery", "status": "following"},
        ]
    }
    response = _upload(client, "mixed.json", json.dumps(payload))
    assert "imported=2" in response.headers["location"]  # Good + unknown-category (falls back to Other)

    with SessionLocal() as db:
        titles = sorted(db.scalars(select(Content.title)).all())
        fallback = db.scalar(select(Content).where(Content.title == "Unknown category"))
    assert titles == ["Good", "Unknown category"]
    assert fallback.category.slug == "other"


def test_import_rejects_malformed_files(client):
    register(client)
    assert "err=" in _upload(client, "bad.json", "{not json").headers["location"]
    assert "err=" in _upload(client, "empty.json", "   ").headers["location"]
    assert "err=" in _upload(client, "notes.pdf", "binary-ish content").headers["location"]


def test_import_enforces_size_limit(client):
    register(client)
    huge = "x" * (13 * 1024 * 1024)
    response = _upload(client, "huge.json", huge)
    assert "err=too-large" in response.headers["location"]


def test_import_never_writes_another_users_data(client):
    register(client, email="first@example.com")
    _seed(client)
    exported = client.get("/export/json").text
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    register(client, email="second@example.com")
    _upload(client, "import.json", exported)

    with SessionLocal() as db:
        first_count = db.scalar(select(func.count(Content.id)).where(Content.user_id == 1))
        second_count = db.scalar(select(func.count(Content.id)).where(Content.user_id == 2))
    assert first_count == 3  # untouched
    assert second_count == 3  # copied into the second account


def test_import_accepts_display_names_for_category_and_status(client):
    register(client)
    payload = {
        "items": [
            {"title": "Friendly", "url": "https://example.org/friendly",
             "category": "Movie / Cinema", "status": "Currently Watching"}
        ]
    }
    _upload(client, "friendly.json", json.dumps(payload))
    with SessionLocal() as db:
        item = db.scalar(select(Content).where(Content.title == "Friendly"))
    assert item.category.slug == "movie"
    assert item.status == "watching"


def test_import_applies_preferences(client):
    register(client)
    payload = {"settings": {"theme": "light", "items_per_page": 48}, "items": []}
    response = _upload(client, "prefs.json", json.dumps(payload))
    assert response.status_code == 303
    from app.models import UserSettings

    with SessionLocal() as db:
        prefs = db.scalar(select(UserSettings).where(UserSettings.user_id == 1))
    assert prefs.theme == "light"
    assert prefs.items_per_page == 48


def test_restore_from_backup(client):
    register(client, email="backup-source@example.com")
    _seed(client)
    backup = client.get("/backup").text
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    register(client, email="restore@example.com")
    response = _upload(client, "backup.json", backup, endpoint="/restore")
    assert response.status_code == 303
    assert "imported=3" in response.headers["location"]

    from app.models import UserSettings

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Content.id)).where(Content.user_id == 2)) == 3
        assert db.scalar(select(UserSettings).where(UserSettings.user_id == 2)).theme == "dark"


def test_tags_are_deduplicated_on_import(client):
    register(client)
    payload = {
        "items": [
            {"title": "A", "url": "https://example.org/a", "category": "manga", "status": "reading",
             "tags": "Action, action, ACTION"},
            {"title": "B", "url": "https://example.org/b", "category": "manga", "status": "reading",
             "tags": "Action"},
        ]
    }
    _upload(client, "tags.json", json.dumps(payload))
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Tag.id))) == 1


def test_export_preview_is_limited(auth_client):
    for index in range(5):
        add_item(auth_client, title=f"Item {index}", url=f"https://example.org/prev{index}",
                 category="other", status="following")
    payload = auth_client.get("/api/export/preview?limit=2").json()
    assert len(payload["items"]) == 2
    assert payload["item_count"] == 5
