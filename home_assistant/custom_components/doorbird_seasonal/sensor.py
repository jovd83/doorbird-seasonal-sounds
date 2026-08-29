"""Sensors for DoorBird Seasonal Sounds.

Hub-level sensors expose the resolved active sound, the reason it was chosen,
and the next scheduled change date. Per-device sensors track the last-applied
sound, the timestamp of the last apply, and any connection/test error.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import HubData
from .const import DOMAIN, SIGNAL_RECONCILED


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    hub: HubData = hass.data[DOMAIN]

    entities: list[SensorEntity] = [
        ActiveSoundSensor(hub),
        ActiveReasonSensor(hub),
        NextChangeSensor(hub),
        LastReconcileSensor(hub),
    ]
    for state in hub.devices.values():
        entities.append(DeviceLastAppliedSensor(hub, state.creds.name))
        entities.append(DeviceLastAppliedAtSensor(hub, state.creds.name))
        entities.append(DeviceErrorSensor(hub, state.creds.name))

    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_should_poll = False
    _hub: HubData

    def __init__(self, hub: HubData) -> None:
        self._hub = hub

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RECONCILED, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class ActiveSoundSensor(_Base):
    _attr_name = "DoorBird seasonal active sound"
    _attr_unique_id = f"{DOMAIN}_active_sound"
    _attr_icon = "mdi:music-note"

    @property
    def native_value(self) -> str | None:
        return self._hub.last_resolution.mp3 if self._hub.last_resolution else None


class ActiveReasonSensor(_Base):
    _attr_name = "DoorBird seasonal active reason"
    _attr_unique_id = f"{DOMAIN}_active_reason"
    _attr_icon = "mdi:calendar-question"

    @property
    def native_value(self) -> str | None:
        return self._hub.last_resolution.reason if self._hub.last_resolution else None


class NextChangeSensor(_Base):
    _attr_name = "DoorBird seasonal next change"
    _attr_unique_id = f"{DOMAIN}_next_change"
    _attr_icon = "mdi:calendar-arrow-right"

    @property
    def native_value(self) -> str | None:
        nc = self._hub.next_change
        return f"{nc[0].isoformat()} — {nc[1]}" if nc else None


class LastReconcileSensor(_Base):
    _attr_name = "DoorBird seasonal last reconcile"
    _attr_unique_id = f"{DOMAIN}_last_reconcile"
    _attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> str | None:
        ts = self._hub.last_reconcile
        return ts.isoformat() if ts else None


class _DeviceBase(_Base):
    def __init__(self, hub: HubData, device_name: str) -> None:
        super().__init__(hub)
        self._device_name = device_name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_name)},
            manufacturer="DoorBird",
            name=self._device_name,
            model="IP Video Door Station",
        )

    @property
    def _state(self):
        return self._hub.devices.get(self._device_name)


class DeviceLastAppliedSensor(_DeviceBase):
    _attr_icon = "mdi:bell-ring"

    @property
    def name(self) -> str:
        return f"{self._device_name} last applied sound"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self._device_name}_last_applied_sound"

    @property
    def native_value(self) -> str | None:
        return self._state.last_applied_mp3 if self._state else None


class DeviceLastAppliedAtSensor(_DeviceBase):
    _attr_icon = "mdi:clock-outline"

    @property
    def name(self) -> str:
        return f"{self._device_name} last applied at"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self._device_name}_last_applied_at"

    @property
    def native_value(self) -> str | None:
        if not self._state or not self._state.last_applied_at:
            return None
        return self._state.last_applied_at.isoformat()


class DeviceErrorSensor(_DeviceBase):
    _attr_icon = "mdi:alert-circle"

    @property
    def name(self) -> str:
        return f"{self._device_name} status"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self._device_name}_status"

    @property
    def native_value(self) -> str:
        if not self._state:
            return "unknown"
        if self._state.last_error:
            return f"error: {self._state.last_error[:80]}"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._state:
            return {}
        return {
            "test_status": self._state.test_status,
            "last_error_full": self._state.last_error,
        }
