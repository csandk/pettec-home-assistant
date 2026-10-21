"""Config flow for PetTec — single step, email + password."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PHONE_CODE,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_PHONE_CODE,
    DOMAIN,
    NAME,
)
from .meari_api import MeariApiError, MeariAuthError, MeariClient, MeariSessionBumpedError

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COUNTRY_CODE, default=DEFAULT_COUNTRY_CODE): str,
        vol.Optional(CONF_PHONE_CODE, default=DEFAULT_PHONE_CODE): str,
    }
)


class PettecConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the PetTec config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            import aiohttp
            client = MeariClient(
                http=async_create_clientsession(
                    self.hass,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    headers={"User-Agent": "PettecHA/0.1"},
                ),
                email=email,
                password=user_input[CONF_PASSWORD],
                country_code=user_input.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE),
                phone_code=user_input.get(CONF_PHONE_CODE, DEFAULT_PHONE_CODE),
            )
            try:
                await client.login()
                devices = await client.list_devices_with_retry()
            except MeariAuthError:
                errors["base"] = "invalid_auth"
            except MeariSessionBumpedError as err:
                _LOGGER.warning("PetTec session invalidated during setup: %s", err)
                errors["base"] = "logged_elsewhere"
            except MeariApiError as err:
                _LOGGER.warning("PetTec API error during setup: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during PetTec setup")
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    return self.async_create_entry(
                        title=f"{NAME} ({email})",
                        data=user_input,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
