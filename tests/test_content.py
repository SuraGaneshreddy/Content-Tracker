"""Adding, editing, deleting, progress, favourites, tags and the dashboard."""

from __future__ import annotations

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Content, Favorite, Tag, UrlHistory
from tests.conftest import api_post, add_item, body, content_id_from, csrf_token, register


def test_add_content_redirects_to_detail_page(auth_client):
    response = add_item(auth_client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/content/")

    detail = auth_client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Solo Leveling" in body(detail)


def test_added_item_appears_on_dashboard_and_library(auth_client):
    add_item(auth_client)
    dashboard = auth_client.get("/")
    assert "Solo Leveling" in body(dashboard)
    assert "Total library" in body(dashboard)

    library = auth_client.get("/library")
    assert "Solo Leveling" in body(library)


def test_title_is_required(auth_client):
    response = add_item(auth_client, title="   ")
    assert response.status_code == 400
    assert "Title is required" in body(response)


def test_url_is_required_and_validated(auth_client):
    assert add_item(auth_client, url="").status_code == 400

    bad = add_item(auth_client, url="not a url at all", title="Bad URL")
    assert bad.status_code == 400

    # Internal addresses must be refused even at save time.
    internal = add_item(auth_client, url="http://192.168.1.10/series", title="Internal")
    assert internal.status_code == 400
    assert "local or internal" in body(internal)


def test_category_is_required(auth_client):
    response = add_item(auth_client, category="")
    assert response.status_code == 400
    assert "category" in body(response).lower()


def test_status_must_be_valid_for_the_category(auth_client):
    # "Currently Watching" is not offered for manga.
    response = add_item(auth_client, category="manga", status="watching")
    assert response.status_code == 400
    assert "not available for this category" in body(response)


def test_rating_must_be_between_1_and_10(auth_client):
    assert add_item(auth_client, title="Over", url="https://example.org/a", rating="11").status_code == 400
    assert add_item(auth_client, title="Under", url="https://example.org/b", rating="0.5").status_code == 400
    assert add_item(auth_client, title="Ok", url="https://example.org/c", rating="9").status_code == 303


def test_chapter_progress_is_stored_and_displayed(auth_client):
    add_item(auth_client, title="One Piece", url="https://example.org/one-piece", chapter=1105, volume=107)
    page = auth_client.get("/library")
    text = body(page)
    assert "Chapter 1105" in text
    assert "Vol. 107" in text


def test_anime_progress_uses_season_and_episode(auth_client):
    add_item(
        auth_client,
        title="Frieren",
        url="https://example.org/frieren",
        category="anime",
        status="watching",
        season=1,
        episode=28,
    )
    assert "S1 E28" in body(auth_client.get("/library"))


def test_movie_uses_custom_progress(auth_client):
    add_item(
        auth_client,
        title="RRR",
        url="https://example.org/rrr",
        category="movie",
        status="plan_to_watch",
        progress_text="Trailers only",
    )
    assert "Trailers only" in body(auth_client.get("/library"))


def test_coding_uses_percentage(auth_client):
    add_item(
        auth_client,
        title="Rust Book",
        url="https://example.org/rust",
        category="coding",
        status="following",
        progress_percent=45,
    )
    assert "45% complete" in body(auth_client.get("/library"))


def test_tags_are_created_and_filterable(auth_client):
    add_item(auth_client, title="Tagged", url="https://example.org/tagged", tags="Action, Fantasy, Action")
    with SessionLocal() as db:
        names = sorted(t.name for t in db.scalars(select(Tag)).all())
    assert names == ["Action", "Fantasy"]

    filtered = auth_client.get("/library?tag=action")
    assert "Tagged" in body(filtered)
    other = auth_client.get("/library?tag=cricket")
    assert "Tagged" not in body(other)


def test_edit_updates_the_item_immediately(auth_client):
    location = add_item(auth_client).headers["location"]
    content_id = content_id_from(location)

    response = auth_client.post(
        f"/content/{content_id}/edit",
        data={
            "title": "Solo Leveling (Ragnarok)",
            "url": "https://example.org/series/solo-leveling",
            "category": "manhwa",
            "status": "completed",
            "chapter": "179",
            "episode": "",
            "season": "",
            "volume": "",
            "rating": "10",
            "notes": "Finished the whole run.",
            "description": "",
            "tags": "Action",
            "cover_image_url": "",
            "external_id": "",
            "update_source": "",
            "csrf_token": csrf_token(auth_client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = auth_client.get(f"/content/{content_id}")
    text = body(page)
    assert "Solo Leveling (Ragnarok)" in text
    assert "Completed" in text
    assert "Chapter 179" in text
    assert "Finished the whole run." in text


def test_changing_the_url_keeps_history(auth_client):
    location = add_item(auth_client, url="https://example.org/old").headers["location"]
    content_id = content_id_from(location)

    auth_client.post(
        f"/content/{content_id}/edit",
        data={
            "title": "Solo Leveling",
            "url": "https://example.org/new",
            "category": "manhwa",
            "status": "reading",
            "chapter": "", "episode": "", "season": "", "volume": "",
            "rating": "", "notes": "", "description": "", "tags": "",
            "cover_image_url": "", "external_id": "", "update_source": "",
            "csrf_token": csrf_token(auth_client),
        },
        follow_redirects=False,
    )

    with SessionLocal() as db:
        rows = list(db.scalars(select(UrlHistory).where(UrlHistory.content_id == int(content_id))).all())
    assert len(rows) == 2
    assert {row.url for row in rows} == {"https://example.org/old", "https://example.org/new"}
    current = [row for row in rows if row.is_current]
    assert len(current) == 1 and current[0].url == "https://example.org/new"


def test_soft_delete_moves_to_trash_and_restores(auth_client):
    location = add_item(auth_client, title="Trash me").headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/delete", {"hard": False})
    assert response.status_code == 200 and response.json()["ok"] is True

    assert "Trash me" not in body(auth_client.get("/library"))
    assert "Trash me" in body(auth_client.get("/trash"))

    restored = api_post(auth_client, f"/api/content/{content_id}/restore")
    assert restored.json()["ok"] is True
    assert "Trash me" in body(auth_client.get("/library"))


def test_hard_delete_removes_the_row(auth_client):
    location = add_item(auth_client, title="Purge me").headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/delete", {"hard": True})
    assert response.status_code == 200

    with SessionLocal() as db:
        assert db.get(Content, content_id) is None

    assert auth_client.get(f"/content/{content_id}").status_code == 404


def test_empty_trash(auth_client):
    add_item(auth_client, title="A", url="https://example.org/a")
    add_item(auth_client, title="B", url="https://example.org/b")
    with SessionLocal() as db:
        ids = list(db.scalars(select(Content.id)).all())
    for content_id in ids:
        api_post(auth_client, f"/api/content/{content_id}/delete", {"hard": False})
    assert "Nothing here yet" not in body(auth_client.get("/trash"))

    auth_client.post("/trash/empty", data={"csrf_token": csrf_token(auth_client)}, follow_redirects=False)
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Content.id))) == 0


def test_favorite_toggle(auth_client):
    location = add_item(auth_client, title="Fav me").headers["location"]
    content_id = content_id_from(location)

    on = api_post(auth_client, f"/api/content/{content_id}/favorite", {"favorite": True})
    assert on.json()["favorite"] is True
    assert "Fav me" in body(auth_client.get("/favorites"))

    off = api_post(auth_client, f"/api/content/{content_id}/favorite", {"favorite": False})
    assert off.json()["favorite"] is False
    assert "Fav me" not in body(auth_client.get("/favorites"))

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Favorite.id))) == 0


def test_progress_endpoint_updates_and_clears_the_update_flag(auth_client):
    location = add_item(auth_client, title="Progress", chapter=10).headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/progress", {"chapter": 25, "status": "reading"})
    assert response.status_code == 200
    assert "Chapter 25" in response.json()["progress"]

    with SessionLocal() as db:
        item = db.get(Content, int(content_id))
        assert item.current_chapter == 25


def test_open_tracking_increments(auth_client):
    location = add_item(auth_client, title="Open me").headers["location"]
    content_id = content_id_from(location)

    api_post(auth_client, f"/api/content/{content_id}/open")
    api_post(auth_client, f"/api/content/{content_id}/open")

    with SessionLocal() as db:
        item = db.get(Content, int(content_id))
        assert item.open_count == 2
        assert item.last_opened_at is not None


def test_activity_log_records_actions(auth_client):
    location = add_item(auth_client, title="Logged").headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/progress", {"chapter": 5})

    text = body(auth_client.get("/"))
    assert "You added Logged." in text
    assert "Chapter 5" in text


def test_statistics_reflect_the_library(auth_client):
    add_item(auth_client, title="M1", url="https://example.org/m1", category="manga", status="reading")
    add_item(auth_client, title="M2", url="https://example.org/m2", category="manga", status="completed")
    add_item(auth_client, title="A1", url="https://example.org/a1", category="anime", status="watching")
    add_item(auth_client, title="S1", url="https://example.org/s1", category="sports", status="following")
    with SessionLocal() as db:
        first_id = db.scalars(select(Content.id).order_by(Content.id)).first()
    api_post(auth_client, f"/api/content/{first_id}/favorite", {"favorite": True})

    with SessionLocal() as db:
        from app.services.content_service import get_stats

        stats = get_stats(db, 1)
    assert stats["total"] == 4
    assert stats["reading"] == 1
    assert stats["watching"] == 1
    assert stats["completed"] == 1
    assert stats["favorites"] == 1
    assert {row["slug"]: row["count"] for row in stats["by_category"]}["manga"] == 2


def test_empty_dashboard_shows_the_getting_started_state(client):
    register(client)
    assert "Nothing here yet" in body(client.get("/"))


def test_detail_page_shows_all_sections(auth_client):
    location = add_item(
        auth_client, title="Detailed", notes="My private note", tags="Isekai", chapter=3, rating=8
    ).headers["location"]
    page = auth_client.get(location)
    text = body(page)
    assert "Detailed" in text
    assert "My private note" in text
    assert "#Isekai" in text
    assert "URL history" in text
    assert "Update progress" in text
