import httpx
import pytest

from app.doorbird import DeviceCreds, DoorBirdClient, DoorBirdError


def test_url_construction_http():
    c = DeviceCreds(host="192.168.1.50", username="u", password="p")
    assert c.url("http", "/bha-api/info.cgi") == "http://192.168.1.50/bha-api/info.cgi"


def test_url_construction_https():
    c = DeviceCreds(host="dbird.local", username="u", password="p", use_https=True)
    assert c.url("https", "/bha-api/info.cgi") == "https://dbird.local/bha-api/info.cgi"


def test_host_normalization_strips_scheme_and_trailing_slash():
    c = DeviceCreds(host="  http://192.168.1.50/  ", username="u", password="p")
    assert c.clean_host == "192.168.1.50"
    assert c.url("http", "/bha-api/info.cgi") == "http://192.168.1.50/bha-api/info.cgi"

    c2 = DeviceCreds(host="HTTPS://Dbird.local///", username="u", password="p")
    assert c2.clean_host == "Dbird.local"


def test_test_connection_handles_401():
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = DoorBirdClient(DeviceCreds(host="dbird.lan", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    ok, msg = client.test_connection()
    assert ok is False
    assert "401" in msg
    assert "http://dbird.lan/" in msg
    assert "API operator" in msg or "admin" in msg.lower()


def test_test_connection_happy_path():
    def transport(request: httpx.Request) -> httpx.Response:
        assert "/bha-api/info.cgi" in str(request.url)
        return httpx.Response(200, json={
            "BHA": {
                "RETURNCODE": "1",
                "VERSION": [{
                    "DEVICE-TYPE": "DoorBird D2101V",
                    "FIRMWARE": "000139",
                    "WIFI-MAC-ADDR": "1CCAE3xxxxxx",
                    "RELAYS": ["1", "2"],
                }]
            }
        })

    client = DoorBirdClient(DeviceCreds(host="192.168.1.50", username="ggaaaa0000", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    ok, msg = client.test_connection()
    assert ok
    assert "D2101V" in msg
    assert "000139" in msg


def test_test_connection_network_failure_gives_helpful_message():
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = DoorBirdClient(DeviceCreds(host="10.0.0.99", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    ok, msg = client.test_connection()
    assert ok is False
    assert "could not reach" in msg
    assert "network_mode: host" in msg  # hint about Docker bridge


def test_test_connection_empty_host():
    client = DoorBirdClient(DeviceCreds(host="   ", username="u", password="p"))
    client._client = httpx.Client()
    ok, msg = client.test_connection()
    assert ok is False
    assert "empty" in msg.lower()


def test_test_connection_http_fails_https_succeeds():
    def transport(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://"):
            raise httpx.ConnectError("http disabled")
        return httpx.Response(200, json={
            "BHA": {"VERSION": [{"DEVICE-TYPE": "DoorBird", "FIRMWARE": "000139"}]}
        })

    client = DoorBirdClient(DeviceCreds(host="dbird.lan", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    ok, msg = client.test_connection()
    assert ok
    assert "DoorBird" in msg


def test_set_button_sound_first_path_wins(tmp_path):
    mp3 = tmp_path / "tone.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 1024)

    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    client = DoorBirdClient(DeviceCreds(host="x", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    msg = client.set_button_sound(mp3)

    assert len(calls) == 2  # upload + activate
    assert "/bha-api/other/buttonsound/file" in str(calls[0].url)
    assert calls[0].headers["content-type"].startswith("audio/mpeg")
    assert "/bha-api/other/buttonsound" in str(calls[1].url)
    assert calls[1].headers["content-type"].startswith("application/json")
    assert b'"buttonSound"' in calls[1].content
    assert b'"custom"' in calls[1].content
    assert "uploaded" in msg


def test_set_button_sound_falls_through_on_404(tmp_path):
    mp3 = tmp_path / "tone.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 64)

    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/bha-api/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200)

    client = DoorBirdClient(DeviceCreds(host="x", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    msg = client.set_button_sound(mp3)

    paths = [str(c.url).split("//", 1)[1].split("/", 1)[1] for c in calls]
    assert "bha-api/other/buttonsound/file" in paths
    assert "other/buttonsound/file" in paths
    assert "uploaded" in msg


def test_set_button_sound_raises_when_no_path_works(tmp_path):
    mp3 = tmp_path / "tone.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 64)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = DoorBirdClient(DeviceCreds(host="x", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(DoorBirdError) as exc:
        client.set_button_sound(mp3)
    assert "no button-sound upload endpoint accepted this account" in str(exc.value)
    # The message must point at the mode that actually works, not leave the
    # user guessing about firmware paths.
    assert "RING_CHIME_ENABLED" in str(exc.value)


def test_set_button_sound_upload_401_surfaces(tmp_path):
    mp3 = tmp_path / "tone.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 64)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = DoorBirdClient(DeviceCreds(host="x", username="u", password="p"))
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(DoorBirdError) as exc:
        client.set_button_sound(mp3)
    assert "401" in str(exc.value)


# --------------------------------------------------- credentials on the wire


def test_probe_never_puts_the_password_on_the_command_line(monkeypatch):
    """`-u user:pass` as an argv entry is readable from the process table.

    Anything that can see /proc — `ps`, a container inspector, another process
    in the same namespace — could read the door station's password for the
    whole life of the call, once per probed path.
    """
    import subprocess

    from app.doorbird import DeviceCreds, DoorBirdClient

    seen: list[list[str]] = []
    stdin_seen: list[str] = []

    class _Result:
        returncode = 0
        stdout = "404\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        stdin_seen.append(kwargs.get("input") or "")
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    secret = "sup3r-s3cret-p4ss"
    creds = DeviceCreds(host="10.0.0.5", username="operator", password=secret)
    with DoorBirdClient(creds) as client:
        results = client.probe_endpoints()

    assert seen, "the probe ran no commands"
    for argv in seen:
        flat = " ".join(argv)
        assert secret not in flat, f"password leaked into argv: {flat}"
        assert "-u" not in argv, "curl -u puts credentials in the process table"

    # It still authenticates — just over stdin.
    assert all(secret in body for body in stdin_seen)
    assert all(body.startswith('user = "operator:') for body in stdin_seen)
    assert len(results) == len(seen)


def test_curl_credentials_escape_quotes_and_backslashes():
    """curl's config format is `key = "value"`; both need escaping."""
    from app.doorbird import DeviceCreds, DoorBirdClient

    quote = chr(34)
    backslash = chr(92)
    creds = DeviceCreds(
        host="h",
        username=f"us{quote}er",       # a quote would close the value early
        password=f"pa{backslash}ss",   # a backslash would escape what follows
    )
    with DoorBirdClient(creds) as client:
        line = client._curl_credentials()

    expected = (
        f"user = {quote}"
        f"us{backslash}{quote}er"
        f":pa{backslash}{backslash}ss"
        f"{quote}\n"
    )
    assert line == expected
