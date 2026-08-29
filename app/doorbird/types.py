"""Shared vocabulary for talking to a door station.

Kept apart from any transport so the credential type, the error type and the
result records can be imported without dragging in httpx, a raw socket, or the
diagnostics subprocess.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 90.0


def normalize_host(raw: str) -> str:
    """Strip a scheme and trailing slashes off whatever the user typed."""
    host = (raw or "").strip().rstrip("/")
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


@dataclass
class DeviceCreds:
    host: str
    username: str
    password: str
    use_https: bool = field(default=False)
    # Check the certificate when talking HTTPS. Off by default: a stock door
    # station has a self-signed certificate and is reached by IP, so
    # verification cannot succeed. On for anyone who has installed a real one.
    verify_tls: bool = field(default=False)

    @property
    def clean_host(self) -> str:
        return normalize_host(self.host)

    def url(self, scheme: str, path: str) -> str:
        path = path if path.startswith("/") else "/" + path
        return f"{scheme}://{self.clean_host}{path}"

    @property
    def preferred_schemes(self) -> tuple[str, str]:
        """Which scheme to try first, then the fallback."""
        return ("https", "http") if self.use_https else ("http", "https")

    @property
    def scheme(self) -> str:
        return "https" if self.use_https else "http"


class DoorBirdError(Exception):
    pass


class NotFound(Exception):
    """Internal: a path answered 404, so try the next candidate."""


@dataclass(frozen=True)
class ProbeResult:
    """One probed path and what the device said about it.

    A dataclass rather than a bare dict because these cross into a Jinja
    template. The dict version let `probe.html` reference `r.POST` and
    `r.GET_excerpt`, neither of which the probe has ever produced -- the
    columns silently rendered blank for everyone. Attributes fail loudly.
    """

    path: str
    scheme: str
    status: int | str

    @property
    def exists(self) -> bool:
        """200, 401 and 405 all mean the path is real; 404 means it is not."""
        return self.status in (200, 401, 405)


@dataclass(frozen=True)
class AuthDiagnostic:
    """How one path answers under each auth scheme we can offer it."""

    path: str
    basic: int | str
    digest: int | str
