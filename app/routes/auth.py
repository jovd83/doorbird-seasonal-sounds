"""Sign in and out.

The login POST is the one mutating endpoint that cannot sit behind
`require_auth`, so it carries its own CSRF token: the token is minted when the
login form is rendered, which also means a stale form left open across a
restart is rejected rather than silently accepted.
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.security import (
    SESSION_KEY,
    check_credentials,
    clear_login_failures,
    csrf_token,
    login_locked_out,
    record_login_failure,
    require_csrf,
    safe_next,
)
from app.shell import resolve_shell
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse, dependencies=[Depends(resolve_shell)])
async def login_form(request: Request, next: str = "/dashboard", error: str | None = None):
    # Minting here means the rendered form always carries a token that matches
    # the session cookie the response is about to set.
    csrf_token(request)
    return templates.TemplateResponse(
        request, "login.html", {"next": safe_next(next), "error": error}
    )


@router.post("/login", dependencies=[Depends(require_csrf)])
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
):
    target = safe_next(next)

    remaining = login_locked_out(request)
    if remaining:
        return _back_to_login(
            target,
            f"Too many failed sign-ins. Try again in {int(remaining) // 60 + 1} minute(s).",
        )

    if not check_credentials(username, password):
        record_login_failure(request)
        return _back_to_login(target, "Invalid credentials")

    clear_login_failures(request)
    # Replace the whole session on privilege change so a token that was handed
    # out before sign-in cannot be replayed afterwards.
    request.session.clear()
    request.session[SESSION_KEY] = username
    csrf_token(request)
    return RedirectResponse(target, status_code=303)


def _back_to_login(next_target: str, message: str) -> RedirectResponse:
    """Bounce to the login form with both values safely encoded."""
    return RedirectResponse(
        f"/login?next={quote(next_target, safe='/')}&error={quote(message, safe='')}",
        status_code=303,
    )


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request):
    """POST, not GET: signing someone out is a state change.

    As a GET it was triggerable by any third-party page that could get the
    browser to load a URL — an `<img>` tag was enough.
    """
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
