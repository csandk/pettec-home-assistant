"""Number entities — sensitivity sliders for the various detectors."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PettecConfigEntry
from .const import DOMAIN
from .coordinator import PettecCoordinator
from .meari_api import (
    IOT_PROP_MOTION_SENS,
    IOT_PROP_PIR_SENS,
    IOT_PROP_SOUND_SENS,
    DeviceOfflineError,
    MeariApiError,
    MeariAuthError,
    MeariClient,
    MeariSessionBumpedError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _NumberDef:
    description: NumberEntityDescription
    prop_id: str
    applies: Callable[[dict[str, Any]], bool] = lambda d: True


def _device_caps(device) -> dict[str, Any]:
    try:
        return json.loads(device.get("capability") or "{}").get("caps", {})
    except (TypeError, ValueError):
        return {}


def _has_battery(device) -> bool:
    return bool(_device_caps(device).get("bat"))


# Most Meari sensitivity sliders use 1..N integer ranges. Empirically the
# UI lets users pick 1..10 or 1..6 depending on the setting. We expose a
# generous 0..10 range and let the device clamp on the server side.
_DEFAULT_RANGE = (0, 10, 1)  # min, max, step


NUMBER_DEFS: list[_NumberDef] = [
    _NumberDef(
        NumberEntityDescription(
            key="motion_sensitivity",
            translation_key="motion_sensitivity",
            icon="mdi:motion-sensor",
            native_min_value=_DEFAULT_RANGE[0],
            native_max_value=_DEFAULT_RANGE[1],
            native_step=_DEFAULT_RANGE[2],
            mode=NumberMode.SLIDER,
        ),
        IOT_PROP_MOTION_SENS,
    ),
    _NumberDef(
        NumberEntityDescription(
            key="sound_sensitivity",
            translation_key="sound_sensitivity",
            icon="mdi:volume-high",
            native_min_value=_DEFAULT_RANGE[0],
            native_max_value=_DEFAULT_RANGE[1],
            native_step=_DEFAULT_RANGE[2],
            mode=NumberMode.SLIDER,
        ),
        IOT_PROP_SOUND_SENS,
    ),
    _NumberDef(
        NumberEntityDescription(
            key="pir_sensitivity",
            translation_key="pir_sensitivity",
            icon="mdi:motion-sensor",
            native_min_value=_DEFAULT_RANGE[0],
            native_max_value=_DEFAULT_RANGE[1],
            native_step=_DEFAULT_RANGE[2],
            mode=NumberMode.SLIDER,
        ),
        IOT_PROP_PIR_SENS,
        applies=_has_battery,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PettecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[PettecNumber] = []
    for device in data.all_devices:
        if not device.get("snNum"):
            continue
        for ndef in NUMBER_DEFS:
            if ndef.applies(device):
                entities.append(
                    PettecNumber(data.coordinator, data.client, device, ndef)
                )
    async_add_entities(entities)


class PettecNumber(CoordinatorEntity[PettecCoordinator], NumberEntity):
    """Generic number slider backed by an IoT property."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PettecCoordinator,
        client: MeariClient,
        device: dict[str, Any],
        ndef: _NumberDef,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._sn = device["snNum"]
        self._ndef = ndef
        self.entity_description = ndef.description

        device_name = device.get("deviceName") or "PetTec Device"
        firmware = device.get("deviceVersionID") or ""
        is_feeder = MeariClient.is_feeder(device)
        self._attr_unique_id = f"{self._sn}_{ndef.description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=device_name,
            manufacturer="PetTec",
            model="Cam Buddy" if is_feeder else "Camera",
            sw_version=firmware,
        )

    @property
    def _device_state(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._sn) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        s = self._device_state
        if not s:
            return False
        # Online or dormant — for dormant cams we transparently wake first.
        if s.get("_status") not in ("online", "dormancy"):
            return False
        return self.native_value is not None

    @property
    def native_value(self) -> float | None:
        s = self._device_state
        if not s:
            return None
        raw = s.get(self._ndef.prop_id)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info(
            "PetTec: %s set %s=%s", self._sn, self._ndef.description.key, value
        )
        # Wake dormant battery cams before writing
        s = self._device_state or {}
        if s.get("_status") == "dormancy":
            _LOGGER.info("PetTec: %s is dormant, sending wake first", self._sn)
            try:
                await self._client.wake_device(self._sn)
            except (MeariAuthError, MeariSessionBumpedError):
                await self._client.login()
                await self._client.wake_device(self._sn)
            except MeariApiError as err:
                raise HomeAssistantError(f"Wake failed: {err}") from err
            await asyncio.sleep(2.0)

        try:
            await self._client.set_number(self._sn, self._ndef.prop_id, int(value))
        except (MeariAuthError, MeariSessionBumpedError):
            await self._client.login()
            await self._client.set_number(self._sn, self._ndef.prop_id, int(value))
        except DeviceOfflineError as err:
            raise HomeAssistantError(
                f"Device is offline (no Wi-Fi or unreachable): {err}"
            ) from err
        except MeariApiError as err:
            raise HomeAssistantError(f"Number command failed: {err}") from err
        await self.coordinator.async_request_refresh()
