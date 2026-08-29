"""Runtime settings the user can change from the web UI.

`app.config.settings` holds deployment defaults from the environment. These
are the ones that have to be switchable while the app is running — chiefly
which ring trigger is in use, since changing that starts or stops listener
threads.

Every getter falls back to the corresponding `.env` value, so an install that
never opens the settings page behaves exactly as its environment says.
"""
from __future__ import annotations

import secrets

from app.config import settings
from app.db import session_scope
from app.models import AppSetting

# --- keys ---------------------------------------------------------------
TRIGGER_MODE = "trigger_mode"
WEBHOOK_TOKEN = "webhook_token"  # noqa: S105 - a settings key name, not a secret
WEBHOOK_FORWARD = "webhook_forward_url"
PUBLIC_BASE_URL = "public_base_url"
PLAY_DEFAULT_IDLE = "play_default_when_idle"
THEME = "theme"

# --- trigger modes ------------------------------------------------------
MODE_MONITOR = "monitor"    # we hold monitor.cgi open (default; changes nothing on the device)
MODE_WEBHOOK = "webhook"    # something else on the LAN calls our /ring URL
MODE_BOTH = "both"          # belt and braces; the shared debounce stops double chimes

TRIGGER_MODES: dict[str, str] = {
    MODE_MONITOR: "Listen for ring events (default)",
    MODE_WEBHOOK: "External trigger — another system calls this app",
    MODE_BOTH: "Both — whichever arrives first wins",
}

# --- appearance ---------------------------------------------------------
THEME_DARK = "dark"
THEME_LIGHT = "light"
THEMES = (THEME_DARK, THEME_LIGHT)


Snapshot = dict[str, str]


def snapshot() -> Snapshot:
    """Every runtime setting in one read.

    The per-key getters each open their own session. That is fine for a one-off
    write, but the shell renders on every page and the settings page reads a
    dozen keys, which measured at three and ten extra sessions per render
    respectively. The table holds a handful of rows, so fetching all of them
    once costs less than fetching two of them twice.
    """
    with session_scope() as db:
        return {row.key: row.value for row in db.query(AppSetting).all()}


def get(key: str, default: str = "", snap: Snapshot | None = None) -> str:
    """One setting. Reads from `snap` when given, otherwise opens a session."""
    if snap is not None:
        return snap.get(key, default)
    with session_scope() as db:
        row = db.get(AppSetting, key)
        return row.value if row is not None else default


def set_value(key: str, value: str) -> None:
    with session_scope() as db:
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value


def trigger_mode(snap: Snapshot | None = None) -> str:
    """Which ring trigger is active.

    Defaults to the monitor stream when ring chime is on at all. An install
    with `RING_CHIME_ENABLED=0` reports `monitor` here but never starts a
    listener — `app.ring.start()` makes that call.
    """
    mode = get(TRIGGER_MODE, MODE_MONITOR, snap)
    return mode if mode in TRIGGER_MODES else MODE_MONITOR


def set_trigger_mode(mode: str) -> str:
    if mode not in TRIGGER_MODES:
        raise ValueError(f"unknown trigger mode {mode!r}")
    set_value(TRIGGER_MODE, mode)
    return mode


def webhook_token(snap: Snapshot | None = None) -> str:
    """The shared secret in the ring webhook URL, generated once on demand.

    The DoorBird cannot present a session cookie, so the URL itself is the
    credential. It is only ever handed to the device over the LAN.
    """
    token = get(WEBHOOK_TOKEN, "", snap)
    if not token:
        token = secrets.token_urlsafe(24)
        set_value(WEBHOOK_TOKEN, token)
    return token


def rotate_webhook_token() -> str:
    token = secrets.token_urlsafe(24)
    set_value(WEBHOOK_TOKEN, token)
    return token


def monitor_enabled() -> bool:
    return settings.ring_chime_enabled and trigger_mode() in (MODE_MONITOR, MODE_BOTH)


def webhook_enabled() -> bool:
    return settings.ring_chime_enabled and trigger_mode() in (MODE_WEBHOOK, MODE_BOTH)


def webhook_forward_url(snap: Snapshot | None = None) -> str:
    """Optional URL the ring webhook relays to, verbatim, before chiming.

    Chaining aid for installs where this app sits in front of something else.
    Note that displacing an existing doorbell HTTP favourite is *not* a
    supported setup here — see the Settings page. This is for the case where
    you deliberately want one ring to fan out to a second system.
    """
    return get(WEBHOOK_FORWARD, "", snap).strip()


def set_webhook_forward_url(url: str) -> None:
    set_value(WEBHOOK_FORWARD, validate_forward_url(url))


def validate_forward_url(url: str) -> str:
    """Check a relay target before it is stored. Returns the cleaned value.

    This URL is fetched by the server on every ring, so it is an outbound
    request the app makes on someone else's say-so. It was previously stored
    and dialled verbatim. Restricting it to http(s) with a real host stops the
    obvious mistakes -- `file://`, a bare hostname, a typo'd scheme -- from
    turning into a confusing failure once a quarter when the doorbell rings.
    """
    from urllib.parse import urlsplit

    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"the forward URL must start with http:// or https:// (got {parsed.scheme or 'no scheme'})")
    if not parsed.hostname:
        raise ValueError("the forward URL has no host")
    return cleaned


# Hosts that mean "you are looking at this from inside the container or from
# the Docker host itself" — useless in a URL handed to another machine.
# A reject-list of addresses that mean "here", not something we bind to.
_INTERNAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")  # noqa: S104


def public_base_url(snap: Snapshot | None = None) -> str:
    """Base URL other systems on the LAN should use to reach this app.

    Auto-detection is not reliable here. This app normally runs in a container
    that listens on 8080 internally while being published on a different host
    port, so neither the container's own address (a 172.x bridge IP) nor the
    port it binds tells you what a caller should dial. The admin sets it once
    and every generated URL uses it.
    """
    return get(PUBLIC_BASE_URL, "", snap).strip().rstrip("/")


def set_public_base_url(url: str) -> None:
    set_value(PUBLIC_BASE_URL, (url or "").strip().rstrip("/"))


def looks_unreachable(url: str) -> bool:
    """True if this URL would not work when dialled from another machine."""
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    return not host or host in _INTERNAL_HOSTS or host.startswith("172.1")


def play_default_when_idle(snap: Snapshot | None = None) -> bool:
    """Whether to play the default MP3 on days no schedule matches.

    On by default, which is the behaviour every install had before this became
    configurable. Turned off, a ring outside every schedule window produces no
    audio at all from this app -- the door station's own built-in chime is then
    the only thing the visitor hears.
    """
    return get(PLAY_DEFAULT_IDLE, "1", snap) != "0"


def set_play_default_when_idle(enabled: bool) -> None:
    set_value(PLAY_DEFAULT_IDLE, "1" if enabled else "0")


def theme(snap: Snapshot | None = None) -> str:
    """Which colour scheme the web UI renders in.

    Dark by default: this is a wall-mounted-tablet and phone app that mostly
    gets opened in a hallway, and the interface is designed dark-first. Stored
    per install rather than per browser so a tablet and a phone agree.
    """
    value = get(THEME, THEME_DARK, snap)
    return value if value in THEMES else THEME_DARK


def set_theme(value: str) -> str:
    if value not in THEMES:
        raise ValueError(f"unknown theme {value!r}")
    set_value(THEME, value)
    return value
