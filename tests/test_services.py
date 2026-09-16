"""Tests for the print action contract (spec §5.1-§5.3, §5.8-§5.9, §7, §9)."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import attr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol
import yaml

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.ypl_printer import protocol, renderer
from custom_components.ypl_printer.const import DOMAIN
from custom_components.ypl_printer.exceptions import PrintOutcomeUncertainError
from custom_components.ypl_printer.protocol import PrinterStatus, RasterError
from custom_components.ypl_printer.transport import TransmitResult

from .bluetooth import PRINTER_ADDRESS, PRINTER_NAME
from .const import ACCEPTANCE_SPECIMEN
from .test_renderer import GOLDEN_SPECIMEN_RASTER_SHA256

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
TRANSMIT = "custom_components.ypl_printer.transport.async_transmit"
RESOLVE_DEVICE = "homeassistant.components.bluetooth.async_ble_device_from_address"
OTHER_ADDRESS = "11:22:33:44:55:66"

# Spec §7: every key with exactly its placeholders.
UNCERTAIN_PLACEHOLDERS = {"name", "sent", "total", "flags", "raw"}
EXCEPTION_PLACEHOLDERS: dict[str, set[str]] = {
    "invalid_target": {"device_id"},
    "printer_not_loaded": {"name"},
    "empty_text": set(),
    "unsupported_character": {"position", "codepoint"},
    "text_does_not_fit": {"lines"},
    "render_failed": {"error"},
    "printer_busy": {"name"},
    "printer_not_found": {"name", "reachability"},
    "no_connection_slots": {"name"},
    "connection_failed": {"name", "error"},
    "unsupported_profile": {"name"},
    "communication_failed": {"name", "error"},
    "printer_not_ready": {"name", "flags", "raw"},
    "outcome_uncertain_transport": UNCERTAIN_PLACEHOLDERS,
    "outcome_uncertain_printer_error": UNCERTAIN_PLACEHOLDERS,
    "outcome_uncertain_still_printing": UNCERTAIN_PLACEHOLDERS,
}

RESPONSE_KEYS = {
    "device_id",
    "raster_width",
    "raster_height",
    "raster_sha256",
    "encoded_bytes",
    "ble_chunks",
    "printer_status",
}


@pytest.fixture
def transmit() -> Generator[AsyncMock]:
    """Replace the BLE transport with a fake that completes with ready."""
    result = TransmitResult(ble_chunks=62, printer_status=PrinterStatus(0))
    with patch(TRANSMIT, AsyncMock(return_value=result)) as fake:
        yield fake


@pytest.fixture
def resolve_device() -> Generator[AsyncMock]:
    """Watch for any Bluetooth device resolution."""
    with patch(RESOLVE_DEVICE) as fake:
        yield fake


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


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0].id


async def _printer(hass: HomeAssistant) -> tuple[MockConfigEntry, str]:
    """Set up one loaded printer and return its entry and device ID."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, _device_id(hass, entry)


async def _print(
    hass: HomeAssistant, data: dict[str, Any], return_response: bool = False
) -> Any:
    return await hass.services.async_call(
        DOMAIN, "print", data, blocking=True, return_response=return_response
    )


def _raster_bytes(raster: list[list[int]]) -> bytes:
    return bytes(pixel for row in raster for pixel in row)


# --- §5.1 registration and schema (test 1) -------------------------------------


async def test_registered_once_at_integration_setup(hass: HomeAssistant) -> None:
    """The action exists after integration setup, with optional response."""
    assert await async_setup_component(hass, DOMAIN, {})

    assert hass.services.has_service(DOMAIN, "print")
    assert (
        hass.services.supports_response(DOMAIN, "print") is SupportsResponse.OPTIONAL
    )
    assert list(hass.services.async_services_for_domain(DOMAIN)) == ["print"]


async def test_service_survives_unload(hass: HomeAssistant) -> None:
    """Unloading the only printer leaves the action registered."""
    entry, _ = await _printer(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "print")


def test_services_yaml_matches_spec() -> None:
    """The action description has exactly the two specified fields."""
    services = yaml.safe_load((INTEGRATION_DIR / "services.yaml").read_text())

    assert services == {
        "print": {
            "fields": {
                "device_id": {
                    "required": True,
                    "selector": {
                        "device": {
                            "filter": {"integration": DOMAIN},
                            "multiple": False,
                        }
                    },
                },
                "text": {
                    "required": True,
                    "selector": {"text": {"multiline": True}},
                },
            }
        }
    }


async def test_valid_call_accepted(hass: HomeAssistant, transmit: AsyncMock) -> None:
    """Exactly the two fields are accepted."""
    _, device_id = await _printer(hass)

    assert await _print(hass, {"device_id": device_id, "text": "Hi"}) is None
    transmit.assert_awaited_once()


@pytest.mark.parametrize(
    "extra",
    [
        {"width": 400},
        {"height": 240},
        {"density": 8},
        {"rotation": 90},
        {"copies": 2},
        {"preview": True},
        {"font": "DejaVu Sans"},
        {"alignment": "left"},
        {"raster": [[0]]},
        {"image": "label.png"},
        {"payload": {"elements": []}},
        {"entity_id": "sensor.label"},
        {"area_id": "kitchen"},
    ],
)
async def test_extra_fields_rejected(
    hass: HomeAssistant,
    transmit: AsyncMock,
    resolve_device: AsyncMock,
    extra: dict[str, Any],
) -> None:
    """The runtime schema rejects every field but device_id and text."""
    _, device_id = await _printer(hass)

    with pytest.raises(vol.Invalid):
        await _print(hass, {"device_id": device_id, "text": "Hi", **extra})

    transmit.assert_not_called()
    resolve_device.assert_not_called()


@pytest.mark.parametrize("text", [None, 5, ["Hi"], {"text": "Hi"}, b"Hi"])
async def test_text_must_be_a_string(
    hass: HomeAssistant, transmit: AsyncMock, text: Any
) -> None:
    """Label text is a required string and is never coerced."""
    _, device_id = await _printer(hass)
    data = {"device_id": device_id}
    if text is not None:
        data["text"] = text

    with pytest.raises(vol.Invalid):
        await _print(hass, data)

    transmit.assert_not_called()


@pytest.mark.parametrize(
    ("data", "placeholder"),
    [
        ({"text": "Hi"}, ""),
        ({"device_id": "", "text": "Hi"}, ""),
        ({"device_id": None, "text": "Hi"}, ""),
        ({"device_id": 5, "text": "Hi"}, "5"),
    ],
)
async def test_missing_or_malformed_device_id(
    hass: HomeAssistant,
    transmit: AsyncMock,
    data: dict[str, Any],
    placeholder: str,
) -> None:
    """A missing or non-string device ID is an invalid target."""
    await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, data)

    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"device_id": placeholder}
    transmit.assert_not_called()


async def test_device_id_cardinality(hass: HomeAssistant, transmit: AsyncMock) -> None:
    """A list of device IDs, even of one, is not a scalar target."""
    _, device_id = await _printer(hass)

    for device_ids in ([device_id], [device_id, device_id]):
        with pytest.raises(ServiceValidationError) as err:
            await _print(hass, {"device_id": device_ids, "text": "Hi"})
        assert err.value.translation_key == "invalid_target"

    # A service-call target is merged into the data the same way.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "print",
            {"text": "Hi"},
            blocking=True,
            target={"device_id": [device_id]},
        )
    assert err.value.translation_key == "invalid_target"
    transmit.assert_not_called()


# --- §5.2 target resolution (test 2) -------------------------------------------


async def test_unknown_device(
    hass: HomeAssistant, transmit: AsyncMock, resolve_device: AsyncMock
) -> None:
    """A device ID that is not in the registry is an invalid target."""
    await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": "not-a-device", "text": "Hi"})

    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"device_id": "not-a-device"}
    transmit.assert_not_called()
    resolve_device.assert_not_called()


async def test_foreign_device(
    hass: HomeAssistant, transmit: AsyncMock, device_registry: dr.DeviceRegistry
) -> None:
    """A device of another integration is rejected despite the selector."""
    await _printer(hass)
    foreign_entry = MockConfigEntry(domain="other")
    foreign_entry.add_to_hass(hass)
    foreign = device_registry.async_get_or_create(
        config_entry_id=foreign_entry.entry_id,
        identifiers={("other", "printer")},
    )

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": foreign.id, "text": "Hi"})

    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"device_id": foreign.id}
    transmit.assert_not_called()


async def test_device_cannot_map_to_several_entries(
    hass: HomeAssistant, transmit: AsyncMock, device_registry: dr.DeviceRegistry
) -> None:
    """An ambiguous target cannot be built: a device has exactly one entry.

    Core 2026.9 refuses to add a second config entry to a device, so the
    action resolves ``config_entry_id`` and the device keeps printing on its
    own printer.
    """
    entry, device_id = await _printer(hass)
    second = _entry(hass, OTHER_ADDRESS, "Y50-5678")
    assert await hass.config_entries.async_setup(second.entry_id)

    with pytest.raises(RuntimeError, match="single config entry"):
        device_registry.async_update_device(
            device_id, add_config_entry_id=second.entry_id
        )

    await _print(hass, {"device_id": device_id, "text": "Hi"})
    assert transmit.await_args.args[1] is entry.runtime_data


async def test_device_of_missing_entry(
    hass: HomeAssistant, transmit: AsyncMock, device_registry: dr.DeviceRegistry
) -> None:
    """A device whose config entry is gone is an invalid target."""
    _, device_id = await _printer(hass)
    orphan = attr.evolve(device_registry.async_get(device_id), config_entry_id="gone")

    with (
        patch.object(device_registry, "async_get", return_value=orphan),
        pytest.raises(ServiceValidationError) as err,
    ):
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"device_id": device_id}
    transmit.assert_not_called()


async def test_unloaded_printer(
    hass: HomeAssistant, transmit: AsyncMock, resolve_device: AsyncMock
) -> None:
    """A printer whose entry is not loaded is named in the error."""
    entry, device_id = await _printer(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert err.value.translation_key == "printer_not_loaded"
    assert err.value.translation_placeholders == {"name": PRINTER_NAME}
    transmit.assert_not_called()
    resolve_device.assert_not_called()


async def test_each_device_resolves_to_its_own_printer(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """With two printers, each device ID selects its own runtime data."""
    first, first_id = await _printer(hass)
    second = _entry(hass, OTHER_ADDRESS, "Y50-5678")
    assert await hass.config_entries.async_setup(second.entry_id)
    second_id = _device_id(hass, second)

    await _print(hass, {"device_id": second_id, "text": "Hi"})
    await _print(hass, {"device_id": first_id, "text": "Hi"})

    printers = [call.args[1] for call in transmit.await_args_list]
    assert printers == [second.runtime_data, first.runtime_data]


async def test_target_checked_before_text(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """Target resolution fails before the text is validated or rendered."""
    await _printer(hass)

    with (
        patch.object(renderer, "render_label") as render,
        pytest.raises(ServiceValidationError) as err,
    ):
        await _print(hass, {"device_id": "not-a-device", "text": "\t"})

    assert err.value.translation_key == "invalid_target"
    render.assert_not_called()


# --- §5.3 label text through the action (tests 3-5) ----------------------------


@pytest.mark.parametrize("text", ["", " ", "\n", " \n \r\n\r  "])
async def test_empty_text(hass: HomeAssistant, transmit: AsyncMock, text: str) -> None:
    """Empty and whitespace-only text is rejected with no placeholders."""
    _, device_id = await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": device_id, "text": text})

    assert err.value.translation_key == "empty_text"
    assert not err.value.translation_placeholders
    transmit.assert_not_called()


@pytest.mark.parametrize(
    ("text", "position", "codepoint"),
    [
        ("A\tB", "1", "U+0009"),
        ("\x1f", "0", "U+001F"),
        ("abc\x7f", "3", "U+007F"),
        ("café", "3", "U+00E9"),
        (" ", "0", "U+00A0"),
        ("A\r\nB ", "3", "U+2028"),
        ("A\r\n\x00", "2", "U+0000"),
        ("ok \U0001f5a8", "3", "U+1F5A8"),
    ],
)
async def test_unsupported_character(
    hass: HomeAssistant,
    transmit: AsyncMock,
    text: str,
    position: str,
    codepoint: str,
) -> None:
    """The first rejected code point is reported after newline normalisation."""
    _, device_id = await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": device_id, "text": text})

    assert err.value.translation_key == "unsupported_character"
    assert err.value.translation_placeholders == {
        "position": position,
        "codepoint": codepoint,
    }
    transmit.assert_not_called()


@pytest.mark.parametrize(
    ("text", "lines"),
    [("\n".join(["A"] * 12), "12"), ("W" * 60, "1"), ("A\n" + "W" * 60, "2")],
)
async def test_text_does_not_fit(
    hass: HomeAssistant, transmit: AsyncMock, text: str, lines: str
) -> None:
    """Text that overflows at size 12 is rejected, never wrapped or clipped."""
    _, device_id = await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": device_id, "text": text})

    assert err.value.translation_key == "text_does_not_fit"
    assert err.value.translation_placeholders == {"lines": lines}
    transmit.assert_not_called()


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("A\r\nB", "A\nB"),
        ("A\rB", "A\nB"),
        ("  A  ", "  A  "),
        ("A\n\nB", "A\n\nB"),
        ("A\n", "A\n"),
        ("\nA", "\nA"),
        ("A\r\n\r\n", "A\n\n"),
    ],
)
async def test_text_rendered_as_normalised(
    hass: HomeAssistant, transmit: AsyncMock, text: str, normalized: str
) -> None:
    """The action renders the normalised text with every character kept."""
    _, device_id = await _printer(hass)

    response = await _print(hass, {"device_id": device_id, "text": text}, True)

    raster = renderer.render_label(normalized)
    assert response["raster_sha256"] == renderer.raster_sha256(raster)
    assert transmit.await_args.args[2] == protocol.build_print_stream(raster)


async def test_spaces_and_blank_lines_change_the_label(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """Leading, trailing and blank-line content is never trimmed away."""
    _, device_id = await _printer(hass)

    digests = set()
    for text in ("A", " A", "A ", "A\n", "\nA", "A\n\nB", "A\nB"):
        response = await _print(hass, {"device_id": device_id, "text": text}, True)
        digests.add(response["raster_sha256"])

    assert len(digests) == 7


# --- render faults and ordering (tests 6-7) ------------------------------------


async def test_specimen_golden_raster(hass: HomeAssistant, transmit: AsyncMock) -> None:
    """The acceptance specimen produces the pinned raster through the action."""
    _, device_id = await _printer(hass)

    response = await _print(
        hass, {"device_id": device_id, "text": ACCEPTANCE_SPECIMEN}, True
    )

    assert response["raster_sha256"] == GOLDEN_SPECIMEN_RASTER_SHA256


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("render_label", renderer.RenderError("bad raster")),
        ("render_label", OSError("font missing")),
        ("render_label", MemoryError()),
        ("build_print_stream", RasterError("wrong width")),
    ],
)
async def test_render_fault(
    hass: HomeAssistant,
    transmit: AsyncMock,
    resolve_device: AsyncMock,
    target: str,
    error: Exception,
) -> None:
    """Faults not caused by the caller are render_failed, before connecting."""
    _, device_id = await _printer(hass)
    module = renderer if target == "render_label" else protocol

    with (
        patch.object(module, target, side_effect=error),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert not isinstance(err.value, ServiceValidationError)
    assert err.value.translation_key == "render_failed"
    assert err.value.translation_placeholders == {"error": type(error).__name__}
    assert err.value.__cause__ is error
    transmit.assert_not_called()
    resolve_device.assert_not_called()


async def test_render_fault_on_invalid_raster(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """A renderer that returns a wrong-shaped raster fails closed."""
    _, device_id = await _printer(hass)

    with (
        patch.object(renderer, "render_label", return_value=[[0] * 399] * 240),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert err.value.translation_key == "render_failed"
    assert err.value.translation_placeholders == {"error": "RasterError"}
    transmit.assert_not_called()


async def test_render_runs_off_the_event_loop(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """Rendering and encoding run in an executor and finish before sending."""
    _, device_id = await _printer(hass)
    order: list[str] = []

    async def add_executor_job(target: Any, *args: Any) -> Any:
        order.append("executor")
        return target(*args)

    transmit.side_effect = lambda *args: order.append("transmit") or TransmitResult(
        0, None
    )
    with patch.object(hass, "async_add_executor_job", add_executor_job):
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert order == ["executor", "transmit"]


async def test_no_connection_attempt_on_validation_failures(
    hass: HomeAssistant, transmit: AsyncMock, resolve_device: AsyncMock
) -> None:
    """No validation failure reaches the transport or Bluetooth."""
    _, device_id = await _printer(hass)
    failures = [
        {"text": "Hi"},
        {"device_id": [device_id], "text": "Hi"},
        {"device_id": "not-a-device", "text": "Hi"},
        {"device_id": device_id},
        {"device_id": device_id, "text": "Hi", "copies": 2},
        {"device_id": device_id, "text": ""},
        {"device_id": device_id, "text": "\t"},
        {"device_id": device_id, "text": "W" * 60},
    ]

    for data in failures:
        with pytest.raises((vol.Invalid, ServiceValidationError)):
            await _print(hass, data)

    transmit.assert_not_called()
    resolve_device.assert_not_called()


# --- §5.9 diagnostic response (test 8) -----------------------------------------


async def test_response_omitted(hass: HomeAssistant, transmit: AsyncMock) -> None:
    """Without a response request the action returns nothing."""
    _, device_id = await _printer(hass)

    assert await _print(hass, {"device_id": device_id, "text": "Hi"}) is None
    transmit.assert_awaited_once()


async def test_response_shape(hass: HomeAssistant, transmit: AsyncMock) -> None:
    """The response has exactly the specified keys and values."""
    entry, device_id = await _printer(hass)

    response = await _print(
        hass, {"device_id": device_id, "text": ACCEPTANCE_SPECIMEN}, True
    )

    raster = renderer.render_label(ACCEPTANCE_SPECIMEN)
    segments = protocol.build_print_stream(raster)
    assert response == {
        "device_id": device_id,
        "raster_width": 400,
        "raster_height": 240,
        "raster_sha256": hashlib.sha256(_raster_bytes(raster)).hexdigest(),
        "encoded_bytes": len(b"".join(segments)),
        "ble_chunks": 62,
        "printer_status": {"raw": 0, "flags": ["ready"], "unknown_bits": 0},
    }
    assert set(response) == RESPONSE_KEYS
    # The response is plain JSON.
    assert json.loads(json.dumps(response)) == response
    transmit.assert_awaited_once_with(hass, entry.runtime_data, segments)


async def test_response_raster_hash_input(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """The hash covers 96,000 row-major bytes, one 0 or 1 per dot."""
    _, device_id = await _printer(hass)
    raster = [[0] * 400 for _ in range(240)]
    raster[0][1] = 1
    raster[239][399] = 1

    with patch.object(renderer, "render_label", return_value=raster):
        response = await _print(hass, {"device_id": device_id, "text": "Hi"}, True)

    data = _raster_bytes(raster)
    assert len(data) == 96_000
    assert data[1] == 1 and data[-1] == 1
    assert response["raster_sha256"] == hashlib.sha256(data).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", response["raster_sha256"])
    segments = protocol.build_print_stream(raster)
    assert response["encoded_bytes"] == len(b"".join(segments))


async def test_response_counts_come_from_the_job(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """ble_chunks is what the transport reports, not what was planned."""
    _, device_id = await _printer(hass)
    transmit.return_value = TransmitResult(ble_chunks=7, printer_status=None)

    response = await _print(hass, {"device_id": device_id, "text": "Hi"}, True)

    assert response["ble_chunks"] == 7
    assert response["encoded_bytes"] == sum(
        len(segment) for segment in transmit.await_args.args[2]
    )


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (None, None),
        (0x00, {"raw": 0, "flags": ["ready"], "unknown_bits": 0}),
        (0x01, {"raw": 1, "flags": ["printing"], "unknown_bits": 0}),
        (0x06, {"raw": 6, "flags": ["cover_open", "paper_out"], "unknown_bits": 0}),
        (
            0x1F,
            {
                "raw": 0x1F,
                "flags": [
                    "printing",
                    "cover_open",
                    "paper_out",
                    "undervoltage",
                    "overheat",
                ],
                "unknown_bits": 0,
            },
        ),
        (0x20, {"raw": 0x20, "flags": [], "unknown_bits": 0x20}),
        (0x1A4, {"raw": 0x1A4, "flags": ["paper_out"], "unknown_bits": 0x1A0}),
    ],
)
async def test_response_printer_status(
    hass: HomeAssistant,
    transmit: AsyncMock,
    raw: int | None,
    status: dict[str, Any] | None,
) -> None:
    """The status is nullable, flags are in bit order, unknown bits are kept."""
    _, device_id = await _printer(hass)
    transmit.return_value = TransmitResult(
        ble_chunks=62, printer_status=None if raw is None else PrinterStatus(raw)
    )

    response = await _print(hass, {"device_id": device_id, "text": "Hi"}, True)

    assert response["printer_status"] == status


# --- §7 translated exceptions (test 9) and no replay (test 10) -----------------


def _placeholders(message: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", message))


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_exception_translations(path: str) -> None:
    """Every §7 key is defined with exactly its placeholders."""
    strings = json.loads((INTEGRATION_DIR / path).read_text(encoding="utf-8"))
    exceptions = strings["exceptions"]

    assert set(exceptions) == set(EXCEPTION_PLACEHOLDERS)
    for key, placeholders in EXCEPTION_PLACEHOLDERS.items():
        message = exceptions[key]["message"]
        assert _placeholders(message) == placeholders, key


def test_uncertain_messages_warn_against_resending() -> None:
    """Uncertain outcomes say the label may have printed and must not be resent."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    exceptions = strings["exceptions"]

    for key in EXCEPTION_PLACEHOLDERS:
        if key.startswith("outcome_uncertain_"):
            message = exceptions[key]["message"]
            assert "may or may not have printed" in message
            assert "Do not resend it automatically" in message
            assert "check the printer" in message


def test_service_translations() -> None:
    """The action and its two fields have names and descriptions."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    service = strings["services"]["print"]

    assert service["name"] and service["description"]
    assert set(service["fields"]) == {"device_id", "text"}
    for field in service["fields"].values():
        assert field["name"] and field["description"]


async def test_validation_message_is_translated(
    hass: HomeAssistant, transmit: AsyncMock
) -> None:
    """Raised validation errors render their English message."""
    _, device_id = await _printer(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _print(hass, {"device_id": device_id, "text": "A\tB"})

    assert str(err.value) == (
        "The label text contains the unsupported character U+0009 at position 1. "
        "Only Basic Latin characters (U+0020 to U+007E) and line breaks are allowed"
    )


def _uncertain(key: str) -> PrintOutcomeUncertainError:
    return PrintOutcomeUncertainError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={
            "name": PRINTER_NAME,
            "sent": "3",
            "total": "62",
            "flags": "cover_open",
            "raw": "0x02",
        },
    )


EXECUTION_ERRORS = [
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="printer_busy",
        translation_placeholders={"name": PRINTER_NAME},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="printer_not_found",
        translation_placeholders={"name": PRINTER_NAME, "reachability": "No adapter."},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="no_connection_slots",
        translation_placeholders={"name": PRINTER_NAME},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="connection_failed",
        translation_placeholders={"name": PRINTER_NAME, "error": "BleakError"},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="unsupported_profile",
        translation_placeholders={"name": PRINTER_NAME},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="communication_failed",
        translation_placeholders={"name": PRINTER_NAME, "error": "TimeoutError"},
    ),
    HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="printer_not_ready",
        translation_placeholders={
            "name": PRINTER_NAME,
            "flags": "cover_open, paper_out",
            "raw": "0x06",
        },
    ),
    _uncertain("outcome_uncertain_transport"),
    _uncertain("outcome_uncertain_printer_error"),
    _uncertain("outcome_uncertain_still_printing"),
]


@pytest.mark.parametrize(
    "error", EXECUTION_ERRORS, ids=[error.translation_key for error in EXECUTION_ERRORS]
)
async def test_transport_errors_raised_translated_and_never_replayed(
    hass: HomeAssistant, transmit: AsyncMock, error: HomeAssistantError
) -> None:
    """Transport failures propagate unchanged, once, never as response data."""
    _, device_id = await _printer(hass)
    transmit.side_effect = error

    with pytest.raises(HomeAssistantError) as raised:
        await _print(hass, {"device_id": device_id, "text": "Hi"}, True)

    assert raised.value is error
    transmit.assert_awaited_once()
    message = str(raised.value)
    assert PRINTER_NAME in message
    assert "{" not in message
    if isinstance(error, PrintOutcomeUncertainError):
        assert "may or may not have printed" in message
        assert "3 of 62" in message


def test_uncertain_error_is_a_home_assistant_error() -> None:
    """Uncertain outcomes are one HomeAssistantError subclass."""
    assert issubclass(PrintOutcomeUncertainError, HomeAssistantError)
    assert not issubclass(PrintOutcomeUncertainError, ServiceValidationError)


async def test_no_replay_after_uncertain_outcome(
    hass: HomeAssistant, transmit: AsyncMock, resolve_device: AsyncMock
) -> None:
    """An uncertain job is sent once; a new call is a new, separate job."""
    _, device_id = await _printer(hass)
    transmit.side_effect = [
        _uncertain("outcome_uncertain_transport"),
        TransmitResult(62, None),
    ]

    with pytest.raises(PrintOutcomeUncertainError):
        await _print(hass, {"device_id": device_id, "text": "Hi"})
    assert transmit.await_count == 1

    # Only the caller decides to print again.
    await _print(hass, {"device_id": device_id, "text": "Hi"})
    assert transmit.await_count == 2
    resolve_device.assert_not_called()


@pytest.mark.parametrize("error", [asyncio.CancelledError(), TimeoutError()])
async def test_other_transport_exceptions_propagate_without_replay(
    hass: HomeAssistant, transmit: AsyncMock, error: BaseException
) -> None:
    """Cancellation and unexpected errors are re-raised unchanged, once."""
    _, device_id = await _printer(hass)
    transmit.side_effect = error

    with pytest.raises(type(error)) as raised:
        await _print(hass, {"device_id": device_id, "text": "Hi"})

    assert raised.value is error
    transmit.assert_awaited_once()
