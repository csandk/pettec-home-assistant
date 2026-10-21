"""Regression coverage for camera-only accounts and device discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.pettec_csandk import PLATFORMS, async_setup_entry
from custom_components.pettec_csandk import button, switch
from custom_components.pettec_csandk.config_flow import PettecConfigFlow
from custom_components.pettec_csandk.meari_api import (
    MeariApiError,
    MeariAuthError,
    MeariClient,
    MeariSessionBumpedError,
)

CAMERA = {"snNum": "camera", "capability": '{"caps":{"ptz":1}}'}
BATTERY_CAMERA = {"snNum": "battery", "capability": '{"caps":{"bat":1}}'}
FEEDER = {"snNum": "feeder", "capability": '{"caps":{"pet":1}}'}
CREDENTIALS = {"email": "owner@example.com", "password": "test-password"}

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"ipc": [CAMERA]}, [CAMERA]),
        ({"ipc": None, "snap": [BATTERY_CAMERA]}, [BATTERY_CAMERA]),
        ({"ipc": [FEEDER]}, [FEEDER]),
        ({"ipc": [CAMERA, FEEDER], "snap": [BATTERY_CAMERA]},
         [CAMERA, FEEDER, BATTERY_CAMERA]),
        ({}, []),
        ({"ipc": [], "snap": None}, []),
    ],
)
async def test_config_flow_accepts_cameras_and_feeders(response, expected):
    """Any discovered camera or feeder permits setup; empty accounts do not."""
    flow = PettecConfigFlow()
    flow.hass = Mock()
    with (
        patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
        patch.object(flow, "_abort_if_unique_id_configured"),
        patch("custom_components.pettec_csandk.config_flow.async_create_clientsession"),
        patch.object(MeariClient, "login", new_callable=AsyncMock),
        patch.object(MeariClient, "get_device_list", return_value=response),
    ):
        result = await flow.async_step_user(CREDENTIALS)

    if expected:
        assert result["type"] == "create_entry"
        assert result["data"] == CREDENTIALS
    else:
        assert result["type"] == "form"
        assert result["errors"] == {"base": "no_devices"}


@pytest.mark.parametrize(
    "error,expected",
    [
        (MeariAuthError, "invalid_auth"),
        (MeariApiError, "cannot_connect"),
        (MeariSessionBumpedError, "logged_elsewhere"),
    ],
)
async def test_config_flow_discovery_errors(error, expected):
    """Discovery failures retain their existing user-facing errors."""
    flow = PettecConfigFlow()
    flow.hass = Mock()
    with (
        patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
        patch.object(flow, "_abort_if_unique_id_configured"),
        patch("custom_components.pettec_csandk.config_flow.async_create_clientsession"),
        patch.object(MeariClient, "login", new_callable=AsyncMock),
        patch.object(MeariClient, "get_device_list", side_effect=error),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await flow.async_step_user(CREDENTIALS)

    assert result["errors"] == {"base": expected}


async def test_discovery_recovers_after_session_bump():
    client = MeariClient(http=Mock(), **CREDENTIALS)
    with (
        patch.object(client, "get_device_list", side_effect=[
            MeariSessionBumpedError(), {"snap": [BATTERY_CAMERA]},
        ]) as get_devices,
        patch.object(client, "login", new_callable=AsyncMock) as login,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        assert await client.list_devices_with_retry() == [BATTERY_CAMERA]
    assert get_devices.await_count == 2
    login.assert_awaited_once()


async def test_discovery_stops_after_retry_limit():
    client = MeariClient(http=Mock(), **CREDENTIALS)
    with (
        patch.object(client, "get_device_list", side_effect=MeariSessionBumpedError)
        as get_devices,
        patch.object(client, "login", new_callable=AsyncMock) as login,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(MeariSessionBumpedError):
            await client.list_devices_with_retry(retries=2)
    assert get_devices.await_count == 3
    assert login.await_count == 2


async def test_feeder_helpers_still_filter_cameras():
    client = MeariClient(http=Mock(), **CREDENTIALS)
    with patch.object(client, "get_device_list", return_value={
        "ipc": [CAMERA, FEEDER], "snap": [BATTERY_CAMERA],
    }):
        assert await client.list_feeders() == [FEEDER]
        assert await client.list_feeders_with_retry() == [FEEDER]


@pytest.mark.parametrize("devices", [[CAMERA], [FEEDER], [CAMERA, FEEDER]])
async def test_entry_setup_and_entity_filtering(devices):
    """Camera-only setup succeeds and feed controls remain feeder-only."""
    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = SimpleNamespace(data=CREDENTIALS)
    with (
        patch("custom_components.pettec_csandk.async_create_clientsession"),
        patch.object(MeariClient, "login", new_callable=AsyncMock),
        patch.object(MeariClient, "get_device_list", return_value={"ipc": devices}),
        patch("custom_components.pettec_csandk.PettecCoordinator") as coordinator_class,
    ):
        coordinator = coordinator_class.return_value
        coordinator.async_config_entry_first_refresh = AsyncMock()
        assert await async_setup_entry(hass, entry)

    coordinator_class.assert_called_once_with(hass, entry.runtime_data.client, devices)
    coordinator.async_config_entry_first_refresh.assert_awaited_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS,
    )
    assert entry.runtime_data.all_devices == devices
    assert entry.runtime_data.feeders == ([FEEDER] if FEEDER in devices else [])

    add_buttons = Mock()
    await button.async_setup_entry(hass, entry, add_buttons)
    buttons = add_buttons.call_args.args[0]
    assert len(buttons) == (1 if FEEDER in devices else 0)

    add_switches = Mock()
    await switch.async_setup_entry(hass, entry, add_switches)
    ids = {entity.unique_id for entity in add_switches.call_args.args[0]}
    for device in devices:
        assert f"{device['snNum']}_camera_active" in ids
    assert "camera_pet_alarm" not in ids
    assert ("feeder_pet_alarm" in ids) == (FEEDER in devices)
