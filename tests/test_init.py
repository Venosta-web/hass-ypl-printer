"""Tests for the integration and its entry lifecycle (spec §4.2)."""

import asyncio
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration

from custom_components.ypl_printer import YplPrinterData, async_setup
from custom_components.ypl_printer.const import DOMAIN

from .bluetooth import (
    PRINTER_ADDRESS,
    PRINTER_NAME,
    PRINTER_UNIQUE_ID,
    inject_advertisement,
    service_info,
)

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

OTHER_ADDRESS = "11:22:33:44:55:66"


def _entry(
    hass: HomeAssistant, address: str = PRINTER_ADDRESS, title: str = PRINTER_NAME
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=dr.format_mac(address),
        data={CONF_ADDRESS: address},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_integration_loads(hass: HomeAssistant) -> None:
    """Home Assistant finds the custom integration and its setup succeeds."""
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.domain == DOMAIN
    assert integration.config_flow
    assert await async_setup(hass, {})


async def test_setup_registers_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    """Setup creates one device keyed by the normalised address."""
    entry = _entry(hass)
    await _setup(hass, entry)

    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {(DOMAIN, PRINTER_UNIQUE_ID)}
    assert device.connections == {(dr.CONNECTION_BLUETOOTH, PRINTER_UNIQUE_ID)}
    assert device.config_entries == {entry.entry_id}
    assert device.name == PRINTER_NAME
    # No manufacturer or model metadata without evidence for it.
    assert device.manufacturer is None
    assert device.model is None


async def test_setup_does_not_connect(hass: HomeAssistant) -> None:
    """Setup neither resolves nor connects to the printer."""
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address"
        ) as resolve,
        patch("bleak_retry_connector.establish_connection") as connect,
        patch(
            "custom_components.ypl_printer.config_flow.establish_connection"
        ) as flow_connect,
    ):
        await _setup(hass, _entry(hass))

    resolve.assert_not_called()
    connect.assert_not_called()
    flow_connect.assert_not_called()


async def test_setup_runtime_data(hass: HomeAssistant) -> None:
    """Runtime data holds only the raw address, the name and a lock."""
    entry = _entry(hass)
    await _setup(hass, entry)

    runtime = entry.runtime_data
    assert isinstance(runtime, YplPrinterData)
    assert runtime.address == PRINTER_ADDRESS
    assert runtime.name == PRINTER_NAME
    assert isinstance(runtime.lock, asyncio.Lock)
    assert set(vars(runtime)) == {"address", "name", "lock"}


async def test_reload_updates_same_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    """Setting up again updates the device instead of adding another."""
    entry = _entry(hass)
    await _setup(hass, entry)
    first_lock = entry.runtime_data.lock

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 1
    assert entry.runtime_data.lock is not first_lock


async def test_each_printer_has_its_own_lock(hass: HomeAssistant) -> None:
    """Every entry gets a separate device and lock."""
    first = _entry(hass)
    second = _entry(hass, OTHER_ADDRESS, "Y50-5678")
    # Setting up the integration sets up every entry.
    await _setup(hass, first)
    assert second.state is ConfigEntryState.LOADED

    assert first.runtime_data.lock is not second.runtime_data.lock


async def test_unload_keeps_service(hass: HomeAssistant) -> None:
    """Unloading an entry leaves services registered by the integration."""

    async def handler(call: ServiceCall) -> None:
        """Stand in for the integration-level print action."""

    entry = _entry(hass)
    await _setup(hass, entry)
    hass.services.async_register(DOMAIN, "print", handler)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, "print")


async def test_unload_clears_runtime_data_only_for_that_entry(
    hass: HomeAssistant,
) -> None:
    """Unload drops the entry's runtime data and leaves other printers alone."""
    first = _entry(hass)
    second = _entry(hass, OTHER_ADDRESS, "Y50-5678")
    # Setting up the integration sets up every entry.
    await _setup(hass, first)
    assert second.state is ConfigEntryState.LOADED
    second_runtime = second.runtime_data

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()

    assert not hasattr(first, "runtime_data")
    assert second.state is ConfigEntryState.LOADED
    assert second.runtime_data is second_runtime


async def test_running_job_keeps_runtime_after_unload(hass: HomeAssistant) -> None:
    """A job holding the runtime reference finishes after the entry unloads."""
    entry = _entry(hass)
    await _setup(hass, entry)
    runtime = entry.runtime_data
    job_may_finish = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def job() -> None:
        async with runtime.lock:
            await job_may_finish.wait()
            assert runtime.address == PRINTER_ADDRESS
        cleaned_up.set()

    # Untracked, like a service call that outlives the entry.
    task = asyncio.create_task(job())
    await asyncio.sleep(0)
    assert runtime.lock.locked()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not task.done()

    job_may_finish.set()
    await task
    assert cleaned_up.is_set()
    assert not runtime.lock.locked()


async def test_removal_requests_rediscovery(hass: HomeAssistant) -> None:
    """Removing an entry offers the printer for discovery again."""
    inject_advertisement(hass, service_info())
    await hass.async_block_till_done(wait_background_tasks=True)
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        hass.config_entries.flow.async_abort(flow["flow_id"])

    entry = _entry(hass)
    await _setup(hass, entry)

    with patch(
        "homeassistant.components.bluetooth.async_rediscover_address",
        wraps=bluetooth.async_rediscover_address,
    ) as rediscover:
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    rediscover.assert_called_once_with(hass, PRINTER_ADDRESS)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_BLUETOOTH
    assert flows[0]["context"]["unique_id"] == PRINTER_UNIQUE_ID
    assert flows[0]["step_id"] == "bluetooth_confirm"
