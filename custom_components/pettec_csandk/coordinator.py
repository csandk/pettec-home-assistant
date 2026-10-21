"""DataUpdateCoordinator for the PetTec integration.

Polls the Meari cloud once per hour for state of all cameras + feeders on
the account using a single batch IoT call (the same endpoint the Snoop
Cube home view uses). One HTTP request fetches state for every device,
including dormant battery cameras.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .meari_api import (
    BATCH_READ_PROPS,
    MeariApiError,
    MeariAuthError,
    MeariClient,
    MeariSessionBumpedError,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=1)


class PettecCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls per-device IoT state, keyed by serial number.

    coordinator.data shape:
        {
            "ppsc642116f069554396": {                # Cam Buddy
                "118": 1, "114": 1, "115": "59.463G",
                "_online": True, "_status": "online",
            },
            "ppsld26b0afe10ea4939": {                # battery cam, dormant
                "154": 89, "1007": 70, "_online": True, "_status": "dormancy",
            },
            "ppscaada2b01e7ab4013": {                # offline (no Wi-Fi)
                "_online": False, "_status": "notfound",
            },
        }

    Entities read via .get() on missing properties → unavailable, no crash.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: MeariClient,
        devices: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.devices = devices

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Step 1 — per-device liveness. Cheap individual calls; tells us
        # which devices are online/dormant/offline/notfound. Without this,
        # cached prop 118 makes the switch falsely "on" for unreachable cams.
        statuses: dict[str, str] = {}
        for device in self.devices:
            sn = device.get("snNum")
            if not sn:
                continue
            try:
                statuses[sn] = await self._status_with_retry(sn)
            except (MeariApiError, MeariAuthError) as err:
                _LOGGER.warning("PetTec: status query failed for %s: %s", sn, err)
                statuses[sn] = "unknown"

        # Step 2 — batch IoT fetch for every device in one call. Includes
        # dormant ones (cloud-cached state).
        sn_list = [d["snNum"] for d in self.devices if d.get("snNum")]
        try:
            batch = await self._batch_with_retry(sn_list, BATCH_READ_PROPS)
        except (MeariApiError, MeariAuthError) as err:
            raise UpdateFailed(f"batch IoT fetch failed: {err}") from err

        # Step 3 — assemble the final state per device.
        result: dict[str, dict[str, Any]] = {}
        for device in self.devices:
            sn = device.get("snNum")
            if not sn:
                continue
            status = statuses.get(sn, "unknown")
            online = status in ("online", "dormancy")
            props = batch.get(sn, {})
            result[sn] = {
                **props,
                "_online": online,
                "_status": status,
            }

        if not result:
            raise UpdateFailed("No device state retrieved")
        return result

    async def _status_with_retry(self, sn: str, retries: int = 2) -> str:
        for attempt in range(retries + 1):
            try:
                return await self.client.get_device_status(sn)
            except MeariSessionBumpedError:
                if attempt == retries:
                    raise
                await asyncio.sleep(0.3 + attempt * 0.3)
                await self.client.login()
        return "unknown"

    async def _batch_with_retry(
        self, sn_list: list[str], props: list[str], retries: int = 2
    ) -> dict[str, dict[str, Any]]:
        for attempt in range(retries + 1):
            try:
                return await self.client.get_iot_batch(sn_list, props)
            except MeariSessionBumpedError:
                if attempt == retries:
                    raise
                _LOGGER.info("PetTec: session bumped, re-logging in")
                await asyncio.sleep(0.3 + attempt * 0.3)
                await self.client.login()
        return {}
