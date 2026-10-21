"""Feed-one-portion button for each PetTec feeder discovered at setup."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PettecConfigEntry
from .const import DOMAIN
from .meari_api import MeariApiError, MeariAuthError, MeariClient, MeariSessionBumpedError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PettecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a Feed One Portion button for each feeder."""
    data = entry.runtime_data
    entities: list[FeedOnePortionButton] = [
        FeedOnePortionButton(data.client, data.coordinator, feeder)
        for feeder in data.feeders
    ]
    async_add_entities(entities)


class FeedOnePortionButton(ButtonEntity):
    """Trigger one feed portion on a Cam Buddy."""

    _attr_has_entity_name = True
    _attr_translation_key = "feed_one_portion"
    _attr_icon = "mdi:food-drumstick"

    def __init__(self, client: MeariClient, coordinator, device: dict) -> None:
        self._client = client
        self._coordinator = coordinator
        self._sn = device["snNum"]
        device_name = device.get("deviceName") or "PetTec Feeder"
        firmware = device.get("deviceVersionID") or ""

        self._attr_name = "Feed one portion"
        self._attr_unique_id = f"{self._sn}_feed_one_portion"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            name=device_name,
            manufacturer="PetTec",
            model="Cam Buddy",
            sw_version=firmware,
        )

    async def async_press(self) -> None:
        """Dispense one portion."""
        _LOGGER.info("PetTec: feed one portion → %s (%s)", self._attr_name, self._sn)
        try:
            await self._client.feed_one_portion(self._sn, portions=1)
        except (MeariAuthError, MeariSessionBumpedError):
            _LOGGER.info("PetTec: re-logging in and retrying")
            await self._client.login()
            await self._client.feed_one_portion(self._sn, portions=1)
        except MeariApiError as err:
            raise HomeAssistantError(f"Feed command failed: {err}") from err
        # Trigger a coordinator refresh so today_feed_count etc. update soon.
        await self._coordinator.async_request_refresh()
