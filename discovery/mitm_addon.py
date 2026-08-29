"""mitmproxy addon: capture the DoorBird app's button-sound change end-to-end.

Run with:
    mitmweb -s discovery/mitm_addon.py --set candidate_dir=discovery/captures

Goal: when you change the door-station "Button Sound" in the DoorBird app
(with your phone proxied through mitmproxy), this addon records the EXACT
request(s) the app sends — endpoint, method, auth header, body — and the
response status. That is the authoritative answer to the open question:
does the app change the sound over the LAN, or via the captcha-gated cloud?

What it captures:
  * EVERY request to a DoorBird host (LAN `bha-api`, `api.doorbird.io`,
    `account.doorbird.io`) — full headers + body, plus the response status —
    so we see the whole sequence, including the small JSON activation call
    that the old size-based filter used to miss.
  * A one-line entry per flow appended to `_sequence.log` so you can read the
    ordered call chain at a glance.

SECURITY NOTE: the dumps contain the live Authorization header / device token /
password the app sends. They are written under discovery/captures/ which is
gitignored. Don't share them unredacted — that header IS the credential.
"""
from __future__ import annotations

import time
from pathlib import Path

from mitmproxy import ctx, http


# Path keywords that mark a flow as button-sound related (any method/size).
_SOUND_HINTS = ("buttonsound", "button-sound", "customsound", "custom-sound", "/sound")


class DoorbirdSniffer:
    def __init__(self) -> None:
        self.dir = Path("discovery/captures")
        self._seq = self.dir / "_sequence.log"

    def load(self, loader):  # noqa: D401
        loader.add_option(
            name="candidate_dir",
            typespec=str,
            default="discovery/captures",
            help="Directory to write candidate request dumps to.",
        )

    def configure(self, updated):
        if "candidate_dir" in updated:
            self.dir = Path(ctx.options.candidate_dir)
            self.dir.mkdir(parents=True, exist_ok=True)
            self._seq = self.dir / "_sequence.log"

    # We dump on `response` so we can record the status code the device/cloud
    # returned (200 = the call that actually worked — that's the one to copy).
    def response(self, flow: http.HTTPFlow) -> None:
        if not _looks_like_doorbird(flow):
            return

        req = flow.request
        sound = _is_sound_related(flow)
        upload = _looks_like_upload(flow)
        # Skip pure media/keepalive noise unless it's sound-related or an upload.
        if not sound and not upload and _is_noise(flow):
            return

        status = flow.response.status_code if flow.response else "?"
        ts = time.strftime("%Y%m%d-%H%M%S")
        tag = "SOUND_" if sound else ("UPLOAD_" if upload else "")
        slug = req.path.split("?")[0].replace("/", "_").strip("_")[:60] or "root"
        out = self.dir / f"{ts}__{tag}{req.method}__{slug}__{status}.txt"
        out.write_text(_dump(flow), encoding="utf-8")

        ctype = req.headers.get("content-type", "?")
        size = len(req.raw_content or b"")
        auth = _auth_kind(req)
        line = (
            f"{time.strftime('%H:%M:%S')}  {req.method:6} {req.pretty_url}  "
            f"ct={ctype} body={size}B auth={auth} -> HTTP {status}  [{out.name}]"
        )
        with self._seq.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        marker = "  <<< BUTTON SOUND" if sound else ("  <<< upload" if upload else "")
        ctx.log.alert(f"[doorbird-sniff] {line}{marker}")


def _looks_like_doorbird(flow: http.HTTPFlow) -> bool:
    host = (flow.request.host or "").lower()
    path = flow.request.path or ""
    if "doorbird" in host:
        return True
    if "bha-api" in path or path.startswith("/bha-api"):
        return True
    return False


def _is_sound_related(flow: http.HTTPFlow) -> bool:
    path = (flow.request.path or "").lower()
    return any(h in path for h in _SOUND_HINTS)


def _looks_like_upload(flow: http.HTTPFlow) -> bool:
    method = (flow.request.method or "").upper()
    if method not in {"POST", "PUT"}:
        return False
    body = flow.request.raw_content or b""
    ctype = (flow.request.headers.get("content-type") or "").lower()
    if ctype.startswith(("audio/", "multipart/", "application/octet-stream")):
        return True
    if body[:3] == b"ID3" or body[:2] == b"\xff\xfb":  # MP3 magic
        return True
    return False


def _is_noise(flow: http.HTTPFlow) -> bool:
    """Live-view / image / video / webrtc polling we don't care about."""
    path = (flow.request.path or "").lower()
    noisy = ("image.cgi", "video.cgi", "monitor.cgi", "live-image",
             "live/video", "webrtc", "favicon", ".jpg", ".png", "/static/")
    return any(n in path for n in noisy)


def _auth_kind(req: http.Request) -> str:
    a = req.headers.get("authorization", "")
    if a.lower().startswith("basic "):
        return "Basic"
    if a.lower().startswith("bearer "):
        return "Bearer"
    if a:
        return a.split(" ", 1)[0]
    if req.headers.get("x-cloud-account-authorization"):
        return "X-Cloud-Account"
    if "sessionid" in (req.path or "").lower():
        return "sessionid-param"
    return "none"


def _dump(flow: http.HTTPFlow) -> str:
    req = flow.request
    headers = "\n".join(f"{k}: {v}" for k, v in req.headers.items())
    raw = req.raw_content or b""
    is_text = req.headers.get("content-type", "").startswith(
        ("application/json", "text/", "application/x-www-form-urlencoded")
    )
    if is_text and len(raw) < 4096:
        body_block = ["## Body (decoded)", raw.decode("utf-8", "replace")]
    else:
        body_block = ["## Body (first 512 bytes, hex)", raw[:512].hex()]

    resp_line = (
        f"HTTP {flow.response.status_code}" if flow.response else "(no response captured)"
    )
    parts = [
        f"# {req.method} {req.pretty_url}",
        f"# captured at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# response: {resp_line}",
        f"# auth scheme: {_auth_kind(req)}",
        "",
        "## Request line",
        f"{req.method} {req.path} HTTP/{req.http_version}",
        "",
        "## Headers",
        headers,
        "",
        *body_block,
        "",
        "## Body length",
        str(len(raw)),
        "",
        "## Reproduce with curl",
        _curl(flow),
    ]
    return "\n".join(parts)


def _curl(flow: http.HTTPFlow) -> str:
    req = flow.request
    parts = [f"curl -X {req.method} '{req.pretty_url}'"]
    for k, v in req.headers.items():
        if k.lower() in {"host", "content-length"}:
            continue
        parts.append(f"  -H '{k}: {v}'")
    parts.append("  --data-binary @body.bin")
    return " \\\n".join(parts)


addons = [DoorbirdSniffer()]
