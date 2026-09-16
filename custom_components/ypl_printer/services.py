"""The ``ypl_printer.print`` action (spec §5.1-§5.2, §5.8-§5.9, §7)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from . import protocol, renderer, transport
from .const import DOMAIN

if TYPE_CHECKING:
    from . import YplPrinterData

SERVICE_PRINT = "print"
ATTR_DEVICE_ID = "device_id"
ATTR_TEXT = "text"

_FIELDS_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): str, vol.Required(ATTR_TEXT): str},
    extra=vol.PREVENT_EXTRA,
)


def _print_schema(data: Any) -> dict[str, Any]:
    """Validate exactly one scalar device ID and the label text, nothing else.

    A missing or malformed ``device_id`` is ``invalid_target`` (spec §7);
    every other schema violation is a plain ``vol.Invalid``.
    """
    if isinstance(data, dict):
        device_id = data.get(ATTR_DEVICE_ID)
        if not isinstance(device_id, str) or not device_id:
            raise _invalid_target("" if device_id is None else str(device_id))
    return _FIELDS_SCHEMA(data)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration-level print action."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_PRINT,
        _async_print,
        schema=_print_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def _async_print(call: ServiceCall) -> ServiceResponse:
    """Validate, render and send one label, awaiting the whole job."""
    hass = call.hass
    device_id: str = call.data[ATTR_DEVICE_ID]
    printer = _resolve_printer(hass, device_id)
    raster, segments = await hass.async_add_executor_job(
        _prepare_job, call.data[ATTR_TEXT]
    )
    # Called exactly once: a failed or uncertain job is never replayed.
    result = await transport.async_transmit(hass, printer, segments)
    if not call.return_response:
        return None
    status = result.printer_status
    return {
        "device_id": device_id,
        "raster_width": renderer.RASTER_WIDTH,
        "raster_height": renderer.RASTER_HEIGHT,
        "raster_sha256": renderer.raster_sha256(raster),
        "encoded_bytes": sum(len(segment) for segment in segments),
        "ble_chunks": result.ble_chunks,
        "printer_status": None
        if status is None
        else {
            "raw": status.raw,
            "flags": list(status.flags),
            "unknown_bits": status.unknown_bits,
        },
    }


def _resolve_printer(hass: HomeAssistant, device_id: str) -> YplPrinterData:
    """Map a device ID to the runtime data of exactly one loaded entry."""
    # Core gives every device exactly one config entry, so a device can map
    # to several printer entries only if that invariant is ever relaxed.
    device = dr.async_get(hass).async_get(device_id)
    entry = (
        None
        if device is None
        else hass.config_entries.async_get_entry(device.config_entry_id)
    )
    if entry is None or entry.domain != DOMAIN:
        raise _invalid_target(device_id)
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="printer_not_loaded",
            translation_placeholders={"name": entry.title},
        )
    return entry.runtime_data


def _prepare_job(text: str) -> tuple[renderer.Raster, Sequence[bytes]]:
    """Render the label and encode the complete job; runs in an executor."""
    try:
        raster = renderer.render_label(text)
        segments = protocol.build_print_stream(raster)
    except renderer.EmptyTextError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="empty_text"
        ) from err
    except renderer.UnsupportedCharacterError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_character",
            translation_placeholders={
                "position": str(err.position),
                "codepoint": err.codepoint,
            },
        ) from err
    except renderer.TextDoesNotFitError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="text_does_not_fit",
            translation_placeholders={"lines": str(err.lines)},
        ) from err
    except Exception as err:
        # Any other fault is ours, not the caller's (spec §7 render_failed).
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="render_failed",
            translation_placeholders={"error": type(err).__name__},
        ) from err
    return raster, segments


def _invalid_target(device_id: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_target",
        translation_placeholders={"device_id": device_id},
    )
