"""Switch entities for PetTec cameras + feeders.

Each switch wraps one IoT property that's a simple on/off (1/0) toggle.
Camera "active" switch (DP 118) is tri-state (on/off/privacy) — privacy is
mapped to off for now; v0.3 may surface it as a separate select.

Capability filtering keeps the entity registry clean:
  - Battery-cam switches (PIR) only created when caps.bat=1.
  - PTZ switches (human tracking) only when caps.ptz=1.
  - Feeder switches (pet alarm, meow) only for feeders.
  - Generic toggles (recording, motion, human, sound) created for every
    device — they show as unavailable if the device doesn't expose the prop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PettecConfigEntry
from .const import DOMAIN
from .coordinator import PettecCoordinator
from .meari_api import (
    IOT_PROP_CAM_ACTIVE,
    IOT_PROP_CRY_DET,
    IOT_PROP_HUMAN_DET,
    IOT_PROP_HUMAN_TRACK,
    IOT_PROP_MOTION_DET,
    IOT_PROP_PET_ALARM_ENABLE,
    IOT_PROP_PET_MEOW,
    IOT_PROP_PIR_DET,
    IOT_PROP_RECORDING,
    IOT_PROP_SOUND_DET,
    DeviceOfflineError,
    MeariApiError,
    MeariAuthError,
    MeariClient,
    MeariSessionBumpedError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SwitchDef:
    description: SwitchEntityDescription
    prop_id: str
    # Map raw IoT value → bool|None. Defaults to int(raw) == 1.
    is_on_fn: Callable[[Any], bool | None] | None = None
    # When True, writes are inverted: switch ON → write "0", OFF → write "1".
    # Needed for prop 118 (sleepMode) where reading 0 means "active".
    inverted_write: bool = False
    # Whether this switch applies to the device.
    applies: Callable[[dict[str, Any]], bool] = lambda d: True


def _bool_default(raw):
    if raw is None:
        return None
    try:
        return int(raw) == 1
    except (TypeError, ValueError):
        return None


def _device_caps(device) -> dict[str, Any]:
    try:
        return json.loads(device.get("capability") or "{}").get("caps", {})
    except (TypeError, ValueError):
        return {}


def _has_battery(device) -> bool:
    return bool(_device_caps(device).get("bat"))


def _has_ptz(device) -> bool:
    caps = _device_caps(device)
    return bool(caps.get("ptz") or caps.get("ptz2"))


# ---- entity definitions ------------------------------------------------------

SWITCH_DEFS: list[_SwitchDef] = [
    _SwitchDef(
        SwitchEntityDescription(
            key="camera_active",
            translation_key="camera_active",
            icon="mdi:cctv",
        ),
        IOT_PROP_CAM_ACTIVE,
        is_on_fn=lambda raw: MeariClient._state_value_is_active(raw),
        inverted_write=True,  # 118 is sleepMode: 0 means "active"
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="recording",
            translation_key="recording",
            icon="mdi:record-rec",
        ),
        IOT_PROP_RECORDING,
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="motion_detection",
            translation_key="motion_detection",
            icon="mdi:motion-sensor",
        ),
        IOT_PROP_MOTION_DET,
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="human_detection",
            translation_key="human_detection",
            icon="mdi:account-eye",
        ),
        IOT_PROP_HUMAN_DET,
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="sound_detection",
            translation_key="sound_detection",
            icon="mdi:volume-high",
        ),
        IOT_PROP_SOUND_DET,
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="cry_detection",
            translation_key="cry_detection",
            icon="mdi:account-voice",
        ),
        IOT_PROP_CRY_DET,
    ),
    # PTZ-only
    _SwitchDef(
        SwitchEntityDescription(
            key="human_tracking",
            translation_key="human_tracking",
            icon="mdi:account-search",
        ),
        IOT_PROP_HUMAN_TRACK,
        applies=_has_ptz,
    ),
    # Battery-cam-only
    _SwitchDef(
        SwitchEntityDescription(
            key="pir_detection",
            translation_key="pir_detection",
            icon="mdi:motion-sensor",
        ),
        IOT_PROP_PIR_DET,
        applies=_has_battery,
    ),
    # Feeder-only
    _SwitchDef(
        SwitchEntityDescription(
            key="pet_alarm",
            translation_key="pet_alarm",
            icon="mdi:bell",
        ),
        IOT_PROP_PET_ALARM_ENABLE,
        applies=MeariClient.is_feeder,
    ),
    _SwitchDef(
        SwitchEntityDescription(
            key="pet_meow",
            translation_key="pet_meow",
            icon="mdi:account-voice",
        ),
        IOT_PROP_PET_MEOW,
        applies=MeariClient.is_feeder,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PettecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[PettecSwitch] = []
    for device in data.all_devices:
        if not device.get("snNum"):
            continue
        for sdef in SWITCH_DEFS:
            if sdef.applies(device):
                entities.append(
                    PettecSwitch(data.coordinator, data.client, device, sdef)
                )
    async_add_entities(entities)


class PettecSwitch(CoordinatorEntity[PettecCoordinator], SwitchEntity):
    """Generic switch backed by an IoT property of a PetTec device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PettecCoordinator,
        client: MeariClient,
        device: dict[str, Any],
        sdef: _SwitchDef,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._sn = device["snNum"]
        self._sdef = sdef
        self.entity_description = sdef.description

        device_name = device.get("deviceName") or "PetTec Device"
        firmware = device.get("deviceVersionID") or ""
        is_feeder = MeariClient.is_feeder(device)
        self._attr_unique_id = f"{self._sn}_{sdef.description.key}"
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
        status = s.get("_status")
        # Online: full control. Dormancy (battery cam asleep) is also
        # available because we transparently wake the device before writes.
        if status not in ("online", "dormancy"):
            return False
        is_on = self._is_on_value(s.get(self._sdef.prop_id))
        return is_on is not None

    @property
    def is_on(self) -> bool | None:
        s = self._device_state
        if not s:
            return None
        return self._is_on_value(s.get(self._sdef.prop_id))

    def _is_on_value(self, raw):
        fn = self._sdef.is_on_fn or _bool_default
        return fn(raw)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        _LOGGER.info(
            "PetTec: %s set %s=%s", self._sn, self._sdef.description.key, on
        )
        # Apply write inversion if the property's true semantic is the inverse
        # of what HA users expect (currently only prop 118 / sleepMode).
        write_value = on if not self._sdef.inverted_write else not on

        # Battery cams in dormancy reject writes with errid=404. Wake them
        # first if needed.
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
            # Brief delay for the camera to come online
            await asyncio.sleep(2.0)

        try:
            await self._client.set_toggle(self._sn, self._sdef.prop_id, write_value)
        except (MeariAuthError, MeariSessionBumpedError):
            await self._client.login()
            await self._client.set_toggle(self._sn, self._sdef.prop_id, write_value)
        except DeviceOfflineError as err:
            raise HomeAssistantError(
                f"Device is offline (no Wi-Fi or unreachable): {err}"
            ) from err
        except MeariApiError as err:
            raise HomeAssistantError(f"Switch command failed: {err}") from err
        await self.coordinator.async_request_refresh()
