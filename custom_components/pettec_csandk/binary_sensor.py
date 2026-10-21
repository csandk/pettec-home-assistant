"""Binary sensors for PetTec feeders.

- food_empty: derived from prop 337 (outFoodDet, "minutes of food shortage").
  > 0 → food empty / out.
- desiccant_expired: derived from prop 339 (desiccantInfo JSON, expiry_days
  ≤ 0 OR status flag).

Both are device_class=PROBLEM so HA renders them with a warning icon.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PettecConfigEntry
from .const import DOMAIN
from .coordinator import PettecCoordinator
from .meari_api import (
    IOT_PROP_CHARGE_STATUS,
    IOT_PROP_DESICCANT_INFO,
    IOT_PROP_OUT_FOOD_DET,
    IOT_PROP_PET_THROW_WARNING,
    MeariClient,
)

_LOGGER = logging.getLogger(__name__)


def _is_food_empty(raw: Any) -> bool | None:
    """Property 337 = minutes the device has been 'out of food'. >0 → empty."""
    if raw is None:
        return None
    try:
        return int(raw) > 0
    except (TypeError, ValueError):
        return None


def _is_desiccant_expired(raw: Any) -> bool | None:
    """Property 339 ships JSON like {"expiry_days": N, "status": M}.
    Expired if status indicates expiry or expiry_days <= 0.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        data = raw
    else:
        return None
    if not isinstance(data, dict):
        return None
    # Status semantics aren't fully nailed down; treat any non-zero status or
    # expiry_days <= 0 as "needs attention".
    status = data.get("status")
    expiry_days = data.get("expiry_days")
    if isinstance(expiry_days, (int, float)) and expiry_days <= 0:
        return True
    if isinstance(status, (int, float)) and status != 0:
        return True
    return False


def _is_charging(raw):
    if raw is None:
        return None
    try:
        return int(raw) > 0
    except (TypeError, ValueError):
        return None


def _is_pet_throw_warning(raw):
    if raw is None:
        return None
    try:
        return int(raw) > 0
    except (TypeError, ValueError):
        return None


FEEDER_BINARY_SENSORS: list[
    tuple[BinarySensorEntityDescription, str, callable]
] = [
    (
        BinarySensorEntityDescription(
            key="food_empty",
            translation_key="food_empty",
            device_class=BinarySensorDeviceClass.PROBLEM,
            icon="mdi:food-off",
        ),
        IOT_PROP_OUT_FOOD_DET,
        _is_food_empty,
    ),
    (
        BinarySensorEntityDescription(
            key="desiccant_expired",
            translation_key="desiccant_expired",
            device_class=BinarySensorDeviceClass.PROBLEM,
            icon="mdi:silica-gel-package",
        ),
        IOT_PROP_DESICCANT_INFO,
        _is_desiccant_expired,
    ),
    (
        BinarySensorEntityDescription(
            key="pet_throw_warning",
            translation_key="pet_throw_warning",
            device_class=BinarySensorDeviceClass.PROBLEM,
            icon="mdi:bowl-alert",
        ),
        IOT_PROP_PET_THROW_WARNING,
        _is_pet_throw_warning,
    ),
]


# Common across cameras + feeders (battery cams only really)
COMMON_BINARY_SENSORS: list[
    tuple[BinarySensorEntityDescription, str, callable]
] = [
    (
        BinarySensorEntityDescription(
            key="charging",
            translation_key="charging",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        ),
        IOT_PROP_CHARGE_STATUS,
        _is_charging,
    ),
]


def _device_has_battery(device) -> bool:
    cap_str = device.get("capability") or "{}"
    try:
        import json
        return bool(json.loads(cap_str).get("caps", {}).get("bat"))
    except (TypeError, ValueError):
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PettecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    coordinator = data.coordinator
    entities: list[PettecBinarySensor] = []

    for device in data.all_devices:
        # Common: charging — only for battery devices
        if _device_has_battery(device):
            for desc, prop, value_fn in COMMON_BINARY_SENSORS:
                entities.append(
                    PettecBinarySensor(coordinator, device, desc, prop, value_fn)
                )
        # Feeder-only
        if MeariClient.is_feeder(device):
            for desc, prop, value_fn in FEEDER_BINARY_SENSORS:
                entities.append(
                    PettecBinarySensor(coordinator, device, desc, prop, value_fn)
                )

    async_add_entities(entities)


class PettecBinarySensor(CoordinatorEntity[PettecCoordinator], BinarySensorEntity):
    """Binary sensor backed by one IoT property of a feeder."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PettecCoordinator,
        device: dict[str, Any],
        description: BinarySensorEntityDescription,
        prop_id: str,
        value_fn: callable,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = device["snNum"]
        self._prop_id = prop_id
        self._value_fn = value_fn

        device_name = device.get("deviceName") or "PetTec Feeder"
        firmware = device.get("deviceVersionID") or ""
        self._attr_unique_id = f"{self._sn}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=device_name,
            manufacturer="PetTec",
            model="Cam Buddy",
            sw_version=firmware,
        )

    @property
    def _device_state(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._sn) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        s = self._device_state
        if not s or not s.get("_online", False):
            return False
        return self._value_fn(s.get(self._prop_id)) is not None

    @property
    def is_on(self) -> bool | None:
        s = self._device_state
        if not s:
            return None
        return self._value_fn(s.get(self._prop_id))
