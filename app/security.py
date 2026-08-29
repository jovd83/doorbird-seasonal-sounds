"""Authentication, CSRF and redirect safety for the web UI.

Three separate concerns that all guard the same trust boundary — the browser —
so they live together:

* **Credentials.** The admin password is never compared as plaintext. Whatever
  the deployment supplies (a scrypt hash, or a plaintext password for backwards
  compatibility) is normalised to a hash once at startup, and every login goes
  through a constant-time verify. See `app.passwords`.
* **CSRF.** Every mutating form carries a per-session token. `SameSite=lax`
  already blocks the cross-site cookie on a top-level POST in current browsers,
  but that is a browser policy, not something this app controls, so it is not
  relied on as the only defence.
* **Redirect safety.** `?next=` is attacker-controllable and ends up in a
  `Location` header, so it is restricted to same-site paths.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

from fastapi import HTTPException, Request, status
from markupsafe import Markup

from app.config import settings
from app.passwords import InvalidHash, hash_password, looks_hashed, verify_password

log = logging.getLogger("doorbird.security")

SESSION_KEY = "authed_user"
CSRF_SESSION_KEY = "csrf_token"
CSRF_FIELD = "csrf_token"

# The placeholder shipped in .env.example. Booting with it still set means the
# deployment was never configured, and the web UI is effectively open.
_PLACEHOLDER_PASSWORDS = frozenset({"change-me-please", "changeme", "admin", "password"})

# Login throttling. A single shared admin credential is exactly the shape that
# invites online brute force, and this app is often published to a LAN.
MAX_FAILED_LOGINS = 8
LOCKOUT_SECONDS = 300.0

HASH_COMMAND = "python -m app.hash_password"


def _resolve_password_hash() -> str:
    """The scrypt hash every login is verified against.

    `ADMIN_PASSWORD_HASH` is preferred. A plaintext `ADMIN_PASSWORD` is still
    accepted -- every existing install has one -- but it is hashed here at
    startup so the plaintext never participates in a comparison.
    """
    configured = settings.admin_password_hash.strip()
    if configured:
        if not looks_hashed(configured):
            raise RuntimeError(
                "ADMIN_PASSWORD_HASH does not look like a hash. Generate one with "
                f"`{HASH_COMMAND}`, or use ADMIN_PASSWORD for a plaintext password."
            )
        return configured

    plaintext = settings.admin_password
    if not plaintext:
        raise RuntimeError(
            "No admin credentials configured. Set ADMIN_PASSWORD_HASH (preferred) "
            f"or ADMIN_PASSWORD in your .env. Generate a hash with `{HASH_COMMAND}`."
        )
    if plaintext.lower() in _PLACEHOLDER_PASSWORDS:
        raise RuntimeError(
            f"ADMIN_PASSWORD is still the example placeholder ({plaintext!r}). "
            "Set a real password before starting the app -- the web UI controls "
            "your door stations."
        )
    log.warning(
        "ADMIN_PASSWORD is set as plaintext; hashing it in memory. Prefer "
        "ADMIN_PASSWORD_HASH (`%s`) so no plaintext password sits in the "
        "environment or in `docker inspect` output.", HASH_COMMAND,
    )
    return hash_password(plaintext)


_password_hash = _resolve_password_hash()

# device/IP -> (failure count, moment the window started)
_failed_logins: dict[str, tuple[int, float]] = {}
_failed_lock = threading.Lock()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def login_locked_out(request: Request) -> float:
    """Seconds remaining on a lockout, or 0.0 when the caller may try again."""
    key = _client_key(request)
    with _failed_lock:
        count, started = _failed_logins.get(key, (0, 0.0))
        if count < MAX_FAILED_LOGINS:
            return 0.0
        remaining = LOCKOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            _failed_logins.pop(key, None)
            return 0.0
        return remaining


def record_login_failure(request: Request) -> None:
    key = _client_key(request)
    now = time.monotonic()
    with _failed_lock:
        count, started = _failed_logins.get(key, (0, now))
        if now - started > LOCKOUT_SECONDS:
            count, started = 0, now
        _failed_logins[key] = (count + 1, started)


def clear_login_failures(request: Request) -> None:
    with _failed_lock:
        _failed_logins.pop(_client_key(request), None)


def is_authed(request: Request) -> bool:
    return request.session.get(SESSION_KEY) == settings.admin_username


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={safe_next(request.url.path)}"},
        )


def check_credentials(username: str, password: str) -> bool:
    """Constant-time on both fields, and always does the hashing work.

    The hash is verified even when the username is wrong, so a bad username and
    a bad password take the same time to answer and the endpoint cannot be used
    to discover the admin's name.
    """
    user_ok = secrets.compare_digest(username, settings.admin_username)
    try:
        password_ok = verify_password(password, _password_hash)
    except InvalidHash as exc:
        # A stored hash we cannot parse must refuse every login, not accept one.
        log.error("stored admin password hash is unusable (%s); refusing login", exc)
        return False
    return user_ok and password_ok


# ------------------------------------------------------------------ redirects


def safe_next(value: str | None, fallback: str = "/dashboard") -> str:
    """Restrict a caller-supplied redirect target to this site.

    `next` reaches us from a query string or a form field and is written
    straight into a `Location` header, so anything that a browser would resolve
    against another origin has to be rejected: absolute URLs, protocol-relative
    `//host`, and the `/\\host` form that several browsers normalise to it.
    """
    candidate = (value or "").strip()
    if not candidate.startswith("/"):
        return fallback
    if candidate.startswith(("//", "/\\", "/%2f", "/%5c")):
        return fallback
    if candidate.lower().startswith(("/%2F".lower(), "/%5C".lower())):
        return fallback
    return candidate


# ---------------------------------------------------------------------- CSRF


def csrf_token(request: Request) -> str:
    """The session's CSRF token, minted on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_input(request: Request) -> Markup:
    """Hidden form field carrying the token. Used as `{{ csrf_input(request) }}`."""
    # The token is `secrets.token_urlsafe`, so it is already URL-safe base64
    # with nothing to escape, and CSRF_FIELD is a module constant.
    return Markup(  # noqa: S704
        f'<input type="hidden" name="{CSRF_FIELD}" value="{csrf_token(request)}">'
    )


async def require_csrf(request: Request) -> None:
    """Reject a mutating request whose form token does not match the session.

    Only unsafe methods are checked, so a router can declare this once and GET
    handlers are unaffected. The token is read from the parsed form; FastAPI
    caches the body, so reading it here does not consume it for the handler.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    if not expected:
        raise HTTPException(400, "Your session has expired — reload the page and try again.")

    try:
        form = await request.form()
    except Exception as exc:
        log.warning("CSRF check could not read the form body: %s", exc)
        raise HTTPException(400, "Malformed form submission.") from exc

    supplied = form.get(CSRF_FIELD)
    if not isinstance(supplied, str) or not secrets.compare_digest(supplied, expected):
        log.warning("CSRF token mismatch on %s %s", request.method, request.url.path)
        raise HTTPException(
            400,
            "That form could not be verified — it may have been left open too long. "
            "Reload the page and try again.",
        )
