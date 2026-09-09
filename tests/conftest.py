"""
Shared fixtures.

Every test runs against a throwaway SQLite file so the real database is never
touched, and all outbound HTTP is replaced by a local stub — no test depends
on the network.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate configuration BEFORE the app modules are imported.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="pct-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use-1234567890"
os.environ["APP_ENV"] = "test"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ENABLE_THUMBNAIL_CACHE"] = "false"
os.environ["ENABLE_URL_METADATA_SCRAPING"] = "false"
os.environ["JIKAN_ENABLED"] = "true"   # on, but stubbed in tests (no real network calls)
os.environ["RSS_ENABLED"] = "false"
os.environ["ESPN_ENABLED"] = "false"
os.environ["TMDB_API_KEY"] = ""
os.environ["AUTH_RATE_LIMIT"] = "25"     # low enough to be exercised, high enough not to mask other auth tests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.utils import rate_limit  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_database():
    """Recreate every table before each test."""
    Base.metadata.drop_all(bind=engine)
    init_db()
    rate_limit.reset()
    yield
    Base.metadata.drop_all(bind=engine)


def api_post(client, url, payload=None, method="POST"):
    """State-changing API call with the CSRF header, exactly like static/js/app.js."""
    kwargs = {"headers": {"X-CSRF-Token": csrf_token(client)}}
    if payload is not None:
        kwargs["json"] = payload
    return client.request(method, url, **kwargs)


def content_id_from(location: str) -> int:
    """Pull the numeric id out of a /content/<id> redirect target."""
    return int(location.split("?")[0].rstrip("/").rsplit("/", 1)[-1])


def body(response) -> str:
    """Response text with HTML entities unescaped, so assertions read naturally."""
    import html

    return html.unescape(response.text)


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def register(client, email="reader@example.com", password="Str0ngPassw0rd!", name="Reader"):
    return client.post(
        "/register",
        data={
            "email": email,
            "password": password,
            "password_confirm": password,
            "display_name": name,
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )


def login(client, email="reader@example.com", password="Str0ngPassw0rd!"):
    return client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )


def csrf_token(client) -> str:
    """Read the CSRF token the server issued to this client."""
    cookie = client.cookies.get("pct_csrf")
    if cookie:
        return cookie
    client.get("/login")
    return client.cookies.get("pct_csrf") or ""


@pytest.fixture
def auth_client(client):
    """A client that is already signed in."""
    response = register(client)
    assert response.status_code == 303, response.text
    return client


def add_item(
    client,
    *,
    title="Solo Leveling",
    url="https://example.org/series/solo-leveling",
    category="manhwa",
    status="reading",
    chapter=None,
    episode=None,
    season=None,
    volume=None,
    rating="",
    tags="",
    notes="",
    progress_text="",
    progress_percent="",
    cover="",
):
    data = {
        "title": title,
        "url": url,
        "category": category,
        "status": status,
        "chapter": chapter if chapter is not None else "",
        "episode": episode if episode is not None else "",
        "season": season if season is not None else "",
        "volume": volume if volume is not None else "",
        "rating": rating,
        "tags": tags,
        "notes": notes,
        "progress_text": progress_text,
        "progress_percent": progress_percent,
        "description": "",
        "cover_image_url": cover,
        "external_id": "",
        "update_source": "",
        "auto_metadata": "0",
        "csrf_token": csrf_token(client),
    }
    return client.post("/add", data=data, follow_redirects=False)
