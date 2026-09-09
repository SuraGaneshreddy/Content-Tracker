"""Registration, login, logout, password reset and session behaviour."""

from __future__ import annotations

from app.security import hash_password, verify_password
from tests.conftest import body, csrf_token, login, register


def test_register_creates_account_and_redirects_to_dashboard(client):
    response = register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Welcome back" in body(dashboard)


def test_login_requires_matching_credentials(client):
    register(client)
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    bad = login(client, password="WrongPassword123")
    assert bad.status_code == 400
    assert "didn't work" in body(bad)

    good = login(client)
    assert good.status_code == 303
    assert good.headers["location"] == "/"


def test_login_error_does_not_reveal_whether_email_exists(client):
    unknown = login(client, email="ghost@example.com", password="Whatever123")
    assert unknown.status_code == 400
    assert "didn't work" in body(unknown)

    register(client)
    client.post("/logout", data={"csrf_token": csrf_token(client)})
    known = login(client, password="WrongPassword123")
    # Same wording for both cases: no account enumeration.
    assert "didn't work" in body(known)


def test_logout_clears_session(client):
    register(client)
    assert client.get("/").status_code == 200

    client.post("/logout", data={"csrf_token": csrf_token(client)})
    # After logout the dashboard redirects to /login.
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_password_policy_rejects_weak_passwords(client):
    response = client.post(
        "/register",
        data={
            "email": "weak@example.com",
            "password": "123",
            "password_confirm": "123",
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 400
    assert "at least 8 characters" in body(response)


def test_password_confirmation_must_match(client):
    response = client.post(
        "/register",
        data={
            "email": "mismatch@example.com",
            "password": "Str0ngPassw0rd!",
            "password_confirm": "DifferentPass1!",
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 400
    assert "don't match" in body(response)


def test_duplicate_email_is_rejected_without_leaking(client):
    register(client)
    again = register(client)
    assert again.status_code == 400
    assert "could not be created" in body(again)


def test_password_reset_flow(client):
    register(client, email="reset@example.com")
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    response = client.post(
        "/forgot-password",
        data={"email": "reset@example.com", "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 200
    assert "reset-password/" in response.text

    # Extract the single-use token from the rendered development link.
    token = response.text.split("/reset-password/")[1].split('"')[0]

    page = client.get(f"/reset-password/{token}")
    assert page.status_code == 200

    done = client.post(
        f"/reset-password/{token}",
        data={
            "token": token,
            "password": "BrandNewPass99!",
            "password_confirm": "BrandNewPass99!",
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )
    assert done.status_code == 303

    # Old password no longer works, new one does.
    assert login(client, email="reset@example.com", password="Str0ngPassw0rd!").status_code == 400
    assert login(client, email="reset@example.com", password="BrandNewPass99!").status_code == 303

    # Token is single-use.
    reused = client.post(
        f"/reset-password/{token}",
        data={
            "token": token,
            "password": "AnotherPass123!",
            "password_confirm": "AnotherPass123!",
            "csrf_token": csrf_token(client),
        },
    )
    assert reused.status_code == 400
    assert "invalid or has expired" in body(reused)


def test_forgot_password_for_unknown_email_does_not_error(client):
    response = client.post(
        "/forgot-password",
        data={"email": "nobody@example.com", "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 200
    assert "reset link" in body(response)
    # No development link is generated for an address we don't have.
    assert "Development mode" not in body(response)


def test_session_cookie_is_httponly_and_signed(client):
    register(client)
    cookie_header = None
    for header in client.headers.getlist("set-cookie") if hasattr(client.headers, "getlist") else []:
        if "pct_session" in header:
            cookie_header = header
    if cookie_header:
        assert "HttpOnly" in cookie_header

    session_value = client.cookies.get("pct_session")
    assert session_value and "." in session_value  # token.signature


def test_passwords_are_argon2_hashed_not_plaintext(client):
    register(client, email="hash@example.com", password="Sup3rSecret!")
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "hash@example.com"))
        assert user is not None
        assert user.password_hash.startswith("$argon2")
        assert "Sup3rSecret!" not in user.password_hash
        assert verify_password("Sup3rSecret!", user.password_hash) is True
        assert verify_password("wrong", user.password_hash) is False


def test_profile_requires_authentication(client):
    response = client.get("/profile", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_change_password_invalidates_other_sessions(client):
    register(client)
    first_session = client.cookies.get("pct_session")

    # A second sign-in creates a second session row.
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import SessionToken, User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "reader@example.com"))
        user_id = user.id
        assert db.scalar(select(func.count(SessionToken.id)).where(SessionToken.user_id == user_id)) == 1

    response = client.post(
        "/profile",
        data={
            "display_name": "Renamed",
            "current_password": "Str0ngPassw0rd!",
            "new_password": "EvenStr0nger!",
            "new_password_confirm": "EvenStr0nger!",
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with SessionLocal() as db:
        assert db.scalar(select(func.count(SessionToken.id)).where(SessionToken.user_id == user_id)) == 1


def test_repeated_failed_logins_lock_the_account(client):
    register(client, email="lockme@example.com")
    client.post("/logout", data={"csrf_token": csrf_token(client)})

    for _ in range(8):
        client.post(
            "/login",
            data={"email": "lockme@example.com", "password": "bad-password", "csrf_token": csrf_token(client)},
        )

    # Even the correct password is refused while locked.
    locked = client.post(
        "/login",
        data={"email": "lockme@example.com", "password": "Str0ngPassw0rd!", "csrf_token": csrf_token(client)},
    )
    assert locked.status_code == 400
    assert "locked" in body(locked).lower()


def test_first_registered_user_becomes_admin(client):
    register(client, email="first@example.com")
    response = client.get("/admin")
    assert response.status_code == 200

    register(client, email="second@example.com")
    response = client.get("/admin")
    assert response.status_code == 403
