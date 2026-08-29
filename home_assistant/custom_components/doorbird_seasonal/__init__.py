"""DoorBird Seasonal Sounds — Home Assistant integration.

YAML-configured; one integration instance manages all DoorBirds + the
shared schedule + MP3 library. Reconciles daily at the configured time;
also exposes manual services and per-device sensors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType

from .client import DeviceCreds, DoorBirdClient, DoorBirdError
from .const import (
    ATTR_DEVICES,
    ATTR_FORCE,
    ATTR_MP3,
    ATTR_SOUND,
    CONF_DAILY_RUN_TIME,
    CONF_DEFAULT_MP3,
    CONF_DEVICES,
    CONF_END_TIME,
    CONF_END_YEAR,
    CONF_FROM,
    CONF_MP3,
    CONF_MP3_DIR,
    CONF_PRIORITY,
    CONF_SCHEDULES,
    CONF_START_TIME,
    CONF_START_YEAR,
    CONF_TO,
    CONF_YEAR,
    DEFAULT_DAILY_RUN_TIME,
    DEFAULT_MP3_DIR,
    DOMAIN,
    SERVICE_ACTIVATE_BUILTIN,
    SERVICE_APPLY_NOW,
    SERVICE_SET_BUTTON_SOUND,
    SERVICE_TEST_CONNECTION,
    SIGNAL_RECONCILED,
)
from .date_logic import Resolution, Schedule, resolve_active

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def _validate_md(value: str) -> tuple[int, int]:
    parts = value.strip().split("-")
    if len(parts) != 2:
        raise vol.Invalid(f"expected MM-DD, got {value!r}")
    try:
        m, d = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise vol.Invalid(f"non-numeric MM-DD: {value!r}") from exc
    if not 1 <= m <= 12 or not 1 <= d <= 31:
        raise vol.Invalid(f"out of range MM-DD: {value!r}")
    return m, d


def _validate_time_str(value: str) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError) as exc:
        raise vol.Invalid(f"expected HH:MM, got {value!r}") from exc


def _validate_hhmm(value) -> int:
    """'HH:MM' -> minutes since midnight, matching the app's storage."""
    raw = str(value).strip()
    try:
        hh, _, mm = raw.partition(":")
        hours, minutes = int(hh), int(mm)
    except ValueError as exc:
        raise vol.Invalid(f"expected HH:MM, got {value!r}") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise vol.Invalid(f"expected a time between 00:00 and 23:59, got {value!r}")
    return hours * 60 + minutes


DEVICE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
})

_YEAR_RANGE = vol.All(int, vol.Range(min=2000, max=2100))

SCHEDULE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_MP3): cv.string,
    vol.Required(CONF_FROM): _validate_md,
    vol.Optional(CONF_TO): _validate_md,
    # `year:` is a back-compat shortcut for start_year+end_year on the same year.
    # For multi-year one-offs, set both start_year and end_year.
    vol.Optional(CONF_YEAR): _YEAR_RANGE,
    vol.Optional(CONF_START_YEAR): _YEAR_RANGE,
    vol.Optional(CONF_END_YEAR): _YEAR_RANGE,
    vol.Optional(CONF_PRIORITY, default=100): vol.All(int, vol.Range(min=0, max=10000)),
    # A time-of-day window, as HH:MM. Give both or neither; a start later than
    # the end wraps past midnight (22:00 -> 02:00), same as the app.
    vol.Optional(CONF_START_TIME): _validate_hhmm,
    vol.Optional(CONF_END_TIME): _validate_hhmm,
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_DEVICES): vol.All(cv.ensure_list, [DEVICE_SCHEMA]),
        vol.Required(CONF_DEFAULT_MP3): cv.string,
        vol.Optional(CONF_SCHEDULES, default=list): vol.All(cv.ensure_list, [SCHEDULE_SCHEMA]),
        vol.Optional(CONF_MP3_DIR, default=DEFAULT_MP3_DIR): cv.string,
        vol.Optional(CONF_DAILY_RUN_TIME, default=DEFAULT_DAILY_RUN_TIME): _validate_time_str,
    })
}, extra=vol.ALLOW_EXTRA)


@dataclass
class DeviceState:
    creds: DeviceCreds
    enabled: bool = True
    last_applied_mp3: str | None = None
    last_applied_at: datetime | None = None
    last_error: str | None = None
    test_status: str | None = None


@dataclass
class HubData:
    devices: dict[str, DeviceState] = field(default_factory=dict)
    schedules: list[Schedule] = field(default_factory=list)
    default_mp3: str = ""
    mp3_dir: Path = field(default_factory=Path)
    daily_run_time: time = field(default_factory=lambda: time(3, 15))
    last_reconcile: datetime | None = None
    last_resolution: Resolution | None = None
    next_change: tuple[date, str] | None = None
    http: httpx.AsyncClient | None = None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN not in config:
        return True

    cfg = config[DOMAIN]
    hub = HubData(
        devices={
            d[CONF_NAME]: DeviceState(creds=DeviceCreds(
                name=d[CONF_NAME],
                host=d[CONF_HOST],
                username=d[CONF_USERNAME],
                password=d[CONF_PASSWORD],
            ))
            for d in cfg[CONF_DEVICES]
        },
        schedules=[
            Schedule(
                name=s[CONF_NAME],
                mp3=s[CONF_MP3],
                start_month=s[CONF_FROM][0],
                start_day=s[CONF_FROM][1],
                end_month=s.get(CONF_TO, (None, None))[0],
                end_day=s.get(CONF_TO, (None, None))[1],
                start_year=s.get(CONF_START_YEAR, s.get(CONF_YEAR)),
                end_year=s.get(CONF_END_YEAR, s.get(CONF_YEAR)),
                priority=s[CONF_PRIORITY],
                start_minute=s.get(CONF_START_TIME),
                end_minute=s.get(CONF_END_TIME),
            )
            for s in cfg[CONF_SCHEDULES]
        ],
        default_mp3=cfg[CONF_DEFAULT_MP3],
        mp3_dir=Path(cfg[CONF_MP3_DIR]),
        daily_run_time=cfg[CONF_DAILY_RUN_TIME],
        http=httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=90.0, pool=10.0),
            headers={"Accept": "application/json"},
        ),
    )
    hub.mp3_dir.mkdir(parents=True, exist_ok=True)
    hass.data[DOMAIN] = hub

    await _async_register_services(hass)
    _schedule_daily(hass, hub)

    async def _initial(_: Any) -> None:
        await _async_reconcile(hass, hub)

    hass.async_create_task(_initial(None))

    hass.async_create_task(
        discovery.async_load_platform(hass, Platform.SENSOR, DOMAIN, {}, config)
    )
    return True


@callback
def _schedule_daily(hass: HomeAssistant, hub: HubData) -> None:
    async def _fire(_now: datetime) -> None:
        _LOGGER.info("daily reconcile firing at %s", _now.isoformat())
        await _async_reconcile(hass, hub)

    async_track_time_change(
        hass, _fire,
        hour=hub.daily_run_time.hour,
        minute=hub.daily_run_time.minute,
        second=0,
    )
    _LOGGER.info("scheduled daily reconcile at %02d:%02d",
                 hub.daily_run_time.hour, hub.daily_run_time.minute)


def _mp3_path(hub: HubData, name: str) -> Path:
    return (hub.mp3_dir / name).resolve()


def _resolve_today(hub: HubData) -> Resolution:
    return resolve_active(hub.schedules, hub.default_mp3, date.today())


def _next_change(hub: HubData) -> tuple[date, str] | None:
    today = date.today()
    current = _resolve_today(hub)
    current_mp3 = current.mp3
    for offset in range(1, 366):
        d = today + timedelta(days=offset)
        future = resolve_active(hub.schedules, hub.default_mp3, d)
        if future.mp3 != current_mp3:
            label = future.schedule.name if future.schedule else "default"
            return d, f"{label} → {future.mp3}"
    return None


async def _async_apply_one(hub: HubData, state: DeviceState, mp3_filename: str, *,
                           force: bool = False) -> tuple[bool, str]:
    if not state.enabled:
        return False, "device disabled"
    if state.last_applied_mp3 == mp3_filename and not force:
        return True, "already current (skipped)"

    path = _mp3_path(hub, mp3_filename)
    if not path.exists():
        msg = f"MP3 missing: {path}"
        state.last_error = msg
        return False, msg

    client = DoorBirdClient(state.creds, client=hub.http)
    try:
        result = await client.set_button_sound(path)
    except DoorBirdError as exc:
        state.last_error = str(exc)
        return False, str(exc)
    except Exception as exc:
        state.last_error = f"unexpected: {exc!r}"
        _LOGGER.exception("apply crash for %s", state.creds.name)
        return False, state.last_error

    state.last_applied_mp3 = mp3_filename
    state.last_applied_at = datetime.now(UTC)
    state.last_error = None
    return True, result


async def _async_reconcile(hass: HomeAssistant, hub: HubData, *, force: bool = False) -> dict:
    res = _resolve_today(hub)
    hub.last_resolution = res
    hub.next_change = _next_change(hub)

    applied = failed = 0
    details: list[dict] = []
    for state in hub.devices.values():
        ok, msg = await _async_apply_one(hub, state, res.mp3, force=force)
        details.append({"device": state.creds.name, "ok": ok, "message": msg})
        if ok:
            applied += 1
        else:
            failed += 1
            _LOGGER.warning("reconcile: %s -> %s", state.creds.name, msg)

    hub.last_reconcile = datetime.now(UTC)
    async_dispatcher_send(hass, SIGNAL_RECONCILED)
    _LOGGER.info("reconcile done: active=%s reason=%s applied=%d failed=%d",
                 res.mp3, res.reason, applied, failed)
    return {
        "active_mp3": res.mp3,
        "reason": res.reason,
        "applied": applied,
        "failed": failed,
        "details": details,
    }


async def _async_register_services(hass: HomeAssistant) -> None:
    async def apply_now(call: ServiceCall) -> None:
        hub: HubData = hass.data[DOMAIN]
        await _async_reconcile(hass, hub, force=bool(call.data.get(ATTR_FORCE, False)))

    async def set_button_sound(call: ServiceCall) -> None:
        hub: HubData = hass.data[DOMAIN]
        mp3 = call.data[ATTR_MP3]
        target_names = call.data.get(ATTR_DEVICES)
        targets = _select_devices(hub, target_names)
        for state in targets:
            ok, msg = await _async_apply_one(hub, state, mp3, force=True)
            _LOGGER.info("set_button_sound %s: %s -> %s", state.creds.name, mp3, msg)
            if not ok:
                raise HomeAssistantError(f"{state.creds.name}: {msg}")
        async_dispatcher_send(hass, SIGNAL_RECONCILED)

    async def activate_builtin(call: ServiceCall) -> None:
        hub: HubData = hass.data[DOMAIN]
        sound = call.data[ATTR_SOUND]
        target_names = call.data.get(ATTR_DEVICES)
        targets = _select_devices(hub, target_names)
        for state in targets:
            client = DoorBirdClient(state.creds, client=hub.http)
            try:
                await client.activate_button_sound(sound)
                state.last_applied_mp3 = f"<builtin:{sound}>"
                state.last_applied_at = datetime.now(UTC)
                state.last_error = None
                _LOGGER.info("activate_builtin %s: %s", state.creds.name, sound)
            except DoorBirdError as exc:
                state.last_error = str(exc)
                raise HomeAssistantError(f"{state.creds.name}: {exc}") from exc
        async_dispatcher_send(hass, SIGNAL_RECONCILED)

    async def test_connection(call: ServiceCall) -> None:
        hub: HubData = hass.data[DOMAIN]
        target_names = call.data.get(ATTR_DEVICES)
        targets = _select_devices(hub, target_names)
        for state in targets:
            client = DoorBirdClient(state.creds, client=hub.http)
            ok, msg = await client.test_connection()
            state.test_status = ("ok: " if ok else "fail: ") + msg
            _LOGGER.info("test_connection %s: %s", state.creds.name, state.test_status)
        async_dispatcher_send(hass, SIGNAL_RECONCILED)

    hass.services.async_register(DOMAIN, SERVICE_APPLY_NOW, apply_now,
                                 schema=vol.Schema({vol.Optional(ATTR_FORCE, default=False): cv.boolean}))
    hass.services.async_register(DOMAIN, SERVICE_SET_BUTTON_SOUND, set_button_sound,
                                 schema=vol.Schema({
                                     vol.Required(ATTR_MP3): cv.string,
                                     vol.Optional(ATTR_DEVICES): vol.All(cv.ensure_list, [cv.string]),
                                 }))
    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE_BUILTIN, activate_builtin,
                                 schema=vol.Schema({
                                     vol.Required(ATTR_SOUND): cv.string,
                                     vol.Optional(ATTR_DEVICES): vol.All(cv.ensure_list, [cv.string]),
                                 }))
    hass.services.async_register(DOMAIN, SERVICE_TEST_CONNECTION, test_connection,
                                 schema=vol.Schema({
                                     vol.Optional(ATTR_DEVICES): vol.All(cv.ensure_list, [cv.string]),
                                 }))


def _select_devices(hub: HubData, names: list[str] | None) -> list[DeviceState]:
    if not names:
        return [d for d in hub.devices.values() if d.enabled]
    missing = [n for n in names if n not in hub.devices]
    if missing:
        raise HomeAssistantError(f"unknown device(s): {missing}")
    return [hub.devices[n] for n in names]
