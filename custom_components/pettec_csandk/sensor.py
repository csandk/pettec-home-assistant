"""Read-only sensor entities for PetTec feeders.

All sensors source their state from `PettecCoordinator.data[sn][prop_id]`.
Sensors handle missing properties (server silently drops props the device
doesn't track) by reporting `None`, which HA renders as `unavailable`.

Entities are only created for devices that match. The Buddy gets all of them;
non-feeder cameras get just the SD-card and battery sensors.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PettecConfigEntry
from .const import DOMAIN
from .coordinator import PettecCoordinator
from .meari_api import (
    IOT_PROP_BATTERY_PERCENT,
    IOT_PROP_DESICCANT_INFO,
    IOT_PROP_FIRMWARE_VERSION,
    IOT_PROP_NEW_TODAY_FEED_PLAN,
    IOT_PROP_OUT_FOOD_DET,
    IOT_PROP_SD_CAPACITY,
    IOT_PROP_SD_REMAINING,
    IOT_PROP_SD_STATUS,
    IOT_PROP_WIFI_STRENGTH,
    MeariClient,
)

_LOGGER = logging.getLogger(__name__)


# SD card status enum values from APK ChimeSDBaseinfoPresenter
SD_STATUS_LABELS = {
    0: "ok",
    1: "mounted",
    3: "error",
    4: "full",
    5: "unformatted",
    6: "bad",
}


def _parse_today_plan(value: Any) -> list[dict[str, Any]]:
    """Property 344 ships a JSON string array of {time,count,enable} dicts."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def _today_feed_count(value: Any) -> int | None:
    plan = _parse_today_plan(value)
    if not plan:
        return None
    return sum(int(p.get("count", 0)) for p in plan if int(p.get("enable", 0)))


def _next_feed_time(value: Any) -> str | None:
    plan = _parse_today_plan(value)
    if not plan:
        return None
    now = datetime.now().strftime("%H:%M:%S")
    upcoming = [
        p.get("time")
        for p in plan
        if int(p.get("enable", 0)) and p.get("time", "") > now
    ]
    upcoming.sort()
    return upcoming[0] if upcoming else None


def _sd_status_label(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return SD_STATUS_LABELS.get(int(value), f"unknown ({value})")
    except (TypeError, ValueError):
        return None


def _battery_percent(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _food_out_minutes(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# (entity_description, value_fn, attrs_fn) per sensor.
# value_fn: receives raw IoT prop value → entity state.
# attrs_fn: optionally returns extra_state_attributes dict (or None).
FEEDER_SENSOR_DEFS: list[
    tuple[SensorEntityDescription, str, callable, callable | None]
] = [
    (
        SensorEntityDescription(
            key="today_feed_count",
            translation_key="today_feed_count",
            icon="mdi:food-drumstick",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        IOT_PROP_NEW_TODAY_FEED_PLAN,
        _today_feed_count,
        None,
    ),
    (
        SensorEntityDescription(
            key="next_feed_time",
            translation_key="next_feed_time",
            icon="mdi:clock-outline",
        ),
        IOT_PROP_NEW_TODAY_FEED_PLAN,
        _next_feed_time,
        lambda raw: {"plan": _parse_today_plan(raw)} if raw else None,
    ),
    (
        SensorEntityDescription(
            key="food_out_minutes",
            translation_key="food_out_minutes",
            icon="mdi:food-off",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="min",
        ),
        IOT_PROP_OUT_FOOD_DET,
        _food_out_minutes,
        None,
    ),
    (
        SensorEntityDescription(
            key="desiccant_info",
            translation_key="desiccant_info",
            icon="mdi:silica-gel-package",
        ),
        IOT_PROP_DESICCANT_INFO,
        # Just expose the JSON status; the binary_sensor handles "expired" derivation.
        lambda raw: raw if raw else None,
        None,
    ),
]

# Sensors that apply to ALL device types (cameras + feeders): SD card, battery
COMMON_SENSOR_DEFS: list[
    tuple[SensorEntityDescription, str, callable, callable | None]
] = [
    (
        SensorEntityDescription(
            key="sd_card_status",
            translation_key="sd_card_status",
            icon="mdi:sd",
        ),
        IOT_PROP_SD_STATUS,
        _sd_status_label,
        None,
    ),
    (
        SensorEntityDescription(
            key="sd_card_capacity",
            translation_key="sd_card_capacity",
            icon="mdi:sd",
        ),
        IOT_PROP_SD_CAPACITY,
        lambda raw: raw if raw else None,
        None,
    ),
    (
        SensorEntityDescription(
            key="sd_card_remaining",
            translation_key="sd_card_remaining",
            icon="mdi:sd",
        ),
        IOT_PROP_SD_REMAINING,
        lambda raw: raw if raw else None,
        None,
    ),
    (
        SensorEntityDescription(
            key="battery",
            translation_key="battery",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
        ),
        IOT_PROP_BATTERY_PERCENT,
        _battery_percent,
        None,
    ),
    (
        SensorEntityDescription(
            key="wifi_strength",
            translation_key="wifi_strength",
            icon="mdi:wifi",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
        ),
        IOT_PROP_WIFI_STRENGTH,
        _battery_percent,  # same int parser
        None,
    ),
    (
        SensorEntityDescription(
            key="firmware_version",
            translation_key="firmware_version",
            icon="mdi:chip",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        IOT_PROP_FIRMWARE_VERSION,
        lambda raw: str(raw) if raw else None,
        None,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PettecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensor entities for each device."""
    data = entry.runtime_data
    coordinator = data.coordinator
    entities: list[PettecSensor] = []

    for device in data.all_devices:
        sn = device.get("snNum")
        if not sn:
            continue
        is_feeder = MeariClient.is_feeder(device)

        # Common sensors (battery skipped for non-battery devices)
        for desc, prop, value_fn, attrs_fn in COMMON_SENSOR_DEFS:
            if desc.key == "battery" and not _device_has_battery(device):
                continue
            entities.append(
                PettecSensor(coordinator, device, desc, prop, value_fn, attrs_fn)
            )

        # Feeder-only sensors
        if is_feeder:
            for desc, prop, value_fn, attrs_fn in FEEDER_SENSOR_DEFS:
                entities.append(
                    PettecSensor(coordinator, device, desc, prop, value_fn, attrs_fn)
                )

    async_add_entities(entities)


def _device_has_battery(device: dict[str, Any]) -> bool:
    """True if the device's capability JSON includes bat:1."""
    cap_str = device.get("capability") or "{}"
    try:
        cap = json.loads(cap_str)
    except (TypeError, ValueError):
        return False
    return bool(cap.get("caps", {}).get("bat"))


class PettecSensor(CoordinatorEntity[PettecCoordinator], SensorEntity):
    """Generic sensor backed by one IoT property of one PetTec device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PettecCoordinator,
        device: dict[str, Any],
        description: SensorEntityDescription,
        prop_id: str,
        value_fn: callable,
        attrs_fn: callable | None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = device["snNum"]
        self._prop_id = prop_id
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn

        device_name = device.get("deviceName") or "PetTec Device"
        firmware = device.get("deviceVersionID") or ""
        is_feeder = MeariClient.is_feeder(device)
        self._attr_unique_id = f"{self._sn}_{description.key}"
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
        if not s or not s.get("_online", False):
            return False
        return self._value_fn(s.get(self._prop_id)) is not None

    @property
    def native_value(self):
        s = self._device_state
        if not s:
            return None
        return self._value_fn(s.get(self._prop_id))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._attrs_fn is None:
            return None
        s = self._device_state
        if not s:
            return None
        return self._attrs_fn(s.get(self._prop_id))
