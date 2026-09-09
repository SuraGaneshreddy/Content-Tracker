"""
URL health checking and "find alternative" suggestions.

All network access is replaced with a local stub, so these tests are
deterministic and never touch the internet.
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Content, Notification, UrlCheckLog, UrlHistory
from app.services import url_checker
from app.utils.safe_http import FetchError, FetchResult, ThrottledError, UnsafeUrlError, health_status_from_code
from tests.conftest import add_item, api_post, body, content_id_from, register


class _StubResponse:
    def __init__(self, status_code=200, elapsed_ms=42, text="<html><title>ok</title></html>"):
        self.status_code = status_code
        self.elapsed_ms = elapsed_ms
        self.text = text
        self.final_url = "https://example.org/x"
        self.content_type = "text/html"
        self.content = text.encode()


def _stub(monkeypatch, behaviour):
    """Replace safe_get everywhere it is imported from."""
    def fake(url, **kwargs):
        return behaviour(url, **kwargs)

    import app.services.url_checker as uc
    import app.services.updates as up
    import app.utils.safe_http as sh

    monkeypatch.setattr(uc, "safe_get", fake)
    monkeypatch.setattr(up, "safe_get", fake)
    monkeypatch.setattr(sh, "safe_get", fake)
    return fake


def _get_item(content_id):
    with SessionLocal() as db:
        return db.get(Content, content_id)


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,expected",
    [
        (200, "ok"),
        (204, "ok"),
        (301, "ok"),
        (404, "broken"),
        (410, "broken"),
        (403, "degraded"),
        (429, "degraded"),
        (500, "degraded"),
        (503, "degraded"),
    ],
)
def test_http_status_maps_to_health(code, expected):
    assert health_status_from_code(code) == expected


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------
def test_working_link_is_marked_ok(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(200))
    location = add_item(auth_client, title="Healthy", url="https://example.org/healthy").headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/check-url")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["status_code"] == 200

    item = _get_item(content_id)
    assert item.url_status == "ok"
    assert item.url_last_checked_at is not None


def test_missing_link_is_marked_broken_and_notifies(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(404))
    location = add_item(auth_client, title="Gone", url="https://example.org/gone").headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/check-url")
    assert response.json()["status"] == "broken"

    item = _get_item(content_id)
    assert item.url_status == "broken"
    assert item.url_status_code == 404

    with SessionLocal() as db:
        note = db.query(Notification).filter(Notification.content_id == content_id).first()
        assert note is not None
        assert note.kind == "broken_url"
        assert "no longer be working" in note.title.lower() or "link" in note.title.lower()

    assert "no longer be working" in body(auth_client.get(f"/content/{content_id}")).lower()


def test_server_error_is_treated_as_temporary_not_broken(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(503))
    location = add_item(auth_client, title="Flaky", url="https://example.org/flaky").headers["location"]
    content_id = content_id_from(location)

    api_post(auth_client, f"/api/content/{content_id}/check-url")
    assert _get_item(content_id).url_status == "degraded"


def test_network_failure_is_treated_as_temporary(auth_client, monkeypatch):
    def boom(url, **kwargs):
        raise FetchError("Could not connect to that site.")

    _stub(monkeypatch, boom)
    location = add_item(auth_client, title="Unreachable", url="https://example.org/down").headers["location"]
    content_id = content_id_from(location)

    response = api_post(auth_client, f"/api/content/{content_id}/check-url")
    assert response.json()["status"] == "degraded"
    assert "Could not connect" in (response.json()["error"] or "")


def test_throttling_does_not_overwrite_the_last_known_status(auth_client, monkeypatch):
    """A throttled check must not be reported as a verdict about the link."""
    calls = {"n": 0}

    def throttled(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _StubResponse(200)
        raise ThrottledError("Too soon to contact example.org again.")

    _stub(monkeypatch, throttled)
    location = add_item(auth_client, title="Throttled", url="https://example.org/t").headers["location"]
    content_id = content_id_from(location)

    api_post(auth_client, f"/api/content/{content_id}/check-url")
    assert _get_item(content_id).url_status == "ok"

    result = api_post(auth_client, f"/api/content/{content_id}/check-url").json()
    assert result["status"] == "ok"  # unchanged, not degraded


def test_repeated_failures_escalate_to_broken(auth_client, monkeypatch):
    """A link that keeps failing eventually becomes 'broken'."""
    _stub(monkeypatch, lambda url, **kw: (_ for _ in ()).throw(FetchError("timeout")))
    location = add_item(auth_client, title="Persistent", url="https://example.org/p").headers["location"]
    content_id = content_id_from(location)

    with SessionLocal() as db:
        item = db.get(Content, content_id)
        for _ in range(url_checker.DEGRADED_STREAK_BEFORE_BROKEN):
            url_checker.check_single(db, item, force=True)

    assert _get_item(content_id).url_status == "broken"


def test_check_result_is_mirrored_onto_url_history_and_logged(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(200, elapsed_ms=17))
    location = add_item(auth_client, title="Logged", url="https://example.org/logged").headers["location"]
    content_id = content_id_from(location)

    api_post(auth_client, f"/api/content/{content_id}/check-url")

    with SessionLocal() as db:
        history = db.query(UrlHistory).filter(UrlHistory.content_id == content_id).first()
        assert history.status == "ok"
        assert history.last_checked_at is not None

        log = db.query(UrlCheckLog).filter(UrlCheckLog.content_id == content_id).first()
        assert log is not None
        assert log.status == "ok"
        assert log.latency_ms == 17
        assert log.host == "example.org"


def test_head_405_falls_back_to_get(auth_client, monkeypatch):
    """Some servers reject HEAD; we retry with GET before judging the link."""
    def stub(url, **kwargs):
        return _StubResponse(405) if kwargs.get("head_only") else _StubResponse(200)

    _stub(monkeypatch, stub)
    location = add_item(auth_client, title="NoHead", url="https://example.org/nohead").headers["location"]
    content_id = content_id_from(location)

    assert api_post(auth_client, f"/api/content/{content_id}/check-url").json()["status"] == "ok"


def test_batch_check_summary(auth_client, monkeypatch):
    def stub(url, **kwargs):
        if "broken" in url:
            return _StubResponse(404)
        if "flaky" in url:
            return _StubResponse(500)
        return _StubResponse(200)

    _stub(monkeypatch, stub)
    add_item(auth_client, title="A", url="https://example.org/a")
    add_item(auth_client, title="B", url="https://example.org/broken")
    add_item(auth_client, title="C", url="https://example.org/flaky")

    with SessionLocal() as db:
        items = list(db.query(Content).all())
        summary = url_checker.check_many(db, items, force=True)

    assert summary["checked"] == 3
    assert summary["ok"] == 1
    assert summary["broken"] == 1
    assert summary["degraded"] == 1


def test_ssrf_urls_never_reach_the_network(auth_client, monkeypatch):
    reached = []

    def spy(url, **kwargs):
        reached.append(url)
        return _StubResponse(200)

    _stub(monkeypatch, spy)
    response = add_item(auth_client, title="SSRF", url="http://169.254.169.254/latest/meta-data/")
    assert response.status_code == 400
    assert reached == []


# ---------------------------------------------------------------------------
# Alternatives
# ---------------------------------------------------------------------------
def test_alternatives_are_legitimate_sources_only(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(404))
    location = add_item(auth_client, title="Solo Leveling", url="https://example.org/solo",
                        category="manhwa").headers["location"]
    content_id = content_id_from(location)

    page = auth_client.get(f"/content/{content_id}/alternatives")
    assert page.status_code == 200
    text = body(page)
    assert "Alternative sources" in text
    assert "review the destination" in text.lower()
    # Only reputable catalogues / search engines are offered.
    assert "wikipedia.org" in text or "duckduckgo.com" in text
    for banned in ["127.0.0.1", "localhost", "http://10.", "javascript:"]:
        assert banned not in text


def test_alternative_url_is_reviewed_before_it_replaces_the_current_one(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(200))
    location = add_item(auth_client, title="Swap", url="https://example.org/old",
                        category="anime", status="watching").headers["location"]
    content_id = content_id_from(location)

    # Nothing changes until the user explicitly submits one.
    assert _get_item(content_id).url == "https://example.org/old"

    from tests.conftest import csrf_token

    auth_client.post(
        f"/content/{content_id}/use-alternative",
        data={"new_url": "https://myanimelist.net/anime/1", "csrf_token": csrf_token(auth_client)},
        follow_redirects=False,
    )
    assert _get_item(content_id).url == "https://myanimelist.net/anime/1"

    with SessionLocal() as db:
        rows = list(db.query(UrlHistory).filter(UrlHistory.content_id == content_id).all())
    assert len(rows) == 2
    assert {r.url for r in rows} == {"https://example.org/old", "https://myanimelist.net/anime/1"}
    # The suggestion is recorded as the origin of the change.
    assert any(r.source == "suggested" for r in rows)


def test_invalid_alternative_is_rejected(auth_client, monkeypatch):
    _stub(monkeypatch, lambda url, **kw: _StubResponse(200))
    location = add_item(auth_client, title="Bad swap", url="https://example.org/keep").headers["location"]
    content_id = content_id_from(location)

    from tests.conftest import csrf_token

    auth_client.post(
        f"/content/{content_id}/use-alternative",
        data={"new_url": "http://192.168.0.1/x", "csrf_token": csrf_token(auth_client)},
        follow_redirects=False,
    )
    assert _get_item(content_id).url == "https://example.org/keep"
