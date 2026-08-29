"""Async DoorBird LAN HTTP client (httpx.AsyncClient).

Uses HTTP Basic Auth against the device's LAN IP. See the sibling Docker
app's `app/doorbird_client.py` docstring for the full reasoning on why
we hit the LAN directly instead of the cloud admin (captcha-gated).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

_BUTTON_SOUND_PATHS: tuple[tuple[str, str], ...] = (
    ("/bha-api/other/buttonsound/file", "/bha-api/other/buttonsound"),
    ("/other/buttonsound/file",         "/other/buttonsound"),
)


@dataclass
class DeviceCreds:
    name: str
    host: str  # LAN IP / hostname
    username: str
    password: str
    use_https: bool = field(default=False)

    def url(self, path: str) -> str:
        scheme = "https" if self.use_https else "http"
        path = path if path.startswith("/") else "/" + path
        return f"{scheme}://{self.host}{path}"


class DoorBirdError(Exception):
    pass


class _NotFound(Exception):
    pass


class DoorBirdClient:
    def __init__(self, creds: DeviceCreds, *, client: httpx.AsyncClient | None = None):
        self.creds = creds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            auth=(creds.username, creds.password),
            timeout=httpx.Timeout(connect=8.0, read=30.0, write=90.0, pool=10.0),
            headers={"Accept": "application/json"},
            verify=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def info(self) -> dict:
        try:
            r = await self._client.get(
                self.creds.url("/bha-api/info.cgi"),
                auth=(self.creds.username, self.creds.password),
            )
        except httpx.ConnectError as exc:
            raise DoorBirdError(
                f"cannot reach {self.creds.host} — check the host is the device's LAN IP: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DoorBirdError(f"network error talking to {self.creds.host}: {exc}") from exc

        if r.status_code == 401:
            raise DoorBirdError(
                "401 unauthorized — admin credentials rejected. Use the DoorBird "
                "ADMINISTRATION user (ends in '0000'), not the regular app user."
            )
        if r.status_code != 200:
            raise DoorBirdError(f"GET /bha-api/info.cgi returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise DoorBirdError(
                "info.cgi did not return JSON — is the host really a DoorBird?") from exc

    async def test_connection(self) -> tuple[bool, str]:
        try:
            data = await self.info()
        except DoorBirdError as exc:
            return False, str(exc)
        bha = data.get("BHA", {})
        ver = (bha.get("VERSION") or [{}])[0]
        device = ver.get("DEVICE-TYPE", "?")
        fw = ver.get("FIRMWARE", "?")
        return True, f"connected: {device} fw={fw}"

    async def set_button_sound(self, mp3_path: Path) -> str:
        data = mp3_path.read_bytes()
        if not data:
            raise DoorBirdError(f"empty MP3 file: {mp3_path}")

        last_error = ""
        for upload_path, activate_path in _BUTTON_SOUND_PATHS:
            try:
                await self._upload(upload_path, data)
            except _NotFound:
                last_error = f"{upload_path}: 404"
                continue
            try:
                await self._activate(activate_path, "custom")
            except _NotFound:
                last_error = f"{activate_path}: 404"
                continue
            size_kb = len(data) / 1024
            return f"uploaded {mp3_path.name} ({size_kb:.0f} KB) via {upload_path}"

        raise DoorBirdError(
            f"no known button-sound endpoint responded on {self.creds.host}. "
            f"Last try: {last_error}."
        )

    async def activate_button_sound(self, sound: str = "custom") -> None:
        last_error = ""
        for _, activate_path in _BUTTON_SOUND_PATHS:
            try:
                await self._activate(activate_path, sound)
                return
            except _NotFound:
                last_error = f"{activate_path}: 404"
        raise DoorBirdError(f"no known activate endpoint. Last try: {last_error}")

    async def _upload(self, path: str, data: bytes) -> None:
        try:
            r = await self._client.post(
                self.creds.url(path),
                content=data,
                headers={"Content-Type": "audio/mpeg;charset=UTF-8"},
                auth=(self.creds.username, self.creds.password),
            )
        except httpx.HTTPError as exc:
            raise DoorBirdError(f"network error on {path}: {exc}") from exc
        if r.status_code == 404:
            raise _NotFound()
        if r.status_code == 401:
            raise DoorBirdError("401 unauthorized — admin credentials rejected during upload")
        if r.status_code >= 400:
            raise DoorBirdError(f"POST {path} returned HTTP {r.status_code}: {r.text[:300]}")

    async def _activate(self, path: str, sound: str) -> None:
        try:
            r = await self._client.post(
                self.creds.url(path),
                json={"buttonSound": sound},
                headers={"Content-Type": "application/json"},
                auth=(self.creds.username, self.creds.password),
            )
        except httpx.HTTPError as exc:
            raise DoorBirdError(f"network error on {path}: {exc}") from exc
        if r.status_code == 404:
            raise _NotFound()
        if r.status_code == 401:
            raise DoorBirdError("401 unauthorized — admin credentials rejected during activate")
        if r.status_code >= 400:
            raise DoorBirdError(f"POST {path} returned HTTP {r.status_code}: {r.text[:300]}")
