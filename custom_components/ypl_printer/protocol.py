"""YPL-v1 wire protocol for the fixed 50 x 30 mm print path.

A narrow, fail-closed port of the captured Y50P path from yplib
(https://github.com/slastra/yplib, commit
4748c21393b30f78defed256a7394d0960ad9a07, MIT, Shaun Lastra); see
``docs/protocol.md``. This module performs no I/O, no BLE chunking and no
text or image rendering.

    frame   := 1a 01 <payload_length:u16-le> <payload> <crc32:u32-le> a1
    payload := <group:u8> <command:u8> <direction:u8> <data_length:u16-le> <data>
    raster  := (18 <run>+){240}, run := <colour:1><run_length - 1:7>
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import struct
import zlib

FRAME_START = b"\x1a\x01"
FRAME_END = 0xA1
# Magic, payload length, CRC and terminator around the payload.
FRAME_OVERHEAD = 9
# Group, command, direction and data length.
PAYLOAD_HEADER_LENGTH = 5
MAX_PAYLOAD_LENGTH = 0xFFFF

CRC_INIT = 0xCA896ADE

DIRECTION_REQUEST = 0x01
DIRECTION_REPLY = 0x02
DIRECTION_EVENT = 0x03
DIRECTION_SET = 0x04
DIRECTIONS = frozenset(
    {DIRECTION_REQUEST, DIRECTION_REPLY, DIRECTION_EVENT, DIRECTION_SET}
)

REPLY_BYTES = 0x01
REPLY_INTEGER = 0x03
# Discriminator, value length.
REPLY_HEADER_LENGTH = 3

GROUP_DEVICE = 0x01
GROUP_PRINT = 0x05

CMD_SERIAL = 0x02
CMD_MODEL = 0x04
CMD_FIRMWARE = 0x07
CMD_HARDWARE_INFO = 0xB7

CMD_STATUS = 0x0B
CMD_EVENT = 0x0F
CMD_DENSITY = 0x11
CMD_START_PRINT = 0x19
CMD_END_PRINT = 0x1A
CMD_PAPER_TYPE = 0x20
CMD_PAPER_LOCATE = 0x21
CMD_FIRST_TASK_WITHDRAWAL = 0x36
CMD_END_TASK_FORMFEED = 0x37
CMD_PRINT_WIDTH = 0x38
CMD_COMPRESSION_RATE = 0x39
CMD_X_REFERENCE = 0x40
CMD_CANVAS_WIDTH = 0x41

RASTER_WIDTH = 400
RASTER_HEIGHT = 240
ROW_MARKER = 0x18
MAX_RUN_LENGTH = 128

STATUS_REQUEST_PAYLOAD = bytes.fromhex("050b010000")

# The captured session order, including every repetition. The minimum
# sequence is unproven, so nothing here may be removed or reordered.
PREAMBLE_PAYLOADS: tuple[bytes, ...] = tuple(
    bytes.fromhex(payload)
    for payload in (
        *["0104010000"] * 2,
        "01b7010000",
        "0107010000",
        "0102010000",
        *["050b010000"] * 6,
        "052001010000",
        "051101010008",
        "0519010000",
        "0536010000",
        "050b010000",
        "05410402003200",
        "05380402003200",
        "05400402000000",
    )
)
# Fixed at 0x08; there is no dynamic compression-rate formula.
COMPRESSION_RATE_PAYLOAD = bytes.fromhex("053904010008")
TRAILER_PAYLOADS: tuple[bytes, ...] = tuple(
    bytes.fromhex(payload)
    for payload in (
        "0521010000",
        "0537010000",
        "051a010000",
        *["050b010000"] * 10,
    )
)

STATUS_READY = 0x00
STATUS_FLAGS: tuple[tuple[int, str], ...] = (
    (0x01, "printing"),
    (0x02, "cover_open"),
    (0x04, "paper_out"),
    (0x08, "undervoltage"),
    (0x10, "overheat"),
)
KNOWN_STATUS_BITS = 0x1F


class ProtocolError(Exception):
    """A frame, envelope or raster violates the YPL-v1 contract."""


class FrameError(ProtocolError):
    """A control frame or its envelope is malformed."""


class RasterError(ProtocolError):
    """A raster or its row-RLE encoding is malformed."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A strictly validated YPL-v1 control frame."""

    group: int
    command: int
    direction: int
    data: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class MalformedCandidate:
    """Bytes a notification decoder discarded while resynchronising."""

    raw: bytes
    reason: str


@dataclass(frozen=True, slots=True)
class Reply:
    """A direction-02 reply with a validated nested envelope.

    ``value`` is a little-endian integer for discriminator 03 and the raw
    value bytes for every other discriminator.
    """

    group: int
    command: int
    discriminator: int
    value: int | bytes
    frame: Frame


@dataclass(frozen=True, slots=True)
class Event:
    """A direction-03 unsolicited event; its data is never interpreted."""

    group: int
    command: int
    data: bytes
    frame: Frame


@dataclass(frozen=True, slots=True)
class PrinterStatus:
    """A printer status value with every bit preserved."""

    raw: int

    @property
    def flags(self) -> tuple[str, ...]:
        """Known flag names in ascending bit order; raw zero is ready."""
        if self.raw == STATUS_READY:
            return ("ready",)
        return tuple(name for bit, name in STATUS_FLAGS if self.raw & bit)

    @property
    def unknown_bits(self) -> int:
        """Bits without an evidence-backed meaning."""
        return self.raw & ~KNOWN_STATUS_BITS


def crc32(payload: bytes) -> int:
    """Return the YPL CRC-32 of a payload."""
    return zlib.crc32(payload, CRC_INIT ^ 0xFFFFFFFF)


def _validate_payload(payload: bytes) -> None:
    if len(payload) < PAYLOAD_HEADER_LENGTH:
        raise FrameError(f"payload is {len(payload)} bytes, shorter than its header")
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise FrameError(f"payload is {len(payload)} bytes, too long to frame")
    if payload[2] not in DIRECTIONS:
        raise FrameError(f"unknown direction 0x{payload[2]:02x}")
    (data_length,) = struct.unpack_from("<H", payload, 3)
    if data_length != len(payload) - PAYLOAD_HEADER_LENGTH:
        raise FrameError(
            f"data length {data_length} does not match "
            f"{len(payload) - PAYLOAD_HEADER_LENGTH} data bytes"
        )


def encode_frame(payload: bytes) -> bytes:
    """Wrap a validated payload in a YPL-v1 control frame."""
    payload = bytes(payload)
    _validate_payload(payload)
    return b"".join(
        (
            FRAME_START,
            struct.pack("<H", len(payload)),
            payload,
            struct.pack("<I", crc32(payload)),
            bytes((FRAME_END,)),
        )
    )


def decode_frame(raw: bytes) -> Frame:
    """Decode exactly one complete control frame, rejecting anything else."""
    raw = bytes(raw)
    if raw[:2] != FRAME_START:
        raise FrameError("frame does not start with 1a 01")
    if len(raw) < 4:
        raise FrameError("frame is shorter than its header")
    (payload_length,) = struct.unpack_from("<H", raw, 2)
    if payload_length < PAYLOAD_HEADER_LENGTH:
        raise FrameError(f"payload length {payload_length} is shorter than its header")
    if len(raw) != payload_length + FRAME_OVERHEAD:
        raise FrameError(
            f"frame is {len(raw)} bytes, expected "
            f"{payload_length + FRAME_OVERHEAD} for its payload length"
        )
    if raw[-1] != FRAME_END:
        raise FrameError(f"terminator is 0x{raw[-1]:02x}, expected 0xa1")
    payload = raw[4 : 4 + payload_length]
    (checksum,) = struct.unpack_from("<I", raw, 4 + payload_length)
    if checksum != crc32(payload):
        raise FrameError(
            f"CRC is 0x{checksum:08x}, expected 0x{crc32(payload):08x}"
        )
    _validate_payload(payload)
    return Frame(
        group=payload[0],
        command=payload[1],
        direction=payload[2],
        data=payload[PAYLOAD_HEADER_LENGTH:],
        raw=raw,
    )


class FrameDecoder:
    """Incremental decoder for notification bytes.

    Buffers fragments, splits coalesced frames and resynchronises on
    ``1a 01``. A candidate is decoded only once its declared length is
    complete; every discarded byte is reported as a ``MalformedCandidate``.
    """

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame | MalformedCandidate]:
        """Add notification bytes and return everything now decidable, in order."""
        self._buffer += data
        results: list[Frame | MalformedCandidate] = []
        while self._buffer:
            start = self._buffer.find(FRAME_START)
            if start < 0:
                # A trailing 1a may be the first half of the next frame start.
                keep = 1 if self._buffer[-1] == FRAME_START[0] else 0
                self._discard(len(self._buffer) - keep, "no frame start", results)
                break
            if start > 0:
                self._discard(start, "no frame start", results)
                continue
            if len(self._buffer) < 4:
                break
            (payload_length,) = struct.unpack_from("<H", self._buffer, 2)
            if payload_length < PAYLOAD_HEADER_LENGTH:
                self._resync(
                    f"payload length {payload_length} is shorter than its header",
                    results,
                )
                continue
            end = payload_length + FRAME_OVERHEAD
            if len(self._buffer) < end:
                break
            try:
                frame = decode_frame(self._buffer[:end])
            except FrameError as err:
                self._resync(str(err), results)
                continue
            del self._buffer[:end]
            results.append(frame)
        return results

    def _discard(
        self, count: int, reason: str, results: list[Frame | MalformedCandidate]
    ) -> None:
        if count:
            results.append(MalformedCandidate(bytes(self._buffer[:count]), reason))
            del self._buffer[:count]

    def _resync(self, reason: str, results: list[Frame | MalformedCandidate]) -> None:
        # Drop the rejected start and everything up to the next possible one.
        next_start = self._buffer.find(FRAME_START, 1)
        if next_start < 0:
            next_start = len(self._buffer)
            if self._buffer[-1] == FRAME_START[0] and next_start > 1:
                next_start -= 1
        self._discard(next_start, reason, results)


def decode_reply(frame: Frame) -> Reply:
    """Decode the nested envelope of a direction-02 reply."""
    if frame.direction != DIRECTION_REPLY:
        raise FrameError(f"direction 0x{frame.direction:02x} is not a reply")
    data = frame.data
    if len(data) < REPLY_HEADER_LENGTH:
        raise FrameError("reply data is shorter than its envelope header")
    (value_length,) = struct.unpack_from("<H", data, 1)
    if value_length != len(data) - REPLY_HEADER_LENGTH:
        raise FrameError(
            f"reply value length {value_length} does not match "
            f"{len(data) - REPLY_HEADER_LENGTH} value bytes"
        )
    value: int | bytes = data[REPLY_HEADER_LENGTH:]
    if data[0] == REPLY_INTEGER:
        if not value:
            raise FrameError("integer reply has no value bytes")
        value = int.from_bytes(value, "little")
    return Reply(
        group=frame.group,
        command=frame.command,
        discriminator=data[0],
        value=value,
        frame=frame,
    )


def decode_envelope(frame: Frame) -> Reply | Event | Frame:
    """Classify a received frame; anything but a reply or event stays raw."""
    if frame.direction == DIRECTION_REPLY:
        return decode_reply(frame)
    if frame.direction == DIRECTION_EVENT:
        return Event(
            group=frame.group, command=frame.command, data=frame.data, frame=frame
        )
    return frame


def decode_status(reply: Reply) -> PrinterStatus:
    """Return the status carried by a ``05/0b`` integer reply."""
    if (reply.group, reply.command) != (GROUP_PRINT, CMD_STATUS):
        raise FrameError(
            f"reply {reply.group:02x}/{reply.command:02x} is not a status reply"
        )
    if reply.discriminator != REPLY_INTEGER or not isinstance(reply.value, int):
        raise FrameError(
            f"status reply discriminator 0x{reply.discriminator:02x} "
            "is not an integer envelope"
        )
    return PrinterStatus(reply.value)


def validate_raster(rows: object) -> None:
    """Require exactly 240 rows of exactly 400 literal integers 0 or 1."""
    if not isinstance(rows, (list, tuple)):
        raise RasterError(f"raster is a {type(rows).__name__}, not a list of rows")
    if len(rows) != RASTER_HEIGHT:
        raise RasterError(f"raster has {len(rows)} rows, expected {RASTER_HEIGHT}")
    for index, row in enumerate(rows):
        _validate_row(row, index)


def _validate_row(row: object, index: int) -> None:
    if not isinstance(row, (list, tuple)):
        raise RasterError(f"row {index} is a {type(row).__name__}, not a list")
    if len(row) != RASTER_WIDTH:
        raise RasterError(
            f"row {index} has {len(row)} pixels, expected {RASTER_WIDTH}"
        )
    for pixel in row:
        # type() rather than isinstance(): booleans are not pixels.
        if type(pixel) is not int or pixel not in (0, 1):
            raise RasterError(f"row {index} contains non-binary pixel {pixel!r}")


def encode_row(row: Sequence[int]) -> bytes:
    """Row-RLE encode one validated 400-pixel row."""
    _validate_row(row, 0)
    out = bytearray((ROW_MARKER,))
    x = 0
    while x < RASTER_WIDTH:
        colour = row[x]
        run = 1
        while (
            x + run < RASTER_WIDTH
            and run < MAX_RUN_LENGTH
            and row[x + run] == colour
        ):
            run += 1
        out.append((colour << 7) | (run - 1))
        x += run
    return bytes(out)


def encode_raster(rows: Sequence[Sequence[int]]) -> bytes:
    """Validate and row-RLE encode a complete 400 x 240 raster."""
    validate_raster(rows)
    return b"".join(encode_row(row) for row in rows)


def decode_raster_prefix(data: bytes) -> tuple[list[list[int]], int]:
    """Decode 240 rows from the start of ``data``.

    Returns the rows and the number of bytes they occupy. A row ends only
    after exactly 400 pixels have been expanded; ``0x18`` is never scanned
    for as a delimiter.
    """
    rows: list[list[int]] = []
    offset = 0
    while len(rows) < RASTER_HEIGHT:
        index = len(rows)
        if offset >= len(data):
            raise RasterError(f"raster ends before row {index}")
        if data[offset] != ROW_MARKER:
            raise RasterError(
                f"row {index} starts with 0x{data[offset]:02x} at byte {offset}, "
                "expected row marker 0x18"
            )
        offset += 1
        row: list[int] = []
        while len(row) < RASTER_WIDTH:
            if offset >= len(data):
                raise RasterError(f"row {index} is incomplete at {len(row)} pixels")
            run = data[offset]
            offset += 1
            length = (run & 0x7F) + 1
            if len(row) + length > RASTER_WIDTH:
                raise RasterError(f"row {index} overruns {RASTER_WIDTH} pixels")
            row.extend([run >> 7] * length)
        rows.append(row)
    return rows, offset


def decode_raster(data: bytes) -> list[list[int]]:
    """Decode a complete raster segment, rejecting trailing bytes."""
    rows, consumed = decode_raster_prefix(data)
    if consumed != len(data):
        raise RasterError(f"{len(data) - consumed} bytes follow row {RASTER_HEIGHT - 1}")
    return rows


def build_print_stream(rows: Sequence[Sequence[int]]) -> tuple[bytes, ...]:
    """Build the exact one-label stream as ordered segments.

    Each framed command is its own segment; the raster is one unframed
    segment between the compression-rate frame and the trailer. The raster
    is validated before anything is built.
    """
    raster = encode_raster(rows)
    return (
        *(encode_frame(payload) for payload in PREAMBLE_PAYLOADS),
        encode_frame(COMPRESSION_RATE_PAYLOAD),
        raster,
        *(encode_frame(payload) for payload in TRAILER_PAYLOADS),
    )
