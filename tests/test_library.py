"""Library filtering, sorting, pagination, search and the category pages."""

from __future__ import annotations

from tests.conftest import api_post, add_item, body, content_id_from, register


def _seed(client):
    add_item(client, title="Solo Leveling", url="https://example.org/solo", category="manhwa",
             status="reading", chapter=125, rating=9, tags="Action, Fantasy")
    add_item(client, title="Solo Max-Level Newbie", url="https://example.org/newbie", category="manhwa",
             status="plan_to_read", rating=7, tags="Action")
    add_item(client, title="Frieren", url="https://example.org/frieren", category="anime",
             status="watching", season=1, episode=12, rating=10, tags="Fantasy")
    add_item(client, title="One Piece", url="https://example.org/onepiece", category="manga",
             status="on_hold", chapter=1100, rating=8)
    add_item(client, title="Champions League", url="https://example.org/ucl", category="sports",
             status="following", tags="Cricket")


def test_library_lists_everything(auth_client):
    _seed(auth_client)
    text = body(auth_client.get("/library"))
    for title in ["Solo Leveling", "Solo Max-Level Newbie", "Frieren", "One Piece", "Champions League"]:
        assert title in text
    assert "5 items" in text


def test_filter_by_status(auth_client):
    _seed(auth_client)
    reading = body(auth_client.get("/library?status=reading"))
    assert "Solo Leveling" in reading
    assert "Frieren" not in reading

    planning = body(auth_client.get("/library?status=planning"))
    assert "Solo Max-Level Newbie" in planning
    assert "Solo Leveling" not in planning

    on_hold = body(auth_client.get("/library?status=on_hold"))
    assert "One Piece" in on_hold


def test_filter_by_category(auth_client):
    _seed(auth_client)
    manhwa = body(auth_client.get("/category/manhwa"))
    assert "Solo Leveling" in manhwa
    assert "Frieren" not in manhwa

    anime = body(auth_client.get("/category/anime"))
    assert "Frieren" in anime
    assert "One Piece" not in anime


def test_unknown_category_is_404(auth_client):
    assert auth_client.get("/category/notathing").status_code == 404


def test_filter_by_minimum_rating(auth_client):
    _seed(auth_client)
    text = body(auth_client.get("/library?rating=9"))
    assert "Solo Leveling" in text
    assert "Frieren" in text
    assert "One Piece" not in text


def test_filter_by_tag(auth_client):
    _seed(auth_client)
    action = body(auth_client.get("/library?tag=action"))
    assert "Solo Leveling" in action
    assert "Solo Max-Level Newbie" in action
    assert "One Piece" not in action

    cricket = body(auth_client.get("/library?tag=cricket"))
    assert "Champions League" in cricket


def test_sorting(auth_client):
    _seed(auth_client)
    az = body(auth_client.get("/library?sort=az"))
    za = body(auth_client.get("/library?sort=za"))
    assert az.index("Champions League") < az.index("Solo Leveling")
    assert za.index("Solo Leveling") < za.index("Champions League")

    top = body(auth_client.get("/library?sort=rating"))
    # Frieren is the only 10/10, so it comes first.
    assert top.index("Frieren") < top.index("Solo Leveling")


def test_pagination(auth_client):
    for index in range(7):
        add_item(auth_client, title=f"Item {index}", url=f"https://example.org/item{index}",
                 category="other", status="following")
    page_one = body(auth_client.get("/library?per_page=6&page=1"))
    page_two = body(auth_client.get("/library?per_page=6&page=2"))
    assert "7 items" in page_one
    assert "Item 6" in page_one
    assert "Item 0" in page_two
    assert "Next →" in page_one


def test_search_matches_titles(auth_client):
    _seed(auth_client)
    results = body(auth_client.get("/search?q=Solo"))
    assert "Solo Leveling" in results
    assert "Solo Max-Level Newbie" in results
    assert "Frieren" not in results


def test_search_matches_notes_and_tags(auth_client):
    add_item(auth_client, title="Quiet Title", url="https://example.org/quiet", notes="recommended by Ravi")
    assert "Quiet Title" in body(auth_client.get("/search?q=ravi"))

    add_item(auth_client, title="Tagged Thing", url="https://example.org/taggedthing", tags="Isekai")
    assert "Tagged Thing" in body(auth_client.get("/search?q=isekai"))


def test_search_api_returns_json(auth_client):
    _seed(auth_client)
    payload = auth_client.get("/api/search?q=Solo").json()
    titles = [row["title"] for row in payload["results"]]
    assert "Solo Leveling" in titles
    assert "Solo Max-Level Newbie" in titles


def test_search_ignores_other_users_data(client):
    register(client, email="one@example.com")
    add_item(client, title="Private Item", url="https://example.org/private")
    client.post("/logout", data={"csrf_token": client.cookies.get("pct_csrf")})

    register(client, email="two@example.com")
    assert "Private Item" not in body(client.get("/search?q=Private"))
    assert "Private Item" not in body(client.get("/library"))


def test_library_json_endpoint(auth_client):
    _seed(auth_client)
    payload = auth_client.get("/api/library?per_page=12").json()
    assert payload["total"] == 5
    titles = [row["title"] for row in payload["items"]]
    assert "Solo Leveling" in titles
    first = payload["items"][0]
    assert {"id", "title", "category", "status_label", "progress", "url"} <= set(first)


def test_favorites_page(auth_client):
    _seed(auth_client)
    location = add_item(auth_client, title="Starred", url="https://example.org/starred").headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/favorite", {"favorite": True})

    page = body(auth_client.get("/favorites"))
    assert "Starred" in page
    assert "One Piece" not in page


def test_tags_page_lists_usage(auth_client):
    _seed(auth_client)
    page = body(auth_client.get("/tags"))
    assert "#Action" in page
    assert "#Fantasy" in page
    assert "#Cricket" in page


def test_prune_unused_tags(auth_client):
    add_item(auth_client, title="Tagged", url="https://example.org/t", tags="Used")
    api_post(auth_client, "/api/tags/prune")
    page = body(auth_client.get("/tags"))
    assert "#Used" in page


def test_combined_filters(auth_client):
    _seed(auth_client)
    page = body(auth_client.get("/library?status=reading&q=Solo"))
    assert "Solo Leveling" in page
    assert "Solo Max-Level Newbie" not in page


def test_empty_filtered_result_shows_helpful_message(auth_client):
    _seed(auth_client)
    page = body(auth_client.get("/library?q=zzzznotathing"))
    assert "Nothing matches those filters" in page
