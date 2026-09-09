"""
Update detection.

The most important property here is the one in the spec: **never claim an
update that cannot be verified.**  Every test asserts both the positive case
(a genuinely newer chapter/episode) and the negative cases (unreachable
source, no number on the page, older number).
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Content, Notification, UpdateHistory
from app.services import updates
from app.services.metadata import Metadata
from app.services.rss import FeedEntry
from app.utils.safe_http import FetchError, FetchResult, ThrottledError
from tests.conftest import add_item, api_post, body, content_id_from


class _StubResponse:
    def __init__(self, status_code=200, text="<html></html>"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.elapsed_ms = 10
        self.final_url = "https://example.org/x"
        self.content_type = "text/html"


def _item(content_id):
    with SessionLocal() as db:
        return db.get(Content, content_id)


def _stub_page(monkeypatch, chapter=None, episode=None, status_code=200, error=None):
    """Make page scraping return a fixed metadata result."""
    meta = Metadata(source="page", site_name="TestSite", chapter=chapter, episode=episode)
    monkeypatch.setattr(updates, "scrape_page", lambda url, **kw: meta)
    monkeypatch.setattr(updates, "safe_get", lambda url, **kw: (
        (_ for _ in ()).throw(error) if error else _StubResponse(status_code, f"<html>chapter {chapter}</html>")
    ))
    monkeypatch.setattr(updates, "fetch_feed", lambda url, **kw: [])
    return meta


# ---------------------------------------------------------------------------
# Manga / manhwa / manhua
# ---------------------------------------------------------------------------
def test_newer_chapter_is_reported(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=126)
    location = add_item(auth_client, title="Solo Leveling", url="https://example.org/solo",
                        category="manhwa", chapter=125).headers["location"]
    content_id = content_id_from(location)

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is True
    assert result["label"] == "Chapter 126"

    item = _item(content_id)
    assert item.update_available is True
    assert item.update_state == "unread"
    assert item.latest_available == "Chapter 126"


def test_same_chapter_reports_no_update(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=125)
    location = add_item(auth_client, title="Solo Leveling", url="https://example.org/solo2",
                        category="manhwa", chapter=125).headers["location"]
    content_id = content_id_from(location)

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is False
    assert _item(content_id).update_available is False


def test_older_chapter_does_not_report_a_regression(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=100)
    location = add_item(auth_client, title="Behind", url="https://example.org/behind",
                        category="manhwa", chapter=125).headers["location"]
    content_id = content_id_from(location)

    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is False
    assert _item(content_id).update_available is False


def test_no_chapter_on_page_means_no_claim(auth_client, monkeypatch):
    """If the page has no parsable number we must not invent an update."""
    _stub_page(monkeypatch, chapter=None)
    location = add_item(auth_client, title="Opaque", url="https://example.org/opaque",
                        category="manga", chapter=50).headers["location"]
    content_id = content_id_from(location)

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is False
    assert _item(content_id).update_available is False


def test_unreachable_source_does_not_claim_an_update(auth_client, monkeypatch):
    # An unreachable page yields empty metadata with an error attached,
    # exactly like the real scrape_page does.
    _stub_page(monkeypatch, chapter=None, error=FetchError("Could not connect to that site."))
    location = add_item(auth_client, title="Offline", url="https://example.org/offline",
                        category="manga", chapter=10).headers["location"]
    content_id = content_id_from(location)

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is False
    assert _item(content_id).update_available is False


def test_fractional_chapters_compare_correctly(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=125.5)
    location = add_item(auth_client, title="Half", url="https://example.org/half",
                        category="manga", chapter=125).headers["location"]
    content_id = content_id_from(location)
    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is True


def test_update_creates_history_and_notification(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=300)
    location = add_item(auth_client, title="Notified", url="https://example.org/notified",
                        category="manhua", chapter=299).headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/check-update")

    with SessionLocal() as db:
        history = db.query(UpdateHistory).filter(UpdateHistory.content_id == content_id).all()
        assert len(history) == 1
        assert history[0].label == "Chapter 300"
        assert history[0].state == "unread"

        notes = db.query(Notification).filter(Notification.content_id == content_id).all()
        assert len(notes) == 1
        assert notes[0].kind == "new_update"

    assert "Notified" in body(auth_client.get("/updates"))
    assert "Notified" in body(auth_client.get("/notifications"))


def test_marking_an_update_read_and_ignored(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=77)
    location = add_item(auth_client, title="States", url="https://example.org/states",
                        category="manga", chapter=76).headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/check-update")
    assert _item(content_id).update_state == "unread"

    api_post(auth_client, f"/api/content/{content_id}/update-state", {"state": "read"})
    assert _item(content_id).update_state == "read"
    assert _item(content_id).update_available is False

    api_post(auth_client, f"/api/content/{content_id}/update-state", {"state": "ignored"})
    assert _item(content_id).update_state == "ignored"

    with SessionLocal() as db:
        states = {h.state for h in db.query(UpdateHistory).filter(UpdateHistory.content_id == content_id)}
    assert states == {"ignored"}


def test_updating_progress_clears_the_update_flag(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=88)
    location = add_item(auth_client, title="Catch up", url="https://example.org/catchup",
                        category="manga", chapter=87).headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/check-update")
    assert _item(content_id).update_available is True

    api_post(auth_client, f"/api/content/{content_id}/progress", {"chapter": 88})
    assert _item(content_id).update_available is False
    assert _item(content_id).update_state == "read"


# ---------------------------------------------------------------------------
# Anime
# ---------------------------------------------------------------------------
def test_anime_episode_from_public_database(auth_client, monkeypatch):
    monkeypatch.setattr(updates, "jikan_latest_episode", lambda mal_id: {"episode": 13, "title": "Ep 13"})

    location = add_item(auth_client, title="Frieren", url="https://example.org/frieren",
                        category="anime", status="watching", episode=12).headers["location"]
    content_id = content_id_from(location)
    with SessionLocal() as db:
        db.get(Content, content_id).external_id = "52991"
        db.commit()

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is True
    assert result["label"] == "Episode 13"


def test_anime_without_linked_id_claims_nothing(auth_client, monkeypatch):
    monkeypatch.setattr(updates, "jikan_latest_episode", lambda mal_id: {"episode": 99})
    location = add_item(auth_client, title="No ID", url="https://example.org/noid",
                        category="anime", status="watching", episode=5).headers["location"]
    content_id = content_id_from(location)

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is False
    assert result["error"] == "No linked anime ID."


def test_anime_already_caught_up(auth_client, monkeypatch):
    monkeypatch.setattr(updates, "jikan_latest_episode", lambda mal_id: {"episode": 12})
    location = add_item(auth_client, title="Caught up", url="https://example.org/caughtup",
                        category="anime", status="watching", episode=12).headers["location"]
    content_id = content_id_from(location)
    with SessionLocal() as db:
        db.get(Content, content_id).external_id = "1"
        db.commit()
    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is False


# ---------------------------------------------------------------------------
# Feeds (news / coding)
# ---------------------------------------------------------------------------
def test_feed_update_detected_from_new_entry(auth_client, monkeypatch):
    entries = [FeedEntry(title="Brand new post", link="https://example.org/post", published="today",
                         summary="hi", source="Blog")]

    monkeypatch.setattr(updates, "fetch_feed", lambda url, **kw: entries)
    monkeypatch.setattr(updates, "safe_get", lambda url, **kw: _StubResponse(200, "<rss></rss>"))

    location = add_item(auth_client, title="A Blog", url="https://example.org/feed",
                        category="news", status="following").headers["location"]
    content_id = content_id_from(location)

    # First pass only records a baseline — no claim yet.
    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is False
    assert (_item(content_id).progress_json or {}).get("feed_top") is not None

    # Second pass with a different top entry is a verified new post.
    monkeypatch.setattr(
        updates, "fetch_feed",
        lambda url, **kw: [FeedEntry(title="Newer post", link="https://example.org/newer",
                                     published="today", summary="", source="Blog")],
    )
    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is True
    assert "Newer post" in result["label"]


def test_feed_page_fingerprint_needs_a_baseline(auth_client, monkeypatch):
    """Without a recorded baseline a changed page must not be reported."""
    monkeypatch.setattr(updates, "fetch_feed", lambda url, **kw: [])
    monkeypatch.setattr(updates, "safe_get", lambda url, **kw: _StubResponse(200, "<html>v2</html>"))

    location = add_item(auth_client, title="Sports page", url="https://example.org/sports",
                        category="sports", status="following").headers["location"]
    content_id = content_id_from(location)

    # The add flow records a baseline; make sure that happened.
    assert (_item(content_id).progress_json or {}).get("page_fingerprint")

    # Same content -> no update.
    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is False

    # Genuinely different content -> update.
    monkeypatch.setattr(updates, "safe_get", lambda url, **kw: _StubResponse(200, "<html>totally different</html>"))
    assert api_post(auth_client, f"/api/content/{content_id}/check-update").json()["found"] is True


def test_disabled_source_is_skipped(auth_client, monkeypatch):
    location = add_item(auth_client, title="Never check", url="https://example.org/never",
                        category="other", status="following").headers["location"]
    content_id = content_id_from(location)
    with SessionLocal() as db:
        item = db.get(Content, content_id)
        item.update_source = "none"
        db.commit()

    result = api_post(auth_client, f"/api/content/{content_id}/check-update").json()
    assert result["found"] is False
    assert "disabled" in (result["error"] or "").lower()


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------
def test_run_checks_processes_due_items(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=200)
    add_item(auth_client, title="Due A", url="https://example.org/duea", category="manga", chapter=199)
    add_item(auth_client, title="Due B", url="https://example.org/dueb", category="manhwa", chapter=199)

    response = api_post(auth_client, "/api/run-checks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["updates"]["checked"] == 2
    assert payload["updates"]["found"] == 2


def test_updates_page_lists_pending(auth_client, monkeypatch):
    _stub_page(monkeypatch, chapter=42)
    location = add_item(auth_client, title="Pending One", url="https://example.org/pendingone",
                        category="manga", chapter=41).headers["location"]
    content_id = content_id_from(location)
    api_post(auth_client, f"/api/content/{content_id}/check-update")

    page = body(auth_client.get("/updates"))
    assert "Pending One" in page
    assert "Chapter 42" in page


def test_update_state_endpoint_rejects_unknown_state(auth_client, monkeypatch):
    location = add_item(auth_client, title="Bad state", url="https://example.org/badstate",
                        category="manga").headers["location"]
    content_id = content_id_from(location)
    response = api_post(auth_client, f"/api/content/{content_id}/update-state", {"state": "bogus"})
    assert response.status_code == 400
