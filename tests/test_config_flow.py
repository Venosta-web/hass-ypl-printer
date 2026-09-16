"""Tests for the discovery-only config flow (spec §4.1, §9 config flow)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import bleak
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakAbortedError,
    BleakConnectionError,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ypl_printer.const import (
    DOMAIN,
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
)

from .bluetooth import (
    ESTABLISH_CONNECTION,
    OTHER_SERVICE_UUID,
    PRINTER_ADDRESS,
    PRINTER_NAME,
    PRINTER_UNIQUE_ID,
    YPL_PROFILE,
    fake_client,
    gatt_services,
    inject_advertisement,
    service_info,
)

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
ABORT_KEYS = {"already_configured", "cannot_connect", "not_supported", "discovery_only"}

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def _discover(hass: HomeAssistant, **kwargs: Any) -> dict[str, Any]:
    """Advertise a printer and return the discovery flow it started."""
    inject_advertisement(hass, service_info(**kwargs))
    await hass.async_block_till_done(wait_background_tasks=True)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_BLUETOOTH
    return flows[0]


async def _confirm(hass: HomeAssistant, client: MagicMock | Exception) -> Any:
    """Discover a printer and confirm it, connecting to ``client``."""
    flow = await _discover(hass)
    if isinstance(client, Exception):
        connect = AsyncMock(side_effect=client)
    else:
        connect = AsyncMock(return_value=client)
    with patch(ESTABLISH_CONNECTION, connect):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    return result


async def test_discovery_shows_confirmation(hass: HomeAssistant) -> None:
    """A ``Y50*`` advertisement starts a flow that waits for confirmation."""
    with patch(ESTABLISH_CONNECTION) as connect:
        flow = await _discover(hass)

    assert flow["step_id"] == "bluetooth_confirm"
    assert flow["context"]["unique_id"] == PRINTER_UNIQUE_ID
    assert flow["context"]["title_placeholders"] == {"name": PRINTER_NAME}
    connect.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_other_advertisement_is_not_discovered(hass: HomeAssistant) -> None:
    """Advertisements whose name does not match ``Y50*`` are ignored."""
    inject_advertisement(hass, service_info(name="NIIMBOT-B1"))
    await hass.async_block_till_done()

    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_confirmation_form(hass: HomeAssistant) -> None:
    """The confirmation step is a confirm-only form naming the printer."""
    flow = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": PRINTER_NAME}


async def test_profile_check_passes_creates_entry(hass: HomeAssistant) -> None:
    """A printer with the YPL profile becomes an entry after disconnecting."""
    client = fake_client()
    flow = await _discover(hass)
    with patch(ESTABLISH_CONNECTION, AsyncMock(return_value=client)) as connect:
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PRINTER_NAME
    assert result["data"] == {CONF_ADDRESS: PRINTER_ADDRESS}
    assert result["result"].unique_id == PRINTER_UNIQUE_ID

    # Core's connector with its default retries, on a freshly resolved device,
    # through the client class Home Assistant's Bluetooth stack installs.
    connect.assert_awaited_once()
    args, kwargs = connect.call_args
    assert args[0] is bleak.BleakClient
    assert args[1].address == PRINTER_ADDRESS
    assert args[2] == PRINTER_NAME
    assert kwargs == {}

    client.disconnect.assert_awaited_once()
    client.start_notify.assert_not_called()
    client.write_gatt_char.assert_not_called()


@pytest.mark.parametrize(
    "services",
    [
        pytest.param(
            gatt_services((OTHER_SERVICE_UUID, YPL_PROFILE[1])), id="no_service"
        ),
        pytest.param(
            gatt_services((SERVICE_UUID, (WRITE_CHARACTERISTIC_UUID,))),
            id="no_notify",
        ),
        pytest.param(
            gatt_services((SERVICE_UUID, (NOTIFY_CHARACTERISTIC_UUID,))),
            id="no_write",
        ),
        pytest.param(
            gatt_services((SERVICE_UUID, ()), (OTHER_SERVICE_UUID, YPL_PROFILE[1])),
            id="characteristics_in_other_service",
        ),
    ],
)
async def test_profile_check_fails_not_supported(
    hass: HomeAssistant, services: BleakGATTServiceCollection
) -> None:
    """A missing service or characteristic aborts without an entry."""
    client = fake_client(services)
    result = await _confirm(hass, client)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"
    assert not hass.config_entries.async_entries(DOMAIN)
    client.disconnect.assert_awaited_once()
    client.start_notify.assert_not_called()
    client.write_gatt_char.assert_not_called()


async def test_unreachable_printer_cannot_connect(hass: HomeAssistant) -> None:
    """A printer Home Assistant cannot resolve aborts before connecting."""
    with patch(ESTABLISH_CONNECTION) as connect:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
        )
        assert result["step_id"] == "bluetooth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    connect.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    "error",
    [
        BleakNotFoundError("not found"),
        BleakConnectionError("failed"),
        BleakAbortedError("aborted"),
        BleakOutOfConnectionSlotsError("no slots"),
        BleakError("other"),
        TimeoutError(),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_connection_error_cannot_connect(
    hass: HomeAssistant, error: Exception
) -> None:
    """Every connection error aborts with ``cannot_connect``."""
    result = await _confirm(hass, error)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("services", "expected"),
    [
        pytest.param(None, FlowResultType.CREATE_ENTRY, id="supported"),
        pytest.param(
            gatt_services((SERVICE_UUID, ())),
            FlowResultType.ABORT,
            id="not_supported",
        ),
    ],
)
async def test_disconnect_failure_keeps_result(
    hass: HomeAssistant,
    services: BleakGATTServiceCollection | None,
    expected: FlowResultType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing disconnect is logged and does not change the check's result."""
    client = fake_client(services)
    client.disconnect.side_effect = BleakError("disconnect failed")
    result = await _confirm(hass, client)

    assert result["type"] is expected
    client.disconnect.assert_awaited_once()
    assert "disconnect after profile check failed" in caplog.text


@pytest.mark.parametrize("name", [None, ""])
async def test_unnamed_printer_title(hass: HomeAssistant, name: str | None) -> None:
    """Without an advertised name the title falls back to ``YPL printer``."""
    with patch(ESTABLISH_CONNECTION, AsyncMock(return_value=fake_client())):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(name=name)
        )
        assert result["description_placeholders"] == {"name": "YPL printer"}
        # The device must resolve for the check; advertise it under a name.
        inject_advertisement(hass, service_info(name=PRINTER_NAME))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "YPL printer"


async def test_configured_address_already_configured(hass: HomeAssistant) -> None:
    """Rediscovering a configured printer aborts, whatever the address case."""
    MockConfigEntry(
        domain=DOMAIN, unique_id=PRINTER_UNIQUE_ID, data={CONF_ADDRESS: PRINTER_ADDRESS}
    ).add_to_hass(hass)

    with patch(ESTABLISH_CONNECTION) as connect:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=service_info(address=PRINTER_ADDRESS.lower()),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    connect.assert_not_called()


async def test_user_step_discovery_only(hass: HomeAssistant) -> None:
    """Manual setup is refused."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "discovery_only"


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_abort_reasons_translated(path: str) -> None:
    """Every abort reason has a translation."""
    strings = json.loads((INTEGRATION_DIR / path).read_text(encoding="utf-8"))
    aborts = strings["config"]["abort"]

    assert set(aborts) == ABORT_KEYS
    assert all(isinstance(text, str) and text for text in aborts.values())


def test_english_translation_matches_strings() -> None:
    """The shipped English translation is ``strings.json`` verbatim."""
    strings = (INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8")
    english = (INTEGRATION_DIR / "translations" / "en.json").read_text(encoding="utf-8")

    assert json.loads(english) == json.loads(strings)
