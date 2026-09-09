"""
Security: authorisation, CSRF, XSS, SQL injection, SSRF, rate limiting and
error handling that never leaks internals.
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import User
from app.security import csrf_token_for, hash_password, verify_password
from app.utils.safe_http import UnsafeUrlError, resolve_and_validate
from app.utils.url_validation import UrlValidationError, validate_url_syntax
from tests.conftest import api_post, add_item, body, content_id_from, csrf_token, register


def _second_user(client, email="intruder@example.com"):
    register(client, email=email, password="An0therPassw0rd!", name="Intruder")
    return client


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
def test_users_cannot_read_each_others_items(client):
    register(client, email="owner@example.com")
    location = add_item(client, title="Owner Secret", url="https://example.org/owner-secret").headers["location"]
    content_id = content_id_from(location)
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    _second_user(client)
    assert client.get(f"/content/{content_id}").status_code == 404
    assert client.get(f"/content/{content_id}/edit").status_code == 404
    assert "Owner Secret" not in body(client.get("/library"))
    assert client.get("/api/library").json()["total"] == 0


def test_users_cannot_mutate_each_others_items(client):
    register(client, email="owner2@example.com")
    location = add_item(client, title="Victim", url="https://example.org/victim").headers["location"]
    content_id = content_id_from(location)
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    _second_user(client)
    assert api_post(client, f"/api/content/{content_id}/delete", {"hard": True}).status_code == 404
    assert api_post(client, f"/api/content/{content_id}/favorite", {"favorite": True}).status_code == 404
    assert api_post(client, f"/api/content/{content_id}/progress", {"chapter": 999}).status_code == 404
    assert api_post(client, f"/api/content/{content_id}/open").status_code == 404

    edit = client.post(
        f"/content/{content_id}/edit",
        data={
            "title": "Hijacked", "url": "https://example.org/victim", "category": "manga",
            "status": "reading", "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )
    assert edit.status_code == 404


def test_admin_page_is_restricted_to_admins(client):
    register(client, email="owner@example.com")  # first account is the admin
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    register(client, email="normal@example.com")  # second account is a plain user
    assert client.get("/admin").status_code == 403
    assert client.get("/api/system/status").status_code == 403
    assert "System" not in body(client.get("/"))  # admin nav link is hidden

    client.post("/logout", data={"csrf_token": csrf_token(client)})
    from tests.conftest import login

    login(client, email="owner@example.com")
    assert client.get("/admin").status_code == 200


def test_api_endpoints_require_a_session(client):
    for url in ["/api/library", "/api/notifications", "/api/search?q=x", "/api/export/preview"]:
        response = client.get(url, follow_redirects=False)
        assert response.status_code in (303, 401, 403), url


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def test_state_changing_post_without_csrf_is_rejected(auth_client):
    response = auth_client.post(
        "/add",
        data={"title": "No CSRF", "url": "https://example.org/x", "category": "manga", "status": "reading"},
    )
    assert response.status_code == 403
    assert "session expired" in body(response).lower()
    # Nothing was written.
    from sqlalchemy import func, select

    from app.models import Content

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Content.id))) == 0


def test_forged_csrf_token_is_rejected(auth_client):
    response = auth_client.post(
        "/add",
        data={
            "title": "Forged", "url": "https://example.org/forged", "category": "manga",
            "status": "reading", "csrf_token": "attacker.0000000000",
        },
    )
    assert response.status_code == 403


def test_csrf_token_is_bound_to_the_session(auth_client):
    row_secret = None
    from app.models import SessionToken

    with SessionLocal() as db:
        row_secret = db.query(SessionToken).first().csrf_secret
    assert csrf_token_for(row_secret)
    assert csrf_token_for("some-other-secret") != csrf_token_for(row_secret)


def test_get_requests_do_not_require_csrf(auth_client):
    assert auth_client.get("/library").status_code == 200


# ---------------------------------------------------------------------------
# XSS
# ---------------------------------------------------------------------------
def test_html_in_titles_is_escaped(auth_client):
    payload = "<script>alert('xss')</script>"
    location = add_item(auth_client, title=payload, url="https://example.org/xss").headers["location"]
    for url in ["/library", location, "/"]:
        text = auth_client.get(url).text
        assert "<script>alert" not in text
        # Angle brackets are neutralised on input, and any residual markup is escaped on output.
        assert "‹script›" in text or "&lt;script&gt;" in text


def test_script_tags_are_neutered_at_input(auth_client):
    location = add_item(auth_client, title="<img src=x onerror=alert(1)>", url="https://example.org/xss2").headers["location"]
    with SessionLocal() as db:
        from app.models import Content

        item = db.get(Content, content_id_from(location))
        assert "<img" not in item.title
        assert "onerror" in item.title  # text preserved, markup characters replaced


def test_notes_are_escaped_in_output(auth_client):
    location = add_item(auth_client, title="Notes Test", url="https://example.org/notes",
                        notes="<b>bold</b><script>steal()</script>").headers["location"]
    text = auth_client.get(location).text
    assert "<script>steal" not in text


def test_url_in_detail_page_is_escaped(auth_client):
    location = add_item(
        auth_client, title="URL Test",
        url='https://example.org/a"onmouseover="alert(1)',
    ).headers["location"]
    text = auth_client.get(location).text
    assert 'onmouseover="alert' not in text


# ---------------------------------------------------------------------------
# SQL injection
# ---------------------------------------------------------------------------
def test_sql_injection_in_search_is_treated_as_text(auth_client):
    add_item(auth_client, title="Normal Item", url="https://example.org/normal")
    hostile = "'; DROP TABLE content; --"
    response = auth_client.get(f"/search?q={hostile}")
    assert response.status_code == 200

    from sqlalchemy import func, select

    from app.models import Content

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Content.id))) == 1  # table survived


def test_sql_injection_in_title_is_stored_verbatim_as_text(auth_client):
    hostile = "Robert'); DROP TABLE users;--"
    location = add_item(auth_client, title=hostile, url="https://example.org/bobby").headers["location"]
    assert location.startswith("/content/")
    with SessionLocal() as db:
        assert db.query(User).count() >= 1


def test_sql_injection_in_tag_is_harmless(auth_client):
    add_item(auth_client, title="Tag Inject", url="https://example.org/taginject", tags="' OR 1=1 --")
    assert auth_client.get("/library").status_code == 200


# ---------------------------------------------------------------------------
# SSRF / URL policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.0.1/",
        "http://172.16.5.5/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://example.com:8080/",
        "http://example.com:22/",
    ],
)
def test_internal_and_dangerous_urls_are_rejected_by_the_fetcher(url):
    with pytest.raises(UnsafeUrlError):
        resolve_and_validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/",
        "http://10.1.2.3/",
        "javascript:alert(1)",
        "ftp://example.com/",
        "http://example.com:8080/x",
        "http://localhost/x",
    ],
)
def test_internal_urls_are_rejected_at_save_time(url):
    with pytest.raises(UrlValidationError):
        validate_url_syntax(url)


def test_scheme_is_added_when_missing():
    assert validate_url_syntax("example.com/series") == "https://example.com/series"


def test_overlong_url_is_rejected():
    with pytest.raises(UrlValidationError):
        validate_url_syntax("https://example.org/" + "a" * 3000)


def test_add_form_rejects_ssrf_urls(auth_client):
    response = add_item(auth_client, title="SSRF", url="http://169.254.169.254/latest/meta-data/")
    assert response.status_code == 400
    assert "internal" in body(response).lower() or "Invalid URL" in body(response)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_login_rate_limiting(client):
    """Per-IP throttling kicks in well before the account lock."""
    from app.config import settings

    blocked_at = None
    for attempt in range(settings.auth_rate_limit + 6):
        response = client.post(
            "/login",
            # Unknown address: only the IP rate limit applies here.
            data={"email": "nobody@nowhere.example", "password": "wrong", "csrf_token": csrf_token(client)},
        )
        if "Too many attempts" in body(response):
            blocked_at = attempt
            break
    assert blocked_at is not None, "rate limit never triggered"
    assert blocked_at == settings.auth_rate_limit


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_404_page_is_friendly(client):
    register(client)
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "couldn't find" in body(response)
    assert "Traceback" not in response.text


def test_unknown_content_id_returns_404(auth_client):
    assert auth_client.get("/content/999999").status_code == 404


def test_api_errors_return_json_not_html(auth_client):
    response = api_post(auth_client, "/api/content/999999/delete", {"hard": False})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_security_headers_are_present(client):
    register(client)
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_session_cookie_is_httponly_and_samesite(client):
    response = register(client)
    session_cookies = [
        value.decode("latin-1")
        for key, value in response.headers.raw
        if key == b"set-cookie" and value.startswith(b"pct_session=")
    ]
    assert len(session_cookies) == 1
    cookie = session_cookies[0]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie


def test_csrf_cookie_is_bound_to_the_new_session(client):
    """The CSRF cookie issued at login must match the session, not 'anonymous'."""
    response = register(client)
    csrf_cookies = [
        value.decode("latin-1")
        for key, value in response.headers.raw
        if key == b"set-cookie" and value.startswith(b"pct_csrf=")
    ]
    assert len(csrf_cookies) == 1, csrf_cookies
    assert not csrf_cookies[0].startswith("pct_csrf=anonymous.")


def test_no_api_docs_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_password_hash_is_not_exposed_anywhere(client):
    register(client, email="leak@example.com", password="Sup3rSecret!")
    for url in ["/profile", "/settings", "/admin", "/api/export/preview", "/export/json", "/backup"]:
        response = client.get(url)
        assert "$argon2" not in response.text, url
        assert "Sup3rSecret!" not in response.text, url


def test_hash_password_is_salted(client):
    first = hash_password("samepassword")
    second = hash_password("samepassword")
    assert first != second  # different salts
    assert verify_password("samepassword", first)
    assert verify_password("samepassword", second)
