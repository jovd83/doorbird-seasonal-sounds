"""Endpoint discovery and auth diagnostics.

Development tooling, not client behaviour, and it lives apart from the client
for that reason. `probe_endpoints` in particular is a second, divergent HTTP
implementation -- it shells out to `curl` -- which had no business sitting on
the same class as the real transport.

The reason it uses curl at all is recorded below and is not superstition:
httpx's timeouts have proved unreliable against this firmware, and curl's `-m`
is an OS-level wall-clock kill that always fires.
"""
from __future__ import annotations

import logging
import subprocess

import httpx

from app.doorbird.types import AuthDiagnostic, DeviceCreds, ProbeResult

log = logging.getLogger("doorbird.probe")

# Hard per-request wall-clock kill, in seconds, and the outer subprocess guard.
CURL_TIMEOUT = 3
SUBPROCESS_TIMEOUT = 5

# Candidate paths we'll probe to discover where (if anywhere) this firmware
# exposes button-sound configuration. The list is intentionally broad — most
# will 404; the few that come back 200/401/405 are real paths worth trying.
PROBE_PATHS: tuple[str, ...] = (
    # documented LAN endpoints (sanity check + known-good points of reference)
    "/bha-api/info.cgi",
    "/bha-api/audio-receive.cgi",
    "/bha-api/audio-transmit.cgi",
    "/bha-api/favorites.cgi",
    "/bha-api/schedule.cgi",
    # CGI-style button-sound guesses
    "/bha-api/buttonsound.cgi",
    "/bha-api/button-sound.cgi",
    "/bha-api/customsound.cgi",
    "/bha-api/custom-sound.cgi",
    "/bha-api/setbuttonsound.cgi",
    "/bha-api/sound.cgi",
    "/bha-api/upload-sound.cgi",
    "/bha-api/uploadsound.cgi",
    "/bha-api/admin/buttonsound.cgi",
    "/bha-api/config/buttonsound.cgi",
    # Mirror of the cloud SPA's paths
    "/bha-api/other/buttonsound",
    "/bha-api/other/buttonsound/file",
    "/bha-api/other/sound",
    "/bha-api/other/sound/file",
    "/other/buttonsound",
    "/other/buttonsound/file",
    # Versioned variants
    "/api/v1/other/buttonsound/file",
    "/api/other/buttonsound/file",
    "/v1/buttonsound/file",
    # Admin section guesses (some firmwares expose /admin/...)
    "/admin/buttonsound",
    "/admin/sound",
    "/admin/config/buttonsound",
)

AUTH_CHECK_PATHS: tuple[str, ...] = (
    "/bha-api/info.cgi",
    "/bha-api/customsound.cgi",
    "/bha-api/nonexistent-control-probe.cgi",
)


def curl_credentials(creds: DeviceCreds) -> str:
    """A curl config-file body carrying the credentials, for stdin.

    Never `-u user:pass` on the command line: an argv entry is readable by
    anything that can see the process table -- `ps`, `/proc/<pid>/cmdline`, a
    container inspector -- for the whole life of the call, once per probed path.

    curl's config format is `key = "value"` with backslash escaping, so both
    metacharacters need escaping or a password containing a quote would
    truncate the value.
    """
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return f'user = "{esc(creds.username)}:{esc(creds.password)}"\n'


def probe_endpoints(creds: DeviceCreds) -> list[ProbeResult]:
    """Scan candidate paths, sequentially and with a guaranteed end.

    27 paths x 3 s is ~80 s worst case; a typical run is far quicker because
    404s come back immediately.
    """
    scheme = creds.scheme
    host = creds.clean_host
    log.info("probe start: host=%s scheme=%s paths=%d", host, scheme, len(PROBE_PATHS))

    config = curl_credentials(creds)
    results: list[ProbeResult] = []
    for path in PROBE_PATHS:
        status = _probe_one(f"{scheme}://{host}{path}", config)
        log.info("probe[%s] %-44s -> GET=%s", scheme, path, status)
        results.append(ProbeResult(path=path, scheme=scheme, status=status))

    log.info("probe done")
    return results


def _probe_one(url: str, config: str) -> int | str:
    try:
        proc = subprocess.run(
            [
                "curl", "-sk", "-m", str(CURL_TIMEOUT),
                "-o", "/dev/null",
                "-w", "%{http_code}\n",
                # The remaining options, credentials included, come from stdin.
                "--config", "-",
                url,
            ],
            input=config,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return "subprocess timeout"
    except FileNotFoundError:
        return "curl not installed in container"

    code = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # curl exit codes: 7=connect refused, 28=timeout, 6=can't resolve
        return f"curl_exit={proc.returncode} ({code or 'no response'})"
    try:
        return int(code)
    except ValueError:
        return code or "?"


def diagnose_button_sound(client: httpx.Client, creds: DeviceCreds) -> list[AuthDiagnostic]:
    """Report how this account is treated on the button-sound endpoints.

    Useful when trying administration credentials: a 401 here with a 200 on
    info.cgi means the account authenticates but is not privileged for
    `customsound.cgi`. Anything other than 401/404 means we finally have access
    and can start working out the payload format.
    """
    auth = (creds.username, creds.password)
    out: list[AuthDiagnostic] = []
    for path in AUTH_CHECK_PATHS:
        codes: dict[str, int | str] = {}
        for label, scheme_auth in (
            ("basic", httpx.BasicAuth(*auth)),
            ("digest", httpx.DigestAuth(*auth)),
        ):
            try:
                r = client.get(creds.url(creds.scheme, path), auth=scheme_auth)
                codes[label] = r.status_code
            except httpx.HTTPError as exc:
                codes[label] = type(exc).__name__
        out.append(AuthDiagnostic(path=path, basic=codes["basic"], digest=codes["digest"]))
    return out
