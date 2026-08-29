"""The template layer must not reach into the database.

`shell()` and `theme()` were Jinja globals that opened their own sessions
during rendering — three per page, ten on /settings, outside the request's
transaction and unmockable from a template test. Worse, it made the error page
depend on the database, so a database failure produced a second exception
instead of an error page.
"""
from __future__ import annotations

import pytest

import app.db as appdb
from app.db import init_db
from app.main import app
from app.shell import DEFAULT_SHELL, ShellState, shell_for
from app.templating import templates
from tests.conftest import FormClient as TestClient


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        c.login()
        yield c


@pytest.fixture
def session_counter(monkeypatch):
    """Count every Session this process opens."""
    original = appdb.SessionLocal
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(appdb, "SessionLocal", counting)
    return calls


PAGES = ["/dashboard", "/devices", "/schedules", "/auto-responses",
         "/mp3s", "/collections", "/audit", "/settings"]


@pytest.mark.parametrize("page", PAGES)
def test_a_page_render_opens_few_sessions(client, session_counter, page):
    """One for the request, one for the settings snapshot. Not one per key."""
    session_counter["n"] = 0
    assert client.get(page).status_code == 200
    assert session_counter["n"] <= 4, (
        f"{page} opened {session_counter['n']} sessions; the shell should need one snapshot")


def test_the_template_global_never_queries(session_counter):
    """`shell(request)` is an attribute read, not a lookup."""
    class _Req:
        class state:
            shell = ShellState(theme="light", trigger_label="x",
                               listener_tone="is-live", listener_label="y")

    session_counter["n"] = 0
    result = shell_for(_Req())
    assert result.theme == "light"
    assert session_counter["n"] == 0, "reading the shell hit the database"


def test_shell_falls_back_when_nothing_resolved_it(session_counter):
    """A render outside a request must still produce a page, not an error."""
    class _Req:
        class state:
            pass

    session_counter["n"] = 0
    assert shell_for(_Req()) is DEFAULT_SHELL
    assert session_counter["n"] == 0


def test_theme_is_no_longer_a_database_backed_global():
    """The old `theme()` global is gone; the theme rides on the shell."""
    assert "theme" not in templates.env.globals
    assert "shell" in templates.env.globals


def test_a_mutating_request_does_not_resolve_the_shell(client, session_counter):
    """POSTs redirect rather than render, so the snapshot would be wasted."""
    session_counter["n"] = 0
    r = client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 303
    # Only the write itself, not a shell read on top of it.
    assert session_counter["n"] <= 2, f"POST opened {session_counter['n']} sessions"


def test_the_rendered_theme_follows_the_stored_setting(client):
    client.post("/settings/theme", data={"theme": "light"}, follow_redirects=False)
    assert 'data-theme="light"' in client.get("/dashboard").text

    client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert 'data-theme="dark"' in client.get("/dashboard").text
