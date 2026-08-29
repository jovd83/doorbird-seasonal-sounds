"""DoorBird HTTP client (LAN).

Auth: HTTP Basic with each device's user credentials (the firmware also
advertises Digest; both are accepted on the documented endpoints). For
`info.cgi`, `monitor.cgi` and `audio-transmit.cgi` any valid user works.

What we can and cannot do over the LAN, established by probing a real device
(see README "What we tried"):

* `monitor.cgi` and `audio-transmit.cgi` -> 200. These are documented, and
  together they let us play any audio out of the door station on a ring.
* `customsound.cgi` -> 401 for a normal app user, while a bogus path returns
  404. The endpoint therefore *exists* but is gated to the factory
  administration account; Basic, Digest and the documented `http-user`
  query-parameter form were all refused for a regular user.
* Every other button-sound path we guessed -> 404.

This module is the HTTP half only. The mu-law transmit is a raw-socket
HTTP/1.0 exchange and lives in `audio_transmit`; endpoint discovery shells out
to curl and lives in `diagnostics`. They are reached through this class so
callers keep one entry point, but they are no longer written into it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import httpx

from app.doorbird import audio_transmit, diagnostics
from app.doorbird.types import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    UPLOAD_TIMEOUT,
    AuthDiagnostic,
    DeviceCreds,
    DoorBirdError,
    NotFound,
    ProbeResult,
)

log = logging.getLogger("doorbird.client")

BUTTON_SOUND_PATHS: tuple[tuple[str, str], ...] = (
    ("/bha-api/other/buttonsound/file", "/bha-api/other/buttonsound"),
    ("/other/buttonsound/file",         "/other/buttonsound"),
)


class DoorBirdClient:
    def __init__(self, creds: DeviceCreds):
        self.creds = creds
        # Built *without* `auth=` so we control which scheme tries which.
        # Verification follows the device's own setting rather than being
        # hardcoded off; see `DeviceCreds.verify_tls`.
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=10.0
            ),
            headers={"Accept": "application/json"},
            verify=creds.verify_tls,
        )

    def __enter__(self) -> DoorBirdClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._client.close()

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.creds.username, self.creds.password)

    # ------------------------------------------------------------------ info

    def _info_via(self, scheme: str) -> httpx.Response:
        return self._client.get(self.creds.url(scheme, "/bha-api/info.cgi"), auth=self._auth)

    def info(self) -> dict:
        """Probe `/bha-api/info.cgi` over HTTP, falling back to HTTPS.

        Surfaces a much more diagnostic error than just "401" when things
        go wrong — including which scheme(s) were tried and what the
        response status was on each.
        """
        host = self.creds.clean_host
        if not host:
            raise DoorBirdError(
                "Host field is empty — fill in the device's LAN IP (e.g. 192.168.1.50)")

        attempts: list[str] = []
        last_401_scheme: str | None = None
        last_200_response: httpx.Response | None = None

        for scheme in self.creds.preferred_schemes:
            try:
                r = self._info_via(scheme)
            except httpx.ConnectError as exc:
                attempts.append(f"{scheme} → connect error: {exc}")
                continue
            except httpx.RemoteProtocolError as exc:
                attempts.append(f"{scheme} → protocol error: {exc}")
                continue
            except httpx.HTTPError as exc:
                attempts.append(f"{scheme} → {type(exc).__name__}: {exc}")
                continue

            attempts.append(f"{scheme} → HTTP {r.status_code}")
            if r.status_code == 200:
                last_200_response = r
                break
            if r.status_code in (401, 403):
                last_401_scheme = scheme
                continue
            # other status — keep trying the next scheme

        if last_200_response is not None:
            try:
                return last_200_response.json()
            except ValueError as exc:
                raise DoorBirdError(
                    f"{host} answered 200 but didn't return JSON — is it really a DoorBird? "
                    f"Tried: {attempts}"
                ) from exc

        if last_401_scheme:
            raise DoorBirdError(
                f"401/403 from {host} — the device IS reachable but rejected the credentials. "
                f"Use a user with admin/API privileges (either the default admin or a user you "
                f"created in the DoorBird app under Settings → Administration → User → that has "
                f"the 'Watch always' / 'API operator' permissions enabled). "
                f"Quickest verification: open http://{host}/ in your browser and try logging in "
                f"with the exact same username and password. If login fails there too → "
                f"credentials are wrong. Attempts: {attempts}"
            )

        raise DoorBirdError(
            f"could not reach {host} on either http or https. "
            f"From inside the container, the DoorBird must be on a routable network. "
            f"If your container uses Docker's default bridge and the DoorBird is on the "
            f"same LAN as the host, try setting `network_mode: host` in docker-compose.yml. "
            f"Attempts: {attempts}"
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            data = self.info()
        except DoorBirdError as exc:
            return False, str(exc)
        return True, _describe(data)

    # ---------------------------------------------------------------- upload

    def set_button_sound(self, mp3_path: Path) -> str:
        data = mp3_path.read_bytes()
        if not data:
            raise DoorBirdError(f"empty MP3 file: {mp3_path}")

        last_error = ""
        for scheme in self.creds.preferred_schemes:
            for upload_path, activate_path in BUTTON_SOUND_PATHS:
                try:
                    self._upload(scheme, upload_path, data)
                except NotFound:
                    last_error = f"{scheme} {upload_path}: 404"
                    continue
                except DoorBirdError as exc:
                    last_error = f"{scheme} {upload_path}: {exc}"
                    if "401" in str(exc):
                        # creds wrong; no point trying other paths on this scheme
                        break
                    continue

                try:
                    self._activate(scheme, activate_path, "custom")
                except NotFound:
                    last_error = f"{scheme} {activate_path}: 404"
                    continue
                except DoorBirdError as exc:
                    raise DoorBirdError(
                        f"uploaded to {upload_path} but activate failed: {exc}") from exc

                return (
                    f"uploaded {mp3_path.name} ({len(data) / 1024:.0f} KB) "
                    f"via {scheme}{upload_path} and activated custom sound"
                )

        raise DoorBirdError(
            "no button-sound upload endpoint accepted this account. "
            f"Last try: {last_error}. What we know: /bha-api/customsound.cgi exists on "
            "this firmware (it answers 401, whereas a made-up path answers 404) but "
            "refuses normal app users under Basic, Digest and http-user auth alike, so "
            "it is gated to the factory administration account. Everything else 404s. "
            "Use ring-chime mode instead (RING_CHIME_ENABLED=1), which plays the "
            "seasonal sound through the door speaker using only documented endpoints."
        )

    def _upload(self, scheme: str, path: str, data: bytes) -> None:
        try:
            r = self._client.post(
                self.creds.url(scheme, path),
                content=data,
                headers={"Content-Type": "audio/mpeg;charset=UTF-8"},
                timeout=httpx.Timeout(
                    connect=CONNECT_TIMEOUT, read=UPLOAD_TIMEOUT,
                    write=UPLOAD_TIMEOUT, pool=10.0,
                ),
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            raise DoorBirdError(f"network error on {path}: {exc}") from exc
        _raise_for_status(r, path)

    def _activate(self, scheme: str, path: str, sound: str) -> None:
        try:
            r = self._client.post(
                self.creds.url(scheme, path),
                json={"buttonSound": sound},
                headers={"Content-Type": "application/json"},
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            raise DoorBirdError(f"network error on {path}: {exc}") from exc
        _raise_for_status(r, path)

    # ------------------------------------------------------------ ring chime

    def stream_ring_events(
        self, *, timeout: float = 0.0, event_types: str = "doorbell",
    ) -> Iterator[tuple[str, str]]:
        """Yield `(event, state)` pairs from `monitor.cgi` as they arrive.

        `monitor.cgi` holds a `multipart/x-mixed-replace` connection open and
        writes one `name:STATE` line per transition, e.g. `doorbell:H` on
        press and `doorbell:L` on release. It emits the current state of every
        requested input immediately on connect, so the first few pairs are a
        snapshot rather than real events.

        This is passive: unlike an HTTP favorite it changes nothing on the
        device, so it cannot disturb an existing home-automation integration
        that already owns the doorbell's HTTP schedule slot.

        `timeout` bounds how long the stream may sit silent. Reaching it ends
        the generator normally rather than raising: a quiet doorbell is the
        expected case, and the caller reconnects. Without a bound the read
        blocks forever, which is what made a stopped watcher impossible to
        actually stop -- the thread could not notice its own stop flag until
        the device happened to send a line.
        """
        url = self.creds.url(
            self.creds.scheme, f"/bha-api/monitor.cgi?ring={event_types}")
        read_timeout = timeout if timeout > 0 else None

        with httpx.Client(
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=read_timeout,
                                  write=READ_TIMEOUT, pool=10.0),
            verify=self.creds.verify_tls,
        ) as client, client.stream("GET", url, auth=self._auth) as r:
            if r.status_code == 401:
                raise DoorBirdError(
                    f"401 on monitor.cgi — credentials rejected for {self.creds.clean_host}")
            if r.status_code != 200:
                raise DoorBirdError(f"monitor.cgi returned HTTP {r.status_code}")
            try:
                for raw in r.iter_lines():
                    line = raw.strip()
                    if not line or ":" not in line:
                        continue
                    name, _, state = line.partition(":")
                    name, state = name.strip(), state.strip().upper()
                    if state in ("H", "L"):
                        yield name, state
            except httpx.ReadTimeout:
                log.debug("monitor.cgi idle for %.0fs on %s; reconnecting",
                          read_timeout or 0.0, self.creds.clean_host)
                return

    def play_ulaw(self, ulaw_path: Path, *, retries: int = 3,
                  retry_wait: float = 1.5) -> str:
        """Stream mu-law out of the door speaker. See `audio_transmit`."""
        return audio_transmit.play_ulaw(
            self.creds, ulaw_path, retries=retries, retry_wait=retry_wait)

    # ----------------------------------------------------------- diagnostics

    def probe_endpoints(self) -> list[ProbeResult]:
        """Scan candidate paths. See `diagnostics`."""
        return diagnostics.probe_endpoints(self.creds)

    def diagnose_button_sound(self) -> list[AuthDiagnostic]:
        """Report how this account is treated on button-sound paths."""
        return diagnostics.diagnose_button_sound(self._client, self.creds)

    def _curl_credentials(self) -> str:
        """Kept for the test that guards against credentials reaching argv."""
        return diagnostics.curl_credentials(self.creds)


def _raise_for_status(r: httpx.Response, path: str) -> None:
    if r.status_code == 404:
        raise NotFound()
    if r.status_code == 401:
        raise DoorBirdError(f"401 unauthorized — credentials rejected on POST {path}")
    if r.status_code >= 400:
        raise DoorBirdError(f"POST {path} returned HTTP {r.status_code}: {r.text[:200]}")


def _describe(data: dict) -> str:
    """One line about the device, from `info.cgi`'s nested JSON."""
    bha = data.get("BHA", {})
    ver = (bha.get("VERSION") or [{}])[0]
    device = ver.get("DEVICE-TYPE", "?")
    fw = ver.get("FIRMWARE", "?")
    mac = ver.get("WIFI-MAC-ADDR") or ver.get("PRIMARY-MAC-ADDR") or "?"
    relays = ver.get("RELAYS")
    suffix = f", relays={relays}" if relays else ""
    return f"connected: {device} fw={fw} mac={mac}{suffix}"
