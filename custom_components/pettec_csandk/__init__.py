"""PetTec (Snoop Cube) — csandk Fork integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PHONE_CODE,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_PHONE_CODE,
    DOMAIN,
)
from .coordinator import PettecCoordinator
from .meari_api import (
    MeariApiError,
    MeariAuthError,
    MeariClient,
    MeariSessionBumpedError,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
]


@dataclass
class PettecData:
    """Runtime data attached to the config entry."""

    client: MeariClient
    feeders: list[dict]      # subset of all_devices that are feeders
    all_devices: list[dict]  # every camera + feeder for the account
    coordinator: PettecCoordinator


type PettecConfigEntry = ConfigEntry[PettecData]


async def async_setup_entry(hass: HomeAssistant, entry: PettecConfigEntry) -> bool:
    """Login, discover devices, build coordinator, forward to platforms."""
    # Dedicated aiohttp session — isolates our auth from HA's shared session,
    # which interferes with Meari's per-token session tracking.
    http = async_create_clientsession(
        hass,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": "PettecHA/0.2"},
    )
    client = MeariClient(
        http=http,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        country_code=entry.data.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE),
        phone_code=entry.data.get(CONF_PHONE_CODE, DEFAULT_PHONE_CODE),
    )
    try:
        await client.login()
        all_devices = await client.list_devices_with_retry()
    except MeariAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except MeariSessionBumpedError as err:
        raise ConfigEntryNotReady(
            "Meari session was invalidated. HA will retry."
        ) from err
    except MeariApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    feeders = [d for d in all_devices if MeariClient.is_feeder(d)]
    _LOGGER.info(
        "PetTec setup: %d device(s), %d feeder(s)", len(all_devices), len(feeders)
    )

    coordinator = PettecCoordinator(hass, client, all_devices)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PettecData(
        client=client,
        feeders=feeders,
        all_devices=all_devices,
        coordinator=coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PettecConfigEntry) -> bool:
    """Unload platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
