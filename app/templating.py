from datetime import date, datetime

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import settings

templates = Jinja2Templates(directory="app/templates")

# Line icons on a 24-box grid, uniform 1.5 stroke, never filled. Held here
# rather than in the templates so a glyph is defined once and referenced by
# name -- `{{ icon("bell") }}` -- from anywhere.
ICONS: dict[str, str] = {
    "gauge":   '<path d="M3 13.5h3.2l2.3-5.4 3 10.2 2.6-7.1 1.8 2.3H21"/>',
    "station": '<rect x="6" y="2.8" width="12" height="18.4" rx="3.4"/>'
               '<circle cx="12" cy="9" r="2.6"/><path d="M9.6 16.6h4.8"/>',
    "bell":    '<path d="M6.4 10.2a5.6 5.6 0 0 1 11.2 0c0 4 1.4 5.4 1.4 5.4H5s1.4-1.4 1.4-5.4Z"/>'
               '<path d="M10.2 19a2 2 0 0 0 3.6 0"/>',
    "speech":  '<path d="M20 14.4a2.6 2.6 0 0 1-2.6 2.6H8.2L4 20.4V5.6A2.6 2.6 0 0 1 6.6 3h10.8'
               'A2.6 2.6 0 0 1 20 5.6Z"/><path d="M8.6 10v1.6M11.5 8v5.6M14.4 9.2v3.2M17 10.4v.8"/>',
    "wave":    '<path d="M3 11v2M6.6 8.4v7.2M10.2 5.4v13.2M13.8 8v8M17.4 10v4M21 11.4v1.2"/>',
    "shuffle": '<path d="M3.6 6.6h3.6L16.8 17.4h3.6"/><path d="M17.6 4.2 20.4 6.6l-2.8 2.4"/>'
               '<path d="M17.6 14.8l2.8 2.6-2.8 2.4"/><path d="M3.6 17.4h3.6l2.6-3"/>',
    "log":     '<rect x="4" y="3.2" width="16" height="17.6" rx="2.6"/>'
               '<path d="M8 8.2h8M8 12h8M8 15.8h4.8"/>',
    "sliders": '<path d="M4 7.6h9M17.4 7.6H20M4 16.4h3.2M11.6 16.4H20"/>'
               '<circle cx="15.2" cy="7.6" r="2.2"/><circle cx="9.4" cy="16.4" r="2.2"/>',
    "check":   '<path d="M4.8 12.6 9.4 17 19.2 7"/>',
    "x":       '<path d="M6.4 6.4 17.6 17.6M17.6 6.4 6.4 17.6"/>',
    "minus":   '<path d="M5.2 12h13.6"/>',
    "alert":   '<path d="M12 4.4 21 19.6H3Z"/><path d="M12 10v4.2"/>'
               '<circle cx="12" cy="17.2" r=".5" fill="currentColor" stroke="none"/>',
    "info":    '<circle cx="12" cy="12" r="8.6"/><path d="M12 11.2v5"/>'
               '<circle cx="12" cy="8.2" r=".55" fill="currentColor" stroke="none"/>',
    "plus":    '<path d="M12 5.2v13.6M5.2 12h13.6"/>',
    "download": '<path d="M12 3.6v11.2M7.6 10.6 12 15l4.4-4.4"/><path d="M4.4 18.4h15.2"/>',
    "upload":  '<path d="M12 15.4V4.2M7.6 8.6 12 4.2l4.4 4.4"/><path d="M4.4 19.2h15.2"/>',
    "trash":   '<path d="M4.6 6.6h14.8"/><path d="M9.4 6.6V4.8h5.2v1.8"/>'
               '<path d="M6.6 6.6 7.6 20h8.8l1-13.4"/><path d="M10.4 10.2v6M13.6 10.2v6"/>',
    "play":    '<path d="M8.4 5.6 18.4 12 8.4 18.4Z"/>',
    "chev-r":  '<path d="M9.4 5.6 15.8 12l-6.4 6.4"/>',
    "chev-d":  '<path d="M5.6 9.4 12 15.8l6.4-6.4"/>',
    "ext":     '<path d="M13.4 4.6H19.4v6"/><path d="M19.4 4.6 11 13"/>'
               '<path d="M17.6 13.6v4.4a1.8 1.8 0 0 1-1.8 1.8H6a1.8 1.8 0 0 1-1.8-1.8V8.2'
               'A1.8 1.8 0 0 1 6 6.4h4.4"/>',
    "search":  '<circle cx="10.8" cy="10.8" r="6.2"/><path d="M15.4 15.4 20 20"/>',
    "refresh": '<path d="M19.6 12a7.6 7.6 0 1 1-2.3-5.4"/><path d="M19.8 3.6v4.6h-4.6"/>',
    "clock":   '<circle cx="12" cy="12" r="8.6"/><path d="M12 7v5.3l3.4 2"/>',
    "cal":     '<rect x="3.6" y="5.2" width="16.8" height="15.2" rx="2.6"/>'
               '<path d="M3.6 10h16.8M8.4 3.2v4M15.6 3.2v4"/>',
    "key":     '<circle cx="8" cy="12" r="3.6"/><path d="M11.6 12H21M18 12v3.2M15 12v2.4"/>',
    "power":   '<path d="M12 3.6v8"/><path d="M17.4 6.6a7.6 7.6 0 1 1-10.8 0"/>',
    "more":    '<circle cx="5.6" cy="12" r="1.1" fill="currentColor" stroke="none"/>'
               '<circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/>'
               '<circle cx="18.4" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
    "arrow-r": '<path d="M4.6 12h14.4M13.4 6.4 19 12l-5.6 5.6"/>',
    "copy":    '<rect x="8.4" y="8.4" width="11.2" height="11.2" rx="2.2"/>'
               '<path d="M15.6 5.6H6.6a2.2 2.2 0 0 0-2.2 2.2v9"/>',
    "moon":    '<path d="M20.4 14.2A8.6 8.6 0 0 1 9.8 3.6a8.6 8.6 0 1 0 10.6 10.6Z"/>',
    "sun":     '<circle cx="12" cy="12" r="4.2"/>'
               '<path d="M12 2.6v2.4M12 19v2.4M4.4 12H2M22 12h-2.4M6.3 6.3 4.6 4.6'
               'M19.4 19.4l-1.7-1.7M17.7 6.3l1.7-1.7M4.6 19.4l1.7-1.7"/>',
}


def _icon(name: str, size: int = 20, cls: str = "") -> Markup:
    """Inline SVG for one named glyph. Colour comes from `currentColor`."""
    body = ICONS.get(name)
    if body is None:
        raise KeyError(f"unknown icon {name!r}")
    classes = f"ic {cls}".strip()
    # Markup is safe here: `body` comes from the ICONS dict above, and `size`
    # and `cls` are template literals written by us, never request data.
    return Markup(  # noqa: S704
        f'<svg class="{classes}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>'
    )


def _ringmark(size: int = 104, tone: str = "live", glyph: str = "bell") -> Markup:
    """Concentric-ring sound mark: three rings and a glyph, all currentColor."""
    cx = size / 2
    r1, r2, r3 = size * 0.455, size * 0.335, size * 0.215
    dash = ' stroke-dasharray="5 6"' if tone == "alert" else ""
    o1, o2, o3 = (0.32, 0.45, 0.6) if tone == "idle" else (0.30, 0.55, 0.9)
    # Every interpolated value here is a number or one of our own tone names.
    return Markup(  # noqa: S704
        f'<span class="ringmark is-{tone}" style="width:{size}px;height:{size}px">'
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" fill="none" '
        f'aria-hidden="true" focusable="false">'
        f'<circle cx="{cx}" cy="{cx}" r="{r1}" stroke="currentColor" stroke-width="1.25" opacity="{o1}"{dash}/>'
        f'<circle cx="{cx}" cy="{cx}" r="{r2}" stroke="currentColor" stroke-width="1.4" opacity="{o2}"{dash}/>'
        f'<circle cx="{cx}" cy="{cx}" r="{r3}" stroke="currentColor" stroke-width="1.6" opacity="{o3}"/>'
        f'</svg>'
        f'<span class="glyph">{_icon(glyph, round(size * 0.235))}</span>'
        f'</span>'
    )


# The left sidebar and the mobile bar render from one list, so a page can never
# go missing from one of them.
NAV: list[dict[str, str]] = [
    {"key": "dashboard",   "label": "Dashboard",       "short": "Dashboard",  "href": "/dashboard",      "icon": "gauge"},
    {"key": "devices",     "label": "Devices",         "short": "Devices",    "href": "/devices",        "icon": "station"},
    {"key": "chimes",      "label": "Chime schedules", "short": "Chimes",     "href": "/schedules",      "icon": "bell"},
    {"key": "auto",        "label": "Auto responses",  "short": "Auto",       "href": "/auto-responses", "icon": "speech"},
    {"key": "mp3s",        "label": "MP3s",            "short": "MP3s",       "href": "/mp3s",           "icon": "wave"},
    {"key": "collections", "label": "Collections",     "short": "Sets",       "href": "/collections",    "icon": "shuffle"},
    {"key": "audit",       "label": "Audit",           "short": "Audit",      "href": "/audit",          "icon": "log"},
    {"key": "settings",    "label": "Settings",        "short": "Settings",   "href": "/settings",       "icon": "sliders"},
]

# Eight entries do not fit a bottom bar at a 44px hit target, so mobile keeps
# four and files the rest under "More".
MOBILE_TABS = [n for n in NAV if n["key"] in ("dashboard", "devices", "chimes", "auto")]
MORE_TABS = [n for n in NAV if n["key"] in ("mp3s", "collections", "audit", "settings")]
MORE_KEYS = [n["key"] for n in MORE_TABS]


def _shell(request):
    """Shell state for `base.html`, read off the request.

    Resolved by the `resolve_shell` dependency before the handler ran, so this
    is a plain attribute read. It used to query the database from inside the
    render -- see app/shell.py for why that had to stop.
    """
    from app.shell import shell_for

    return shell_for(request)


def _csrf_input(request):
    """Hidden CSRF field for a form. Imported lazily to avoid an import cycle.

    `app.security` imports `app.config`, and every route module imports both
    this module and that one; keeping the import inside the call keeps the
    template layer out of that graph at import time.
    """
    from app.security import csrf_input

    return csrf_input(request)


def _fmt_dt(value):
    """Timestamps are stored as local wall-clock time, so print them as-is."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _fmt_md(month: int | None, day: int | None) -> str:
    if month is None or day is None:
        return "—"
    return f"{month:02d}-{day:02d}"


def _fmt_hhmm(minutes: int | None, fallback: str = "") -> str:
    """Minutes since midnight -> 'HH:MM', for populating <input type=time>."""
    if minutes is None:
        return fallback
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_until(value, reference=None) -> str:
    """'in 3 days, 4h' — how far off a timestamp is, in words.

    Sits next to the absolute timestamp rather than replacing it: the exact
    minute is what you check a schedule against, the rough distance is what
    tells you at a glance whether it matters today.
    """
    if not isinstance(value, datetime):
        return ""
    now = reference if isinstance(reference, datetime) else datetime.now()
    delta = value - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"in {days} day{'s' if days != 1 else ''}" + (f", {hours}h" if hours else "")
    if hours:
        return f"in {hours}h" + (f" {minutes}m" if minutes else "")
    return f"in {minutes}m" if minutes else "in under a minute"


def _fmt_since(value, reference=None) -> str:
    """'17 minutes ago' — how long back a timestamp is, in words.

    The mirror of `until`, for the dashboard's last-ring figure. Sits under the
    absolute time rather than replacing it: "3d 13h ago" is what tells you at a
    glance that something has stopped reporting.
    """
    if not isinstance(value, datetime):
        return ""
    now = reference if isinstance(reference, datetime) else datetime.now()
    seconds = int((now - value).total_seconds())
    if seconds < 0:
        return "just now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return "just now"


def _hours_since(value, reference=None) -> float:
    """Age of a timestamp in hours, for deciding when a ring has gone stale."""
    if not isinstance(value, datetime):
        return 0.0
    now = reference if isinstance(reference, datetime) else datetime.now()
    return (now - value).total_seconds() / 3600.0


def _fmt_window(schedule) -> str:
    if schedule.all_day:
        return "all day"
    return f"{_fmt_hhmm(schedule.start_minute, '00:00')}–{_fmt_hhmm(schedule.end_minute, '23:59')}"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["md"] = _fmt_md
templates.env.filters["hhmm"] = _fmt_hhmm
templates.env.filters["window"] = _fmt_window
templates.env.filters["until"] = _fmt_until
templates.env.filters["since"] = _fmt_since
templates.env.filters["hours_since"] = _hours_since
templates.env.globals["tz_name"] = settings.timezone
templates.env.globals["icon"] = _icon
templates.env.globals["ringmark"] = _ringmark
templates.env.globals["NAV"] = NAV
templates.env.globals["MOBILE_TABS"] = MOBILE_TABS
templates.env.globals["MORE_TABS"] = MORE_TABS
templates.env.globals["MORE_KEYS"] = MORE_KEYS
templates.env.globals["shell"] = _shell
templates.env.globals["csrf_input"] = _csrf_input
