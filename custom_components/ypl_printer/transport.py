"""BLE transport for print jobs: the transmission state machine (spec §4.3, §6).

The print action hands a fully rendered and encoded job to
``async_transmit`` and nothing else touches Bluetooth. One call is one
forward-only attempt: it never retries, reconnects or resends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakOutOfConnectionSlotsError, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothReachabilityIntent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import protocol
from .ble import has_ypl_profile
from .const import DOMAIN, NOTIFY_CHARACTERISTIC_UUID, WRITE_CHARACTERISTIC_UUID
from .exceptions import PrintOutcomeUncertainError
from .protocol import PrinterStatus

if TYPE_CHECKING:
    from . import YplPrinterData

_LOGGER = logging.getLogger(__name__)

# Timing (spec §6). These change only through the evidence loop (spec §12).
LOCK_TIMEOUT = 120.0
WRITE_TIMEOUT = 5.0
REPLY_TIMEOUT = 0.5
POLL_INTERVAL = 0.3
PREFLIGHT_BUSY_LIMIT = 15.0
SILENT_POLL_LIMIT = 3
CHUNK_SIZE = 20
CHUNK_PAUSE = 0.010
POST_SEND_WAIT = 1.0
POST_SEND_LIMIT = 15.0
STOP_NOTIFY_TIMEOUT = 2.0
DISCONNECT_TIMEOUT = 10.0

STATUS_REQUEST_FRAME = protocol.encode_frame(protocol.STATUS_REQUEST_PAYLOAD)
STATUS_PRINTING = 0x01


class Clock:
    """Time as the state machine sees it; tests replace ``clock``."""

    def monotonic(self) -> float:
        """Return the current monotonic time in seconds."""
        return asyncio.get_running_loop().time()

    async def sleep(self, delay: float) -> None:
        """Sleep for ``delay`` seconds."""
        await asyncio.sleep(delay)

    async def wait_for[T](self, awaitable: Awaitable[T], timeout: float) -> T:
        """Await ``awaitable``, raising ``TimeoutError`` after ``timeout``."""
        return await asyncio.wait_for(awaitable, timeout)


clock = Clock()


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
    return await _PrintJob(hass, printer, segments).run()


def split_chunks(segments: Sequence[bytes]) -> list[bytes]:
    """Concatenate the job and split it into 20-byte chunks.

    Chunks ignore the reported MTU and frame boundaries (spec §6.6).
    """
    data = b"".join(segments)
    return [data[i : i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]


def format_flags(status: PrinterStatus) -> str:
    """Return a status's flag names for an error message."""
    names = list(status.flags)
    if status.unknown_bits:
        names.append("unknown")
    return ", ".join(names)


def format_raw(status: PrinterStatus) -> str:
    """Return a raw status value as it appears in messages, e.g. ``0x06``."""
    return f"0x{status.raw:02x}"


class _PollWriteError(Exception):
    """A status-poll write raised or timed out; the cause is chained."""


class _PrintJob:
    """One lock-held, forward-only print attempt (spec §6)."""

    def __init__(
        self, hass: HomeAssistant, printer: YplPrinterData, segments: Sequence[bytes]
    ) -> None:
        self._hass = hass
        self._printer = printer
        self._chunks = split_chunks(segments)
        self._client: BleakClient | None = None
        self._subscribed = False
        self._decoder = protocol.FrameDecoder()
        self._phase = "waiting_for_lock"
        self._started = clock.monotonic()
        self._phase_started = self._started
        # Set once the first print-stream write is started (spec §6.3).
        self._boundary_crossed = False
        self._sent = 0
        self._sent_bytes = 0
        self._disconnected = False
        self._last_status: PrinterStatus | None = None
        self._error_after_boundary: PrinterStatus | None = None
        # Correlation of one status poll with its reply (spec §6.4).
        self._poll_open = False
        self._poll_reply: PrinterStatus | None = None
        self._wake = asyncio.Event()

    # --- logging (spec §6.10) -------------------------------------------------

    def _log(self, level: int, msg: str, *args: Any) -> None:
        _LOGGER.log(
            level,
            "%s (%s): " + msg,
            self._printer.name,
            self._printer.address,
            *args,
        )

    def _debug(self, msg: str, *args: Any) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            self._log(logging.DEBUG, msg, *args)

    def _warning(self, msg: str, *args: Any) -> None:
        self._log(logging.WARNING, msg, *args)

    def _enter(self, phase: str) -> None:
        now = clock.monotonic()
        self._debug(
            "phase %s -> %s after %.3f s", self._phase, phase, now - self._phase_started
        )
        self._phase = phase
        self._phase_started = now

    def _status_text(self) -> str:
        status = self._last_status
        if status is None:
            return "unknown"
        return f"{format_flags(status)} ({format_raw(status)})"

    # --- errors (spec §7) -----------------------------------------------------

    def _error(
        self, key: str, cause: BaseException | None = None, **placeholders: str
    ) -> HomeAssistantError:
        """Return a failed-before-transmission error."""
        if cause is not None:
            self._debug("%s: %r", key, cause)
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"name": self._printer.name, **placeholders},
        )

    def _uncertain(
        self, key: str, cause: BaseException | str
    ) -> PrintOutcomeUncertainError:
        """Log and return an uncertain-physical-outcome error."""
        if isinstance(cause, BaseException):
            self._debug("%s: %r", key, cause)
            cause = type(cause).__name__
        status = self._last_status
        self._warning(
            "print outcome is uncertain (%s) in phase %s: sent %d of %d chunks, "
            "last status %s, cause %s",
            key,
            self._phase,
            self._sent,
            len(self._chunks),
            self._status_text(),
            cause,
        )
        return PrintOutcomeUncertainError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={
                "name": self._printer.name,
                "sent": str(self._sent),
                "total": str(len(self._chunks)),
                "flags": "unknown" if status is None else format_flags(status),
                "raw": "unknown" if status is None else format_raw(status),
            },
        )

    # --- lifecycle (spec §4.3, §6.2, §6.9) ------------------------------------

    async def run(self) -> TransmitResult:
        lock = self._printer.lock
        try:
            await clock.wait_for(lock.acquire(), LOCK_TIMEOUT)
        except TimeoutError as err:
            raise self._error("printer_busy", err) from err
        try:
            return await self._run_locked()
        finally:
            lock.release()
            self._debug(
                "job finished: sent %d of %d bytes and %d of %d chunks in %.3f s, "
                "last status %s",
                self._sent_bytes,
                sum(len(chunk) for chunk in self._chunks),
                self._sent,
                len(self._chunks),
                clock.monotonic() - self._started,
                self._status_text(),
            )

    async def _run_locked(self) -> TransmitResult:
        self._enter("resolving")
        address = self._printer.address
        device = bluetooth.async_ble_device_from_address(
            self._hass, address, connectable=True
        )
        if device is None:
            reachability = bluetooth.async_address_reachability_diagnostics(
                self._hass, address, BluetoothReachabilityIntent.CONNECTION
            )
            raise self._error("printer_not_found", reachability=reachability)

        self._enter("connecting")
        try:
            # Core's default attempts are the only retries (spec §6.3).
            client = await establish_connection(
                BleakClient,
                device,
                self._printer.name,
                disconnected_callback=self._on_disconnect,
            )
        except BleakOutOfConnectionSlotsError as err:
            raise self._error("no_connection_slots", err) from err
        except (BleakError, TimeoutError) as err:
            raise self._error(
                "connection_failed", err, error=type(err).__name__
            ) from err

        try:
            return await self._run_connected(client)
        except asyncio.CancelledError:
            if self._boundary_crossed:
                self._warning(
                    "job cancelled in phase %s after transmission began; the print "
                    "outcome is uncertain: sent %d of %d chunks, last status %s",
                    self._phase,
                    self._sent,
                    len(self._chunks),
                    self._status_text(),
                )
            raise
        finally:
            await self._cleanup(client)

    async def _cleanup(self, client: BleakClient) -> None:
        """Stop notifications and disconnect; failures are only logged."""
        self._enter("cleanup")
        if not client.is_connected:
            return
        if self._subscribed:
            try:
                await clock.wait_for(
                    client.stop_notify(NOTIFY_CHARACTERISTIC_UUID), STOP_NOTIFY_TIMEOUT
                )
            except Exception as err:  # noqa: BLE001 - never replaces the outcome
                self._debug("stop_notify failed: %r", err)
                self._warning("stopping notifications failed: %s", type(err).__name__)
        try:
            await clock.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - never replaces the outcome
            self._debug("disconnect failed: %r", err)
            self._warning("disconnecting failed: %s", type(err).__name__)

    async def _run_connected(self, client: BleakClient) -> TransmitResult:
        self._enter("subscribing")
        if not has_ypl_profile(client.services):
            raise self._error("unsupported_profile")
        try:
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, self._on_notify)
        except Exception as err:
            raise self._error(
                "communication_failed", err, error=type(err).__name__
            ) from err
        self._subscribed = True

        self._enter("preflight")
        await self._preflight(client)

        self._enter("transmitting")
        await self._transmit(client)

        self._enter("post_send_observation")
        return await self._observe(client)

    # --- notifications (spec §6.4, §6.7) --------------------------------------

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._debug("disconnected")
        self._disconnected = True
        self._wake.set()

    def _on_notify(
        self, _characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Feed notification bytes to the decoder; never writes."""
        self._debug("notification %s", data.hex())
        for item in self._decoder.feed(bytes(data)):
            if isinstance(item, protocol.MalformedCandidate):
                self._debug(
                    "skipped malformed bytes %s: %s", item.raw.hex(), item.reason
                )
                continue
            try:
                envelope = protocol.decode_envelope(item)
                if not isinstance(envelope, protocol.Reply) or (
                    envelope.group,
                    envelope.command,
                ) != (protocol.GROUP_PRINT, protocol.CMD_STATUS):
                    self._debug("frame %s does not affect the job", item.raw.hex())
                    continue
                status = protocol.decode_status(envelope)
            except protocol.FrameError as err:
                self._debug("skipped malformed frame %s: %s", item.raw.hex(), err)
                continue
            self._on_status(status, item.raw)

    def _on_status(self, status: PrinterStatus, raw: bytes) -> None:
        self._last_status = status
        if self._boundary_crossed and status.raw not in (
            protocol.STATUS_READY,
            STATUS_PRINTING,
        ):
            self._error_after_boundary = status
        if self._poll_open and self._poll_reply is None:
            self._debug("status reply %s: %s", raw.hex(), format_raw(status))
            self._poll_reply = status
            self._wake.set()
        else:
            self._debug(
                "uncorrelated status reply %s: %s", raw.hex(), format_raw(status)
            )

    async def _poll(self, client: BleakClient) -> PrinterStatus | None:
        """Send one status poll and return its correlated reply, if any."""
        self._debug("status poll")
        try:
            await clock.wait_for(
                client.write_gatt_char(
                    WRITE_CHARACTERISTIC_UUID, STATUS_REQUEST_FRAME, response=False
                ),
                WRITE_TIMEOUT,
            )
        except Exception as err:
            raise _PollWriteError from err
        # Only a reply decoded after the write returned can satisfy this poll.
        self._poll_reply = None
        self._poll_open = True
        self._wake.clear()
        try:
            if not self._disconnected:
                await clock.wait_for(self._wake.wait(), REPLY_TIMEOUT)
        except TimeoutError:
            pass
        finally:
            self._poll_open = False
        reply = self._poll_reply
        self._debug(
            "status poll result: %s", "silent" if reply is None else format_raw(reply)
        )
        return reply

    async def _sleep_until(self, deadline: float) -> None:
        if (delay := deadline - clock.monotonic()) > 0:
            await clock.sleep(delay)

    # --- preflight (spec §6.5) ------------------------------------------------

    async def _preflight(self, client: BleakClient) -> None:
        started = clock.monotonic()
        silent = 0
        while True:
            poll_started = clock.monotonic()
            try:
                status = await self._poll(client)
            except _PollWriteError as err:
                cause = err.__cause__ or err
                raise self._error(
                    "communication_failed", cause, error=type(cause).__name__
                ) from cause
            if status is None:
                silent += 1
                if silent >= SILENT_POLL_LIMIT:
                    self._warning(
                        "no status reply to %d consecutive polls; printing with the "
                        "printer status unknown",
                        silent,
                    )
                    return
            else:
                silent = 0
                if status.raw == protocol.STATUS_READY:
                    return
                if status.raw != STATUS_PRINTING:
                    raise self._error(
                        "printer_not_ready",
                        flags=format_flags(status),
                        raw=format_raw(status),
                    )
                if clock.monotonic() - started >= PREFLIGHT_BUSY_LIMIT:
                    raise self._error("printer_busy")
            await self._sleep_until(poll_started + POLL_INTERVAL)

    # --- transmission (spec §6.6, §6.7) ---------------------------------------

    async def _transmit(self, client: BleakClient) -> None:
        for chunk in self._chunks:
            if self._boundary_crossed and self._disconnected:
                raise self._uncertain("outcome_uncertain_transport", "disconnect")
            self._boundary_crossed = True
            try:
                await clock.wait_for(
                    client.write_gatt_char(
                        WRITE_CHARACTERISTIC_UUID, chunk, response=False
                    ),
                    WRITE_TIMEOUT,
                )
            except Exception as err:
                raise self._uncertain("outcome_uncertain_transport", err) from err
            self._sent += 1
            self._sent_bytes += len(chunk)
            await clock.sleep(CHUNK_PAUSE)

    # --- post-send observation (spec §6.8) ------------------------------------

    def _check_observation(self) -> None:
        """Apply rules 1 and 2 of spec §6.8, in that order."""
        if self._error_after_boundary is not None:
            raise self._uncertain("outcome_uncertain_printer_error", "printer status")
        if self._disconnected:
            raise self._uncertain("outcome_uncertain_transport", "disconnect")

    async def _observe(self, client: BleakClient) -> TransmitResult:
        await clock.sleep(POST_SEND_WAIT)
        self._check_observation()
        started = clock.monotonic()
        silent = 0
        while True:
            poll_started = clock.monotonic()
            try:
                status = await self._poll(client)
            except _PollWriteError as err:
                cause = err.__cause__ or err
                if self._error_after_boundary is not None:
                    raise self._uncertain(
                        "outcome_uncertain_printer_error", "printer status"
                    ) from cause
                raise self._uncertain("outcome_uncertain_transport", cause) from cause
            self._check_observation()
            if status is None:
                silent += 1
                if silent >= SILENT_POLL_LIMIT:
                    return self._decide_on_silence()
            else:
                silent = 0
                if status.raw == protocol.STATUS_READY:
                    return TransmitResult(self._sent, status)
            if clock.monotonic() - started >= POST_SEND_LIMIT:
                raise self._uncertain(
                    "outcome_uncertain_still_printing", "printer status"
                )
            await self._sleep_until(poll_started + POLL_INTERVAL)
            self._check_observation()

    def _decide_on_silence(self) -> TransmitResult:
        """Apply rule 4 of spec §6.8 from the last valid status of this job."""
        status = self._last_status
        if status is None:
            self._warning(
                "no status reply after sending %d chunks; the printer status is "
                "unknown",
                self._sent,
            )
            return TransmitResult(self._sent, None)
        if status.raw == STATUS_PRINTING:
            raise self._uncertain("outcome_uncertain_still_printing", "printer status")
        return TransmitResult(self._sent, status)
