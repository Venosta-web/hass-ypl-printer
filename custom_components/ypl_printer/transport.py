"""BLE transport seam for print jobs (spec §4.3, §6).

The print action hands a fully rendered and encoded job to
``async_transmit`` and nothing else touches Bluetooth. Tests replace this
function with a fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .protocol import PrinterStatus

if TYPE_CHECKING:
    from . import YplPrinterData


@dataclass(frozen=True, slots=True)
class TransmitResult:
    """Facts observed by a job that completed without an observed failure."""

    # Chunks whose write call returned.
    ble_chunks: int
    # The last strictly validated status of this job, if any.
    printer_status: PrinterStatus | None


async def async_transmit(
    hass: HomeAssistant, printer: YplPrinterData, segments: Sequence[bytes]
) -> TransmitResult:
    """Send one ordered job to the printer, holding its lock throughout.

    Raises a translated ``HomeAssistantError`` when the job failed before the
    transmission boundary and ``PrintOutcomeUncertainError`` after it. Never
    retries or replays.
    """
    # The transmission state machine is issue #18.
    raise NotImplementedError("the BLE transport is not implemented yet")
