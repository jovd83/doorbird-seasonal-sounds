"""Test isolation.

This runs before any test module is imported, which is the only safe moment
to pin `DATA_DIR`: `app.config.settings` is built at import time, and the
SQLAlchemy engine is bound to whatever path it finds.

It must **assign**, not `setdefault`. Inside the container `DATA_DIR=/data`
is already exported, so a `setdefault` silently leaves it pointing at the live
database — which is exactly how an earlier run created a junk device and
rotated the real webhook token in production data.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="doorbird-tests-")

os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("SECRET_KEY", "test-secret")

if "FERNET_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["FERNET_KEY"] = Fernet.generate_key().decode()


def pytest_report_header(config) -> str:
    return f"doorbird: tests isolated to DATA_DIR={_TMP}"


# --------------------------------------------------------------- HTTP client


import re

from fastapi.testclient import TestClient


class FormClient(TestClient):
    """A TestClient that carries the CSRF token on form posts, like a browser.

    Every mutating route is behind `require_csrf`, so a bare `client.post(...)`
    is rejected the same way a cross-site forgery would be. Rather than thread
    a token through every call site, this mirrors what a browser does: read the
    hidden field out of a rendered page and submit it with the form.

    Pass `csrf=False` to post *without* a token -- that is how the CSRF
    protection itself is tested.
    """

    _csrf_token: str | None = None

    def refresh_csrf(self) -> str:
        """Re-read the token from a rendered page. Call after login/logout."""
        for page in ("/dashboard", "/login"):
            r = super().get(page, follow_redirects=True)
            found = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
            if found:
                self._csrf_token = found.group(1)
                return self._csrf_token
        raise AssertionError("no CSRF token found in any rendered page")

    def post(self, url, *, csrf: bool = True, **kwargs):
        if csrf:
            token = self._csrf_token or self.refresh_csrf()
            data = dict(kwargs.get("data") or {})
            data.setdefault("csrf_token", token)
            kwargs["data"] = data
        return super().post(url, **kwargs)

    def login(self, username: str = "admin", password: str = "test"):  # noqa: S107
        """Sign in and pick up the post-login session's token."""
        r = self.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        # The session is replaced on sign-in, so the old token is now stale.
        self.refresh_csrf()
        return r
