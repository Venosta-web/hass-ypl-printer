"""Tests for the transmission state machine (spec §4.3, §6, §9).

Every test drives a scripted fake ``BleakClient`` (``tests/printer.py``) on
patched time; none touches Bluetooth or really sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import hashlib
import itertools
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakAbortedError,
    BleakConnectionError,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.bluetooth import BluetoothReachabilityIntent
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.ypl_printer import YplPrinterData, protocol, renderer, transport
from custom_components.ypl_printer.const import (
    DOMAIN,
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from custom_components.ypl_printer.exceptions import PrintOutcomeUncertainError
from custom_components.ypl_printer.protocol import PrinterStatus
from custom_components.ypl_printer.transport import (
    STATUS_REQUEST_FRAME,
    TransmitResult,
    split_chunks,
)

from .bluetooth import OTHER_SERVICE_UUID, PRINTER_ADDRESS, PRINTER_NAME, gatt_services
from .const import ACCEPTANCE_SPECIMEN
from .printer import (
    EVENT_FRAME,
    HANG,
    MODEL_REPLY,
    SILENT,
    Call,
    Delay,
    Drop,
    Fail,
    InstantClock,
    Notify,
    Reply,
    ScriptedPrinter,
    VirtualClock,
    status_reply,
)
from .test_renderer import GOLDEN_SPECIMEN_RASTER_SHA256

ESTABLISH_CONNECTION = "custom_components.ypl_printer.transport.establish_connection"
RESOLVE_DEVICE = "homeassistant.components.bluetooth.async_ble_device_from_address"
REACHABILITY = (
    "homeassistant.components.bluetooth.async_address_reachability_diagnostics"
)
LOGGER = "custom_components.ypl_printer"

# A small job: 130 bytes, so 7 chunks with a 10-byte tail.
SEGMENTS = (bytes(range(40)), bytes(range(40, 100)), bytes(range(100, 130)))
CHUNKS = split_chunks(SEGMENTS)
TOTAL = len(CHUNKS)

# End-to-end golden job for the acceptance specimen (spec §9). These are the
# bytes CI expects the hardware test to send.
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
GOLDEN_JOB_BYTES = 4124
GOLDEN_JOB_CHUNKS = 207
GOLDEN_JOB_SHA256 = "0512f28c15f61a2804eb887a89960638d39f15d96fb1e173e81540e117b08227"
GOLDEN_CHUNKS_SHA256 = (
    "0f04ee091b8adb4286ce207fd4b642e031fefa1ef6c0ac1421995cf5dcbb37d8"
)

READY = Reply(0x00)
PRINTING = Reply(0x01)


@pytest.fixture
def clock() -> Generator[VirtualClock]:
    """Run the state machine on virtual time."""
    virtual = VirtualClock()
    with patch.object(transport, "clock", virtual):
        yield virtual


@pytest.fixture
def printer(clock: VirtualClock) -> ScriptedPrinter:
    """The fake client the next job connects; answers ready by default."""
    return ScriptedPrinter(clock, TOTAL, polls=[READY, READY])


@pytest.fixture
def resolve() -> Generator[MagicMock]:
    """Resolve the printer to a connectable device."""
    with patch(
        RESOLVE_DEVICE, return_value=BLEDevice(PRINTER_ADDRESS, PRINTER_NAME, {})
    ) as fake:
        yield fake


@pytest.fixture
def connect(printer: ScriptedPrinter, resolve: MagicMock) -> Generator[MagicMock]:
    """Connect every job to ``printer`` (or to the clients in ``side_effect``)."""
    clients = [printer]

    async def _connect(
        client_class: type,
        device: BLEDevice,
        name: str,
        disconnected_callback: Any = None,
        **kwargs: Any,
    ) -> ScriptedPrinter:
        client = clients.pop(0) if len(clients) > 1 else clients[0]
        client.disconnected_callback = disconnected_callback
        client.log.append(("connect", device.address))
        return client

    with patch(ESTABLISH_CONNECTION, side_effect=_connect) as fake:
        fake.clients = clients
        yield fake


def _printer_data(
    address: str = PRINTER_ADDRESS, name: str = PRINTER_NAME
) -> YplPrinterData:
    return YplPrinterData(address=address, name=name)


async def _transmit(
    hass: HomeAssistant,
    data: YplPrinterData | None = None,
    segments: tuple[bytes, ...] = SEGMENTS,
) -> TransmitResult:
    return await transport.async_transmit(hass, data or _printer_data(), segments)


async def _fails(
    hass: HomeAssistant, key: str, data: YplPrinterData | None = None
) -> HomeAssistantError:
    """Run a job that must fail before the transmission boundary."""
    with pytest.raises(HomeAssistantError) as raised:
        await _transmit(hass, data)
    error = raised.value
    assert not isinstance(error, PrintOutcomeUncertainError)
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key
    assert error.translation_placeholders["name"] == PRINTER_NAME
    return error


async def _uncertain(hass: HomeAssistant, key: str) -> PrintOutcomeUncertainError:
    """Run a job that must end with an uncertain physical outcome."""
    with pytest.raises(PrintOutcomeUncertainError) as raised:
        await _transmit(hass)
    error = raised.value
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key
    assert set(error.translation_placeholders) == {
        "name",
        "sent",
        "total",
        "flags",
        "raw",
    }
    assert error.translation_placeholders["name"] == PRINTER_NAME
    assert error.translation_placeholders["total"] == str(TOTAL)
    return error


def _cleaned_up(printer: ScriptedPrinter) -> bool:
    return printer.log[-2:] == [("stop_notify",), ("disconnect",)]


def _job_log(printer: ScriptedPrinter) -> list[tuple[Any, ...]]:
    """The log without the connection and cleanup bookends."""
    return [
        entry
        for entry in printer.log
        if entry[0] not in {"connect", "start_notify", "stop_notify", "disconnect"}
    ]


# --- 1. happy path ---------------------------------------------------------------


async def test_happy_path(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Subscribe, poll, send every chunk with a 10 ms pause, observe, clean up."""
    result = await _transmit(hass)

    assert result == TransmitResult(ble_chunks=TOTAL, printer_status=PrinterStatus(0))
    assert printer.log == [
        ("connect", PRINTER_ADDRESS),
        ("start_notify",),
        ("poll",),
        *itertools.chain.from_iterable(
            [("chunk", index), ("sleep", 0.010)] for index in range(TOTAL)
        ),
        ("sleep", 1.0),
        ("poll",),
        ("stop_notify",),
        ("disconnect",),
    ]
    assert printer.stream == CHUNKS
    connect.assert_called_once()


def test_chunks_are_20_bytes_across_segment_boundaries() -> None:
    """The job is split into 20-byte chunks regardless of frame boundaries."""
    assert [len(chunk) for chunk in CHUNKS] == [20] * 6 + [10]
    assert b"".join(CHUNKS) == b"".join(SEGMENTS)
    assert split_chunks([b"a" * 7, b"b" * 7, b"c" * 26]) == [
        b"a" * 7 + b"b" * 7 + b"c" * 6,
        b"c" * 20,
    ]


def test_status_request_frame() -> None:
    """A status poll writes the spec's full 05/0b request-frame vector.

    Spec §6.4 calls it 13 bytes; the §3.7 vector it names is 14.
    """
    assert STATUS_REQUEST_FRAME == bytes.fromhex("1a010500050b0100002d54735ca1")


async def test_fresh_device_and_connection_per_job(
    hass: HomeAssistant, clock: VirtualClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """Each job resolves and connects its own client and disconnects it."""
    first = ScriptedPrinter(clock, TOTAL, polls=[READY, READY])
    second = ScriptedPrinter(clock, TOTAL, polls=[READY, READY])
    connect.clients[:] = [first, second]

    await _transmit(hass)
    await _transmit(hass)

    assert resolve.call_count == 2
    for call in resolve.call_args_list:
        assert call.args[1:] == (PRINTER_ADDRESS,)
        assert call.kwargs == {"connectable": True}
    assert connect.call_count == 2
    assert first.stream == second.stream == CHUNKS
    assert not first.is_connected
    assert not second.is_connected


async def test_connects_with_core_defaults(
    hass: HomeAssistant, connect: MagicMock
) -> None:
    """Only establish_connection's own attempts, with Core's defaults."""
    await _transmit(hass)

    (client_class, device, name), kwargs = connect.call_args
    assert client_class is transport.BleakClient
    assert device.address == PRINTER_ADDRESS
    assert name == PRINTER_NAME
    assert set(kwargs) == {"disconnected_callback"}


# --- 2. preflight ----------------------------------------------------------------


async def test_preflight_printing_then_ready(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """0x01 keeps polling every 300 ms; 0x00 starts transmission."""
    printer.polls = iter([PRINTING, PRINTING, READY, READY])

    result = await _transmit(hass)

    assert result.ble_chunks == TOTAL
    log = _job_log(printer)
    assert log[:6] == [
        ("poll",),
        ("sleep", pytest.approx(0.28)),
        ("poll",),
        ("sleep", pytest.approx(0.28)),
        ("poll",),
        ("chunk", 0),
    ]


async def test_preflight_busy_after_15_s(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """Still 0x01 after 15 s is printer_busy with nothing sent."""
    printer.polls = itertools.repeat(PRINTING)

    await _fails(hass, "printer_busy")

    assert printer.stream == []
    assert 15.0 <= clock.now < 15.3
    assert printer.poll_count == 51
    assert _cleaned_up(printer)


@pytest.mark.parametrize(
    ("raw", "flags", "raw_text"),
    [
        (0x02, "cover_open", "0x02"),
        (0x04, "paper_out", "0x04"),
        (0x06, "cover_open, paper_out", "0x06"),
        (0x08, "undervoltage", "0x08"),
        (0x10, "overheat", "0x10"),
        (0x03, "printing, cover_open", "0x03"),
        (0x20, "unknown", "0x20"),
        (0x81, "printing, unknown", "0x81"),
    ],
)
async def test_preflight_not_ready(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    raw: int,
    flags: str,
    raw_text: str,
) -> None:
    """Any other valid status, unknown bits included, stops with zero writes."""
    printer.polls = iter([Reply(raw)])

    error = await _fails(hass, "printer_not_ready")

    assert error.translation_placeholders == {
        "name": PRINTER_NAME,
        "flags": flags,
        "raw": raw_text,
    }
    assert printer.stream == []
    assert printer.poll_count == 1
    assert _cleaned_up(printer)


async def test_preflight_silence_transmits_with_warning(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three consecutive silent polls: WARNING, then transmit."""
    printer.polls = iter([SILENT, SILENT, SILENT, READY])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))
    log = _job_log(printer)
    assert [
        entry for entry in log[: log.index(("chunk", 0))] if entry[0] == "poll"
    ] == [("poll",)] * 3
    assert "printing with the printer status unknown" in caplog.text
    assert [r.levelname for r in caplog.records if "status unknown" in r.message] == [
        "WARNING"
    ]


async def test_preflight_satisfied_poll_resets_silence(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Silence is counted only for consecutive polls."""
    printer.polls = iter(
        [SILENT, SILENT, PRINTING, SILENT, SILENT, PRINTING, READY, READY]
    )

    await _transmit(hass)

    log = _job_log(printer)
    assert log[: log.index(("chunk", 0))].count(("poll",)) == 7


async def test_preflight_poll_write_timeout(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """A poll write that does not return within 5 s is communication_failed."""
    printer.polls = iter([HANG])

    error = await _fails(hass, "communication_failed")

    assert error.translation_placeholders["error"] == "TimeoutError"
    assert isinstance(error.__cause__, TimeoutError)
    assert clock.now == pytest.approx(5.0)
    assert printer.stream == []
    assert _cleaned_up(printer)


async def test_preflight_poll_write_error(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A poll write that raises is communication_failed, never retried."""
    cause = BleakError("write failed")
    printer.polls = iter([SILENT, Fail(cause)])

    error = await _fails(hass, "communication_failed")

    assert error.translation_placeholders["error"] == "BleakError"
    assert error.__cause__ is cause
    assert printer.poll_count == 2
    assert printer.stream == []
    connect.assert_called_once()


# --- 3. correlation and notification decoding ------------------------------------


async def test_late_reply_does_not_satisfy_a_later_poll(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reply decoded before a poll's write returned never answers that poll."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    printer.polls = iter(
        [
            SILENT,
            # The first poll's error reply arrives while the second is written.
            [Reply(0x02, delay=0.05), Delay(0.1)],
            READY,
            READY,
        ]
    )

    result = await _transmit(hass)

    assert result.ble_chunks == TOTAL
    assert "uncorrelated status reply" in caplog.text


async def test_late_reply_after_send_does_not_satisfy_observation(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A ready reply before the first post-send poll does not complete the job."""
    printer.polls = itertools.chain([READY], itertools.repeat(PRINTING))
    printer.chunks = {TOTAL - 1: Reply(0x00, delay=0.5)}

    await _uncertain(hass, "outcome_uncertain_still_printing")


async def test_fragmented_reply(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A reply split across notifications is decoded once complete."""
    reply = status_reply(0x00)
    printer.polls = iter([Notify((reply[:3], reply[3:7], reply[7:])), READY])

    result = await _transmit(hass)

    assert result.ble_chunks == TOTAL
    assert _job_log(printer)[1] == ("chunk", 0)


async def test_coalesced_frames(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Frames coalesced into one notification are each decoded."""
    printer.polls = iter(
        [Notify((EVENT_FRAME + MODEL_REPLY + status_reply(0x00),)), READY]
    )

    result = await _transmit(hass)

    assert result.ble_chunks == TOTAL
    assert _job_log(printer)[1] == ("chunk", 0)


@pytest.mark.parametrize(
    "garbage",
    [
        pytest.param(b"\x00\xff\x13", id="no-frame-start"),
        pytest.param(b"\x1a\x01\x02\x00\xff", id="short-length"),
        pytest.param(status_reply(0x02)[:-1] + b"\x00", id="bad-terminator"),
        pytest.param(
            bytes.fromhex("1a010900050b0204000301000000000000a1"), id="bad-crc"
        ),
        pytest.param(
            protocol.encode_frame(bytes.fromhex("050b0203000100aa")),
            id="bytes-envelope",
        ),
    ],
)
async def test_malformed_candidates_skipped_and_logged(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
    garbage: bytes,
) -> None:
    """Malformed bytes never count as a status and are logged at DEBUG."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    printer.polls = iter([Notify((garbage, status_reply(0x00))), READY])

    result = await _transmit(hass)

    assert result.printer_status == PrinterStatus(0)
    assert "skipped malformed" in caplog.text
    assert _job_log(printer)[1] == ("chunk", 0)


async def test_events_and_other_replies_do_not_affect_the_outcome(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """05/0f events and non-status replies are logged raw and ignored."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    printer.chunks = {1: Notify((EVENT_FRAME, MODEL_REPLY))}
    printer.polls = iter([READY, Notify((EVENT_FRAME,)), READY])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))
    assert EVENT_FRAME.hex() in caplog.text
    assert MODEL_REPLY.hex() in caplog.text


# --- 4. error status during transmission -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "flags"),
    [(0x02, "cover_open"), (0x40, "unknown"), (0x05, "printing, paper_out")],
)
async def test_error_during_transmission_sends_everything(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    raw: int,
    flags: str,
) -> None:
    """An error status does not stop the stream; the job is then uncertain."""
    printer.chunks = {2: Reply(raw)}

    error = await _uncertain(hass, "outcome_uncertain_printer_error")

    assert printer.stream == CHUNKS
    assert error.translation_placeholders["sent"] == str(TOTAL)
    assert error.translation_placeholders["flags"] == flags
    assert error.translation_placeholders["raw"] == f"0x{raw:02x}"
    assert _cleaned_up(printer)


async def test_error_during_transmission_wins_over_ready(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A later ready status does not clear an error seen after the boundary."""
    printer.chunks = {2: Reply(0x02), 4: Reply(0x00)}

    error = await _uncertain(hass, "outcome_uncertain_printer_error")

    assert error.translation_placeholders["raw"] == "0x00"


# --- 5. post-send observation ----------------------------------------------------


async def test_post_send_waits_1_s_then_polls(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """Observation waits 1 s, then polls every 300 ms until ready."""
    printer.polls = iter([READY, PRINTING, PRINTING, READY])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))
    log = _job_log(printer)
    assert log[log.index(("chunk", TOTAL - 1)) + 2 :] == [
        ("sleep", 1.0),
        ("poll",),
        ("sleep", pytest.approx(0.28)),
        ("poll",),
        ("sleep", pytest.approx(0.28)),
        ("poll",),
    ]


async def test_post_send_still_printing_at_deadline(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """Still 0x01 at the 15 s deadline is still_printing."""
    printer.polls = itertools.chain([READY], itertools.repeat(PRINTING))

    error = await _uncertain(hass, "outcome_uncertain_still_printing")

    observation_started = 0.02 + TOTAL * 0.010 + 1.0
    assert 15.0 <= clock.now - observation_started < 15.3
    assert error.translation_placeholders == {
        "name": PRINTER_NAME,
        "sent": str(TOTAL),
        "total": str(TOTAL),
        "flags": "printing",
        "raw": "0x01",
    }


async def test_post_send_printing_then_silence(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """0x01 followed by three silent polls is still_printing."""
    printer.polls = iter([READY, PRINTING, SILENT, SILENT, SILENT])

    await _uncertain(hass, "outcome_uncertain_still_printing")

    assert printer.poll_count == 5


@pytest.mark.parametrize("raw", [0x02, 0x04, 0x06, 0x10, 0x80])
async def test_post_send_error_status(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock, raw: int
) -> None:
    """An error status or unknown bit while observing is printer_error."""
    printer.polls = iter([READY, PRINTING, Reply(raw), READY])

    error = await _uncertain(hass, "outcome_uncertain_printer_error")

    assert error.translation_placeholders["raw"] == f"0x{raw:02x}"
    assert printer.poll_count == 3


async def test_post_send_silence_without_any_status(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No status in the whole job: completed with a null status and a WARNING."""
    printer.polls = iter([])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, None)
    assert printer.poll_count == 6
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "no status reply after sending" in warnings[1]


async def test_post_send_satisfied_poll_resets_silence(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """While observing, silence is also counted only for consecutive polls."""
    printer.polls = iter([READY, SILENT, SILENT, PRINTING, SILENT, READY])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))
    assert printer.poll_count == 6


async def test_post_send_silence_after_earlier_ready(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """0x00 seen earlier in the job, then silence: completed with that status."""
    printer.polls = iter([READY, SILENT, SILENT, SILENT])

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))


async def test_post_send_silence_after_embedded_ready(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Replies to the stream's own polls count as the job's last status."""
    printer.polls = iter([SILENT, SILENT, SILENT, SILENT, SILENT, SILENT])
    printer.chunks = {TOTAL - 1: Reply(0x00)}

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))


async def test_post_send_poll_write_fails(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A failing poll write after sending is a transport uncertainty."""
    cause = BleakError("gone")
    printer.polls = iter([READY, PRINTING, Fail(cause)])

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert error.__cause__ is cause
    assert error.translation_placeholders["flags"] == "printing"
    connect.assert_called_once()


async def test_post_send_poll_write_timeout(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A poll write that times out after sending is a transport uncertainty."""
    printer.polls = iter([READY, HANG])

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert isinstance(error.__cause__, TimeoutError)


async def test_post_send_link_drop(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A disconnect while observing is a transport uncertainty; no reconnect."""
    printer.polls = iter([READY, PRINTING, [PRINTING, Drop(0.2)], READY])

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert error.translation_placeholders["sent"] == str(TOTAL)
    connect.assert_called_once()
    assert printer.log[-1] != ("disconnect",)


async def test_post_send_link_drop_during_wait(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A disconnect during the 1 s wait ends observation without polling."""
    printer.chunks = {TOTAL - 1: Drop(0.5)}

    await _uncertain(hass, "outcome_uncertain_transport")

    assert printer.poll_count == 1


async def test_printer_error_is_evaluated_before_transport(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Rule 1 before rule 2: an error status outranks a dropped link."""
    printer.chunks = {TOTAL - 1: [Reply(0x04), Drop(0.5)]}

    await _uncertain(hass, "outcome_uncertain_printer_error")


async def test_printer_error_outranks_a_failed_poll_write(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Rule 1 before rule 2 also when the observation poll write fails."""
    printer.polls = iter(
        [READY, Reply(0x02, delay=0.6), [Delay(0.2), Fail(BleakError("x"))]]
    )

    await _uncertain(hass, "outcome_uncertain_printer_error")


# --- 6/7. write failures after the boundary --------------------------------------


async def test_first_stream_write_raises(
    hass: HomeAssistant,
    resolve: MagicMock,
    printer: ScriptedPrinter,
    connect: MagicMock,
) -> None:
    """The boundary is the first write started: its failure is uncertain."""
    cause = BleakError("write failed")
    printer.chunks = {0: Fail(cause)}

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert error.__cause__ is cause
    assert error.translation_placeholders["sent"] == "0"
    assert error.translation_placeholders["flags"] == "ready"
    assert error.translation_placeholders["raw"] == "0x00"
    assert printer.stream == CHUNKS[:1]
    assert printer.poll_count == 1
    resolve.assert_called_once()
    connect.assert_called_once()
    assert _cleaned_up(printer)


async def test_first_stream_write_raises_without_status(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """Without any status, the uncertain placeholders say unknown."""
    printer.polls = iter([])
    printer.chunks = {0: Fail(OSError("boom"))}

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert error.translation_placeholders["flags"] == "unknown"
    assert error.translation_placeholders["raw"] == "unknown"


@pytest.mark.parametrize("index", [0, 3, TOTAL - 1])
async def test_stream_write_timeout(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
    index: int,
) -> None:
    """A write that does not return within 5 s is uncertain, never resent."""
    printer.chunks = {index: HANG}

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert isinstance(error.__cause__, TimeoutError)
    assert error.translation_placeholders["sent"] == str(index)
    assert printer.stream == CHUNKS[: index + 1]
    assert clock.now == pytest.approx(0.02 + index * 0.010 + 5.0)
    connect.assert_called_once()
    assert _cleaned_up(printer)


async def test_disconnect_during_transmission(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A dropped link ends transmission early as a transport uncertainty."""
    printer.chunks = {2: Drop()}

    error = await _uncertain(hass, "outcome_uncertain_transport")

    assert error.translation_placeholders["sent"] == "3"
    assert printer.stream == CHUNKS[:3]
    connect.assert_called_once()
    # Cleanup does not touch a client that is no longer connected.
    assert printer.log[-1] == ("sleep", 0.010)


async def test_uncertain_outcome_is_logged_as_warning(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The WARNING names the phase, the counts, the last status and the cause."""
    printer.chunks = {3: Fail(BleakError("secret detail"))}

    await _uncertain(hass, "outcome_uncertain_transport")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "transmitting" in message
    assert f"sent 3 of {TOTAL} chunks" in message
    assert "ready (0x00)" in message
    assert "BleakError" in message
    # Exception text is DEBUG only.
    assert "secret detail" not in message


# --- 8. lock ---------------------------------------------------------------------


async def test_lock_timeout(
    hass: HomeAssistant, clock: VirtualClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """Waiting more than 120 s for the printer's lock is printer_busy."""
    data = _printer_data()
    await data.lock.acquire()

    error = await _fails(hass, "printer_busy", data)

    assert isinstance(error.__cause__, TimeoutError)
    assert clock.now == pytest.approx(120.0)
    resolve.assert_not_called()
    connect.assert_not_called()
    assert data.lock.locked()


async def test_lock_released_after_every_outcome(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """The lock is free again after a failed job."""
    data = _printer_data()
    printer.polls = iter([Reply(0x02)])

    await _fails(hass, "printer_not_ready", data)

    assert not data.lock.locked()


@pytest.fixture
def instant(connect: MagicMock) -> Generator[InstantClock]:
    """Real scheduling with sleeps reduced to yields.

    Depends on ``connect`` so that it replaces the virtual clock.
    """
    fast = InstantClock()
    with patch.object(transport, "clock", fast):
        yield fast


async def test_same_printer_jobs_are_serialised(
    hass: HomeAssistant, instant: InstantClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """A second job on the same printer starts only after the first disconnects."""
    release = asyncio.Event()
    first = ScriptedPrinter(instant, TOTAL, polls=[READY, READY])
    second = ScriptedPrinter(instant, TOTAL, polls=[READY, READY])
    connect.clients[:] = [first, second]
    blocked = asyncio.Event()
    original = first.write_gatt_char

    async def _write(uuid: str, data: bytes, response: bool = False) -> None:
        await original(uuid, data, response)
        if len(first.stream) == 3 and not release.is_set():
            blocked.set()
            await release.wait()

    first.write_gatt_char = _write  # type: ignore[method-assign]
    data = _printer_data()

    job_one = hass.async_create_task(_transmit(hass, data))
    await blocked.wait()
    job_two = hass.async_create_task(_transmit(hass, data))
    for _ in range(50):
        await asyncio.sleep(0)
    assert connect.call_count == 1
    assert resolve.call_count == 1

    release.set()
    await asyncio.gather(job_one, job_two)

    log = instant.log
    assert log.index(("disconnect",)) < log.index(("connect", PRINTER_ADDRESS), 1)
    assert first.stream == second.stream == CHUNKS


async def test_different_printers_do_not_wait_for_each_other(
    hass: HomeAssistant, instant: InstantClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """A stalled job on one printer does not block another printer."""
    stalled = ScriptedPrinter(instant, TOTAL)
    other = ScriptedPrinter(instant, TOTAL, polls=[READY, READY])
    connect.clients[:] = [stalled, other]
    release = asyncio.Event()
    original = stalled.write_gatt_char

    async def _write(uuid: str, data: bytes, response: bool = False) -> None:
        await original(uuid, data, response)
        await release.wait()

    stalled.write_gatt_char = _write  # type: ignore[method-assign]
    first = _printer_data()
    second = _printer_data("11:22:33:44:55:66", "Y50-5678")

    job_one = hass.async_create_task(_transmit(hass, first))
    for _ in range(10):
        await asyncio.sleep(0)
    assert first.lock.locked()

    result = await _transmit(hass, second)

    assert result.ble_chunks == TOTAL
    assert not job_one.done()
    job_one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job_one
    assert not first.lock.locked()


# --- 9. cancellation -------------------------------------------------------------


async def test_cancel_before_the_boundary(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation in preflight cleans up and re-raises, without a WARNING."""
    task = hass.async_create_task(_transmit(hass))
    printer.polls = iter([Call(task.cancel)])

    with pytest.raises(asyncio.CancelledError):
        await task

    assert printer.stream == []
    assert _cleaned_up(printer)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("index", [0, 4])
async def test_cancel_after_the_boundary(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
    index: int,
) -> None:
    """Cancellation after the boundary cleans up, warns and re-raises."""
    task = hass.async_create_task(_transmit(hass))
    printer.chunks = {index: [Call(task.cancel), HANG]}

    with pytest.raises(asyncio.CancelledError):
        await task

    assert printer.stream == CHUNKS[: index + 1]
    assert _cleaned_up(printer)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "outcome is uncertain" in warnings[0]
    assert f"sent {index} of {TOTAL} chunks" in warnings[0]


async def test_cancel_while_observing(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation during post-send observation is still after the boundary."""
    task = hass.async_create_task(_transmit(hass))
    printer.polls = iter([READY, Call(task.cancel)])

    with pytest.raises(asyncio.CancelledError):
        await task

    assert _cleaned_up(printer)
    assert "post_send_observation" in caplog.text


async def test_cancel_during_lock_wait(
    hass: HomeAssistant, resolve: MagicMock, connect: MagicMock
) -> None:
    """A job cancelled while waiting for the lock never connects."""
    data = _printer_data()
    await data.lock.acquire()
    task = hass.async_create_task(_transmit(hass, data))
    for _ in range(5):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    resolve.assert_not_called()
    connect.assert_not_called()
    data.lock.release()
    assert not data.lock.locked()


# --- 10. cleanup failures --------------------------------------------------------


CLEANUP_FAILURES = [
    pytest.param("stop_notify_step", Fail(BleakError("x")), 0.0, id="stop-raises"),
    pytest.param("stop_notify_step", HANG, 2.0, id="stop-times-out"),
    pytest.param("disconnect_step", Fail(EOFError()), 0.0, id="disconnect-raises"),
    pytest.param("disconnect_step", HANG, 10.0, id="disconnect-times-out"),
]


@pytest.mark.parametrize(("step", "action", "waited"), CLEANUP_FAILURES)
async def test_cleanup_failure_keeps_the_original_error(
    hass: HomeAssistant,
    clock: VirtualClock,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
    step: str,
    action: Any,
    waited: float,
) -> None:
    """A failing cleanup is a WARNING and never replaces the job's error."""
    setattr(printer, step, action)
    printer.polls = iter([Reply(0x06)])

    await _fails(hass, "printer_not_ready")

    assert printer.log[-2:] == [("stop_notify",), ("disconnect",)]
    assert clock.now == pytest.approx(0.02 + waited)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "failed" in warnings[0]


@pytest.mark.parametrize(("step", "action", "waited"), CLEANUP_FAILURES)
async def test_cleanup_failure_keeps_the_uncertain_outcome(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    step: str,
    action: Any,
    waited: float,
) -> None:
    """A failing cleanup never replaces an uncertain outcome."""
    setattr(printer, step, action)
    printer.chunks = {1: Fail(BleakError("x"))}

    await _uncertain(hass, "outcome_uncertain_transport")


@pytest.mark.parametrize(("step", "action", "waited"), CLEANUP_FAILURES)
async def test_cleanup_failure_after_success(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    step: str,
    action: Any,
    waited: float,
) -> None:
    """A failing cleanup does not turn a completed job into a failure."""
    setattr(printer, step, action)

    result = await _transmit(hass)

    assert result == TransmitResult(TOTAL, PrinterStatus(0))


@pytest.mark.parametrize(("step", "action", "waited"), CLEANUP_FAILURES)
async def test_cleanup_failure_keeps_the_cancellation(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    step: str,
    action: Any,
    waited: float,
) -> None:
    """A failing cleanup never replaces a cancellation."""
    setattr(printer, step, action)
    task = hass.async_create_task(_transmit(hass))
    printer.chunks = {2: [Call(task.cancel), HANG]}

    with pytest.raises(asyncio.CancelledError):
        await task

    assert printer.log[-2:] == [("stop_notify",), ("disconnect",)]


# --- 11. connector and profile failures ------------------------------------------


async def test_printer_not_found(
    hass: HomeAssistant, connect: MagicMock, resolve: MagicMock
) -> None:
    """No connectable device is printer_not_found with HA's diagnostics."""
    resolve.return_value = None
    with patch(REACHABILITY, return_value="No adapter sees it.") as diagnostics:
        error = await _fails(hass, "printer_not_found")

    assert error.translation_placeholders == {
        "name": PRINTER_NAME,
        "reachability": "No adapter sees it.",
    }
    diagnostics.assert_called_once_with(
        hass, PRINTER_ADDRESS, BluetoothReachabilityIntent.CONNECTION
    )
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("error", "key", "placeholders"),
    [
        (BleakOutOfConnectionSlotsError("slots"), "no_connection_slots", {}),
        (
            BleakNotFoundError("gone"),
            "connection_failed",
            {"error": "BleakNotFoundError"},
        ),
        (
            BleakAbortedError("aborted"),
            "connection_failed",
            {"error": "BleakAbortedError"},
        ),
        (
            BleakConnectionError("failed"),
            "connection_failed",
            {"error": "BleakConnectionError"},
        ),
        (BleakError("other"), "connection_failed", {"error": "BleakError"}),
        (TimeoutError(), "connection_failed", {"error": "TimeoutError"}),
    ],
)
async def test_connector_errors(
    hass: HomeAssistant,
    resolve: MagicMock,
    error: Exception,
    key: str,
    placeholders: dict[str, str],
) -> None:
    """Each connector exception maps to its key, raised from the cause."""
    with patch(ESTABLISH_CONNECTION, side_effect=error) as connect:
        raised = await _fails(hass, key)

    assert raised.translation_placeholders == {"name": PRINTER_NAME, **placeholders}
    assert raised.__cause__ is error
    connect.assert_called_once()


@pytest.mark.parametrize(
    "services",
    [
        gatt_services((OTHER_SERVICE_UUID, ())),
        gatt_services((SERVICE_UUID, ())),
        gatt_services((SERVICE_UUID, (WRITE_CHARACTERISTIC_UUID,))),
        gatt_services((SERVICE_UUID, (NOTIFY_CHARACTERISTIC_UUID,))),
    ],
    ids=["no-service", "no-characteristics", "no-notify", "no-write"],
)
async def test_unsupported_profile(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock, services: Any
) -> None:
    """A missing profile at job time is unsupported_profile, nothing written."""
    printer.services = services

    await _fails(hass, "unsupported_profile")

    assert printer.log == [("connect", PRINTER_ADDRESS), ("disconnect",)]


async def test_start_notify_fails(
    hass: HomeAssistant, printer: ScriptedPrinter, connect: MagicMock
) -> None:
    """A failed subscription is communication_failed, before any write."""
    cause = BleakError("notify")
    printer.start_notify_error = cause

    error = await _fails(hass, "communication_failed")

    assert error.translation_placeholders["error"] == "BleakError"
    assert error.__cause__ is cause
    assert printer.log == [
        ("connect", PRINTER_ADDRESS),
        ("start_notify",),
        ("disconnect",),
    ]


# --- logging ---------------------------------------------------------------------


async def test_logging_on_success(
    hass: HomeAssistant,
    printer: ScriptedPrinter,
    connect: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEBUG only on success, every line naming the printer, no chunk bytes."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    await _transmit(hass)

    records = [r for r in caplog.records if r.name.startswith(LOGGER)]
    assert records
    assert all(r.levelno == logging.DEBUG for r in records)
    for record in records:
        message = record.getMessage()
        assert f"{PRINTER_NAME} ({PRINTER_ADDRESS}): " in message
        assert not any(chunk.hex() in message for chunk in CHUNKS)
    text = "\n".join(r.getMessage() for r in records)
    for phase in (
        "resolving",
        "connecting",
        "subscribing",
        "preflight",
        "transmitting",
        "post_send_observation",
        "cleanup",
    ):
        assert f"-> {phase} after" in text
    assert f"notification {status_reply(0).hex()}" in text
    assert "status poll result: 0x00" in text
    assert f"sent 130 of 130 bytes and {TOTAL} of {TOTAL} chunks" in text


# --- end-to-end golden -----------------------------------------------------------


def _golden_chunks() -> list[bytes]:
    data = (GOLDEN_DIR / "specimen-chunks.hex").read_bytes()
    assert hashlib.sha256(data).hexdigest() == GOLDEN_CHUNKS_SHA256
    return [bytes.fromhex(line) for line in data.decode().splitlines()]


def test_golden_specimen_job() -> None:
    """Specimen -> raster -> job -> exact 20-byte chunks, all pinned."""
    segments = protocol.build_print_stream(renderer.render_label(ACCEPTANCE_SPECIMEN))
    job = b"".join(segments)
    chunks = split_chunks(segments)

    assert len(job) == GOLDEN_JOB_BYTES
    assert len(chunks) == GOLDEN_JOB_CHUNKS
    assert hashlib.sha256(job).hexdigest() == GOLDEN_JOB_SHA256
    assert chunks == _golden_chunks()


async def _loaded_printer(hass: HomeAssistant) -> str:
    """Set up one printer entry and return its device ID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=PRINTER_NAME,
        unique_id=dr.format_mac(PRINTER_ADDRESS),
        data={CONF_ADDRESS: PRINTER_ADDRESS},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return device.id


@pytest.mark.usefixtures("enable_bluetooth")
async def test_golden_specimen_through_the_print_action(
    hass: HomeAssistant, clock: VirtualClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """The print action sends exactly the golden chunks and reports them."""
    golden = _golden_chunks()
    printer = ScriptedPrinter(clock, len(golden), polls=[READY, READY])
    connect.clients[:] = [printer]
    device_id = await _loaded_printer(hass)

    response = await hass.services.async_call(
        DOMAIN,
        "print",
        {"device_id": device_id, "text": ACCEPTANCE_SPECIMEN},
        blocking=True,
        return_response=True,
    )

    assert printer.stream == golden
    assert printer.poll_count == 2
    assert response == {
        "device_id": device_id,
        "raster_width": 400,
        "raster_height": 240,
        "raster_sha256": GOLDEN_SPECIMEN_RASTER_SHA256,
        "encoded_bytes": GOLDEN_JOB_BYTES,
        "ble_chunks": GOLDEN_JOB_CHUNKS,
        "printer_status": {"raw": 0, "flags": ["ready"], "unknown_bits": 0},
    }


@pytest.mark.usefixtures("enable_bluetooth")
async def test_uncertain_outcome_through_the_print_action_is_not_replayed(
    hass: HomeAssistant, clock: VirtualClock, resolve: MagicMock, connect: MagicMock
) -> None:
    """After the boundary the print action raises once and never reconnects."""
    golden = _golden_chunks()
    printer = ScriptedPrinter(
        clock, len(golden), polls=[READY], chunks={100: Fail(BleakError("x"))}
    )
    connect.clients[:] = [printer]
    device_id = await _loaded_printer(hass)

    with pytest.raises(PrintOutcomeUncertainError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "print",
            {"device_id": device_id, "text": ACCEPTANCE_SPECIMEN},
            blocking=True,
        )

    assert str(raised.value).startswith(
        f"The connection to the printer {PRINTER_NAME} failed while sending"
    )
    assert f"Chunks sent: 100 of {GOLDEN_JOB_CHUNKS}" in str(raised.value)
    assert printer.stream == golden[:101]
    resolve.assert_called_once()
    connect.assert_called_once()
