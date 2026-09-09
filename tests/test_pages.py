"""
Every page renders — for a signed-in user, an anonymous visitor and an admin.

External lookups are stubbed so category update pages render deterministically
without touching the network.
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Content
from app.services.metadata import Metadata
from app.services.rss import FeedEntry
from tests.conftest import add_item, body, content_id_from, register


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub every outbound call so page tests never hit the internet."""
    from app.services import metadata, rss, updates

    from app.utils.safe_http import FetchError

    monkeypatch.setattr(metadata, "safe_get", lambda *a, **k: (_ for _ in ()).throw(FetchError("blocked in tests")))
    monkeypatch.setattr(updates, "scrape_page", lambda url, **kw: Metadata(source="page"))
    monkeypatch.setattr(updates, "fetch_feed", lambda url, **kw: [])
    monkeypatch.setattr(updates, "jikan_latest_episode", lambda mal_id: None)
    monkeypatch.setattr(rss, "fetch_feed", lambda url, **kw: [])
    monkeypatch.setattr(rss, "NEWS_FEEDS", {"world": []})
    monkeypatch.setattr(rss, "SPORTS_FEEDS", {})
    monkeypatch.setattr(rss, "CODING_FEEDS", [])


PUBLIC_PAGES = ["/login", "/register", "/forgot-password", "/healthz"]

AUTHED_PAGES = [
    "/",
    "/add",
    "/library",
    "/favorites",
    "/tags",
    "/trash",
    "/search",
    "/search?q=solo",
    "/notifications",
    "/updates",
    "/updates/anime",
    "/updates/manga",
    "/updates/manhwa",
    "/updates/manhua",
    "/updates/movies",
    "/updates/movies?country=jp",
    "/updates/sports",
    "/updates/sports?sport=f1",
    "/updates/news",
    "/updates/news?topic=business",
    "/updates/coding",
    "/settings",
    "/profile",
    "/data",
    "/library?status=reading",
    "/library?sort=rating&per_page=12",
    "/library?q=solo",
    "/library?tag=action",
]

CATEGORY_PAGES = [
    "/category/manga",
    "/category/manhwa",
    "/category/manhua",
    "/category/anime",
    "/category/movie",
    "/category/sports",
    "/category/news",
    "/category/coding",
    "/category/other",
]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200, path


def test_anonymous_visitors_are_sent_to_sign_in(client):
    for path in ["/", "/library", "/add", "/updates", "/settings", "/profile", "/data"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] in ("/login", "/register"), path


def test_first_visitor_is_sent_to_registration(client):
    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/register"


@pytest.mark.parametrize("path", AUTHED_PAGES)
def test_authed_pages_render(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200, path
    assert "<!DOCTYPE html>" in response.text, path


@pytest.mark.parametrize("path", CATEGORY_PAGES)
def test_category_pages_render(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200, path


def test_admin_page_renders_for_admin(auth_client):
    add_item(auth_client, title="Admin View", url="https://example.org/adminview")
    response = auth_client.get("/admin")
    assert response.status_code == 200
    text = body(response)
    assert "Background scheduler" in text
    assert "Table sizes" in text
    assert "$argon2" not in response.text  # no secrets rendered
    assert "postgres" not in text or "***" in text  # DB URL is redacted


def test_pages_render_with_a_full_library(auth_client):
    """Dashboard, library and category pages with real cards on them."""
    add_item(auth_client, title="Solo Leveling", url="https://example.org/solo",
             category="manhwa", status="reading", chapter=125, rating=9, tags="Action")
    add_item(auth_client, title="Frieren", url="https://example.org/frieren",
             category="anime", status="watching", season=1, episode=12)
    add_item(auth_client, title="RRR", url="https://example.org/rrr",
             category="movie", status="plan_to_watch", progress_text="Trailer seen")
    add_item(auth_client, title="IPL", url="https://example.org/ipl",
             category="sports", status="following", progress_text="Following MI")
    add_item(auth_client, title="Rust Book", url="https://example.org/rust",
             category="coding", status="following", progress_percent=45)
    add_item(auth_client, title="BBC News", url="https://example.org/bbc",
             category="news", status="following")

    for path in ["/", "/library", "/category/manhwa", "/category/anime", "/category/movie",
                 "/category/sports", "/category/coding", "/category/news", "/tags", "/updates"]:
        response = auth_client.get(path)
        assert response.status_code == 200, path
        assert "Traceback" not in response.text, path


def test_detail_and_edit_render_for_every_category(auth_client):
    seeds = [
        ("Manga Item", "manga", "reading", {"chapter": 10, "volume": 2}),
        ("Manhwa Item", "manhwa", "reading", {"chapter": 20}),
        ("Manhua Item", "manhua", "reading", {"chapter": 30}),
        ("Anime Item", "anime", "watching", {"season": 2, "episode": 4}),
        ("Movie Item", "movie", "plan_to_watch", {"progress_text": "Not yet"}),
        ("Sports Item", "sports", "following", {"progress_text": "Team X"}),
        ("News Item", "news", "following", {}),
        ("Coding Item", "coding", "following", {"progress_percent": 70}),
        ("Other Item", "other", "following", {"progress_text": "Anything"}),
    ]
    for index, (title, category, status, extra) in enumerate(seeds):
        location = add_item(
            auth_client, title=title, url=f"https://example.org/detail{index}",
            category=category, status=status, **extra
        ).headers["location"]
        content_id = content_id_from(location)

        detail = auth_client.get(f"/content/{content_id}")
        assert detail.status_code == 200, title
        assert title in body(detail)
        assert "Update progress" in body(detail)

        edit = auth_client.get(f"/content/{content_id}/edit")
        assert edit.status_code == 200, title
        assert title in body(edit)


def test_progress_fields_match_the_category(auth_client):
    """The form is category-aware: the server declares which progress fields apply."""
    def edit_page_for(title, url, category, status):
        location = add_item(auth_client, title=title, url=url,
                            category=category, status=status).headers["location"]
        return body(auth_client.get(f"/content/{content_id_from(location)}/edit"))

    manga = edit_page_for("M", "https://example.org/pfm", "manga", "reading")
    assert 'data-fields-for="manga" data-fields-for-value="chapter,volume"' in manga

    anime = edit_page_for("A", "https://example.org/pfa", "anime", "watching")
    assert 'data-fields-for="anime" data-fields-for-value="season,episode"' in anime

    movie = edit_page_for("Mo", "https://example.org/pfmo", "movie", "plan_to_watch")
    # Movies have no chapter/episode columns — free-text progress and a percentage.
    assert 'data-fields-for="movie" data-fields-for-value="progress_text,progress_percent"' in movie
    assert "chapter" not in movie.split('data-fields-for="movie"')[1].split(">")[0]

    coding = edit_page_for("Co", "https://example.org/pfco", "coding", "following")
    assert 'data-fields-for="coding" data-fields-for-value="progress_percent,progress_text"' in coding


def test_theme_is_reflected_in_the_page(auth_client):
    from tests.conftest import csrf_token

    auth_client.post(
        "/settings",
        data={"theme": "light", "default_view": "list", "items_per_page": "24",
              "auto_check_urls": "on", "auto_check_updates": "on",
              "check_interval_minutes": "120", "csrf_token": csrf_token(auth_client)},
        follow_redirects=False,
    )
    response = auth_client.get("/")
    assert '<html lang="en" data-theme="light">' in response.text
    # The remembered view mode is applied server-side, before any JS runs.
    assert '<body class="list-view">' in response.text

    from app.models import UserSettings
    from sqlalchemy import select

    with SessionLocal() as db:
        prefs = db.scalar(select(UserSettings).where(UserSettings.user_id == 1))
    assert prefs.theme == "light"
    assert prefs.check_interval_minutes == 120


def test_theme_api_toggles(auth_client):
    from tests.conftest import api_post

    response = api_post(auth_client, "/api/theme", {"theme": "light"})
    assert response.json() == {"ok": True, "theme": "light"}
    assert 'data-theme="light"' in auth_client.get("/").text


def test_broken_link_warning_and_alternative_link_are_shown(auth_client):
    location = add_item(auth_client, title="Dead Link", url="https://example.org/dead").headers["location"]
    content_id = content_id_from(location)
    with SessionLocal() as db:
        item = db.get(Content, content_id)
        item.url_status = "broken"
        item.url_status_code = 404
        item.url_last_error = "The site returned a not-found response."
        db.commit()

    text = body(auth_client.get(location))
    assert "may no longer be working" in text.lower()
    assert "Find Alternative" in text

    assert "Dead Link" in body(auth_client.get("/"))  # dashboard surfaces it too


def test_notifications_page_actions(auth_client):
    from app.services.activity import create_notification

    with SessionLocal() as db:
        note = create_notification(db, 1, "info", "Hello there", message="A test notification")
        note_id = note.id

    page = body(auth_client.get("/notifications"))
    assert "Hello there" in page
    assert "A test notification" in page

    api = auth_client.get("/api/notifications").json()
    assert api["unread"] == 1
    assert any(item["id"] == note_id for item in api["items"])


def test_trash_page_shows_deleted_items(auth_client):
    location = add_item(auth_client, title="Trashed").headers["location"]
    content_id = content_id_from(location)
    from tests.conftest import api_post

    api_post(auth_client, f"/api/content/{content_id}/delete", {"hard": False})
    page = body(auth_client.get("/trash"))
    assert "Trashed" in page
    assert "Restore" in page


def test_settings_page_lists_integrations(auth_client):
    text = body(auth_client.get("/settings"))
    for panel in ["Appearance", "Automatic checks", "Data sources", "Cover cache", "Your data"]:
        assert panel in text, panel
    # External integrations are reported but never leak their keys.
    assert "Data sources" in text
    assert "Download backup" in text


def test_data_page_lists_export_options(auth_client):
    text = body(auth_client.get("/data"))
    assert "Export as JSON" in text
    assert "Export as CSV" in text
    assert "Download my backup" in text
    assert "Import" in text
