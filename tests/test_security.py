"""The browser trust boundary: redirects, CSRF, password handling, throttling.

Each of these covers a defect that was live before this suite existed, so they
are written to fail loudly if the protection is ever removed.
"""
from __future__ import annotations

import pytest

from app.db import init_db
from app.main import app
from app.passwords import InvalidHash, hash_password, looks_hashed, verify_password
from app.security import safe_next
from tests.conftest import FormClient as TestClient


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        c.login()
        yield c


# ------------------------------------------------------------- open redirect


@pytest.mark.parametrize("hostile", [
    "https://evil.example/pwn",
    "http://evil.example",
    "//evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
    "https://user:pw@evil.example/",
])
def test_login_refuses_an_offsite_redirect(hostile):
    """`next` lands in a Location header, so it may only ever be a local path."""
    assert safe_next(hostile) == "/dashboard"


@pytest.mark.parametrize("ok", ["/dashboard", "/devices", "/schedules?x=1", "/mp3s"])
def test_login_keeps_a_local_redirect(ok):
    assert safe_next(ok) == ok


def test_login_does_not_redirect_offsite_over_http():
    """The end-to-end version: the router must not emit an external Location."""
    init_db()
    with TestClient(app) as c:
        r = c.post(
            "/login",
            data={"username": "admin", "password": "test", "next": "https://evil.example/pwn"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard"


# --------------------------------------------------------------------- CSRF


def test_post_without_a_csrf_token_is_refused(client):
    r = client.post("/settings/theme", data={"theme": "light"},
                    csrf=False, follow_redirects=False)
    assert r.status_code == 400


def test_post_with_a_wrong_csrf_token_is_refused(client):
    r = client.post("/settings/theme", data={"theme": "light", "csrf_token": "nope"},
                    csrf=False, follow_redirects=False)
    assert r.status_code == 400


def test_post_with_the_right_csrf_token_succeeds(client):
    r = client.post("/settings/theme", data={"theme": "light"}, follow_redirects=False)
    assert r.status_code == 303


def test_every_rendered_form_carries_a_token(client):
    """A form without a token is a page that silently stopped working."""
    import re

    for page in ("/dashboard", "/devices", "/schedules", "/auto-responses",
                 "/mp3s", "/collections", "/audit", "/settings"):
        body = client.get(page).text
        forms = re.findall(r"<form\s[^>]*method=[\"']post[\"'][^>]*>", body, re.I)
        tokens = body.count('name="csrf_token"')
        assert len(forms) <= tokens, f"{page}: {len(forms)} POST forms but {tokens} tokens"


def test_logout_is_not_reachable_by_get(client):
    """As a GET it was triggerable by any third-party page with an <img> tag."""
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 405


def test_logout_by_post_clears_the_session():
    """Its own client: logging out would poison the module-scoped session."""
    init_db()
    with TestClient(app) as c:
        c.login()
        assert c.get("/dashboard", follow_redirects=False).status_code == 200
        assert c.post("/logout", follow_redirects=False).status_code == 303
        assert c.get("/dashboard", follow_redirects=False).status_code == 303


# ----------------------------------------------------------------- passwords


def test_password_round_trip():
    stored = hash_password("s3cret-passphrase")
    assert looks_hashed(stored)
    assert verify_password("s3cret-passphrase", stored)
    assert not verify_password("s3cret-passphras", stored)


def test_password_hash_is_salted():
    """Two hashes of the same password must differ, or the salt is not working."""
    assert hash_password("same") != hash_password("same")


def test_password_has_no_length_ceiling():
    """bcrypt caps input at 72 bytes and *raises*; scrypt has no such limit."""
    long_password = "x" * 500
    stored = hash_password(long_password)
    assert verify_password(long_password, stored)
    assert not verify_password("x" * 499, stored)


def test_a_malformed_stored_hash_is_rejected_not_ignored():
    with pytest.raises(InvalidHash):
        verify_password("anything", "not-a-hash")


def test_login_rejects_an_overlong_password_without_crashing(client):
    """A 500 here would be an unauthenticated denial-of-service."""
    r = client.post(
        "/login",
        data={"username": "admin", "password": "x" * 5000},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------- throttling


def test_repeated_failures_lock_the_caller_out():
    from app.security import MAX_FAILED_LOGINS, clear_login_failures

    init_db()
    with TestClient(app) as c:
        for _ in range(MAX_FAILED_LOGINS):
            c.post("/login", data={"username": "admin", "password": "wrong"},
                   follow_redirects=False)

        # Even the correct password is refused while the lockout stands.
        r = c.post("/login", data={"username": "admin", "password": "test"},
                   follow_redirects=False)
        assert "Too+many" in r.headers["location"] or "Too%20many" in r.headers["location"]

        # Do not leave the lockout in place for the next test in this process.
        class _Req:
            client = type("C", (), {"host": "testclient"})()

        clear_login_failures(_Req())


# ------------------------------------------------------- the error page itself


def test_the_error_page_does_not_touch_the_database():
    """The one page whose job is to work when things are broken.

    It used to extend base.html, whose `shell()` and `theme()` both query the
    database. With the database unavailable, rendering the error page raised a
    second exception, the original error vanished behind it, and the caller got
    a raw ASGI crash instead of an explanation.
    """
    from sqlalchemy import create_engine

    import app.db as appdb

    init_db()
    with TestClient(app, raise_server_exceptions=False) as c:
        # Break the database only *after* startup, so this exercises rendering
        # rather than the lifespan.
        broken = create_engine(
            "sqlite:////nonexistent-directory/nope.sqlite",
            connect_args={"check_same_thread": False},
        )
        original_bind = appdb.SessionLocal.kw.get("bind")
        appdb.SessionLocal.configure(bind=broken)
        try:
            r = c.get("/definitely-not-a-page")
            assert r.status_code == 404
            assert "<html" in r.text.lower(), "no page was rendered at all"
            assert "Traceback" not in r.text
            assert "Not found" in r.text
        finally:
            appdb.SessionLocal.configure(bind=original_bind)


def test_the_error_page_carries_no_shell_chrome():
    """No sidebar means no shell() call means no query."""
    init_db()
    with TestClient(app) as c:
        c.login()
        body = c.get("/definitely-not-a-page").text
    assert "Not found" in body
    assert 'class="sidebar"' not in body
    assert "/logout" not in body
