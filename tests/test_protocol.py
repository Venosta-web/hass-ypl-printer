"""Tests for the YPL-v1 protocol module (spec §3)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from custom_components.ypl_printer import protocol
from custom_components.ypl_printer.protocol import (
    Event,
    Frame,
    FrameDecoder,
    FrameError,
    MalformedCandidate,
    PrinterStatus,
    RasterError,
    Reply,
    build_print_stream,
    crc32,
    decode_envelope,
    decode_frame,
    decode_raster,
    decode_raster_prefix,
    decode_reply,
    decode_status,
    encode_frame,
    encode_raster,
    encode_row,
    validate_raster,
)

# Copied unmodified from yplib's captures/ directory at commit
# 4748c21393b30f78defed256a7394d0960ad9a07 (MIT, Shaun Lastra); see
# tests/fixtures/captures/README.md.
CAPTURES = Path(__file__).parent / "fixtures" / "captures"
FIXTURES = {
    "y50p-flashlabel-label.bin": (
        3106,
        "c2838f27da086f993da6be3793d9f762e60936746f14249e9fc36792a2ce8465",
    ),
    "y50p-horizontal-line.bin": (
        1507,
        "0d4e7b64a07b2767eaa96ab05206241e560b009e2da9e337bde6e176a08dcda0",
    ),
    "y50p-vertical-line.bin": (
        1581,
        "dd19f0e9fbedf097ad27e37a8831b286e47081c9634476e0d1a2c6e4e69d7c0d",
    ),
}

STATUS_FRAME = bytes.fromhex("1a010500050b0100002d54735ca1")
WHITE_ROW = [0] * 400
WHITE_RASTER = [WHITE_ROW] * 240

# 19 preamble frames, the compression-rate frame, the raster, 13 trailer frames.
RATE_SEGMENT = 19
RASTER_SEGMENT = 20
SEGMENT_COUNT = 34


def load_fixture(name: str) -> bytes:
    """Return a capture after checking it is the pinned upstream file."""
    data = (CAPTURES / name).read_bytes()
    size, sha256 = FIXTURES[name]
    assert len(data) == size
    assert hashlib.sha256(data).hexdigest() == sha256
    return data


def frame_length(data: bytes, offset: int) -> int:
    """Return the length a frame starting at ``offset`` declares."""
    return 9 + int.from_bytes(data[offset + 2 : offset + 4], "little")


def split_capture(data: bytes) -> tuple[list[Frame], bytes, list[Frame]]:
    """Split a capture into strict frames, the raster, and strict frames.

    The raster starts right after the ``05/39`` frame and ends where 240
    expanded rows end; the rest must be complete frames.
    """
    frames = []
    offset = 0
    while True:
        frame = decode_frame(data[offset : offset + frame_length(data, offset)])
        frames.append(frame)
        offset += len(frame.raw)
        if (frame.group, frame.command) == (0x05, 0x39):
            break
    _, raster_length = decode_raster_prefix(data[offset:])
    raster = data[offset : offset + raster_length]
    offset += raster_length
    trailer = []
    while offset < len(data):
        frame = decode_frame(data[offset : offset + frame_length(data, offset)])
        trailer.append(frame)
        offset += len(frame.raw)
    return frames, raster, trailer


def payload(frame: Frame) -> bytes:
    """Return the payload of a frame."""
    return frame.raw[4:-5]


def reply_frame(group: int, command: int, data: bytes) -> bytes:
    """Frame a direction-02 reply."""
    return encode_frame(
        bytes((group, command, 0x02)) + len(data).to_bytes(2, "little") + data
    )


# §3.7 vectors


@pytest.mark.parametrize(
    ("payload_hex", "expected"),
    [
        ("050b010000", 0x5C73542D),
        ("0104010000", 0xF190E2BB),
        ("053904010013", 0xFEC2F091),
    ],
)
def test_crc_vectors(payload_hex: str, expected: int) -> None:
    """The CRC matches the captured payload checksums."""
    assert crc32(bytes.fromhex(payload_hex)) == expected


def test_status_request_frame_vector() -> None:
    """The full status-request frame matches the capture."""
    assert encode_frame(protocol.STATUS_REQUEST_PAYLOAD) == STATUS_FRAME
    assert len(STATUS_FRAME) == 14


def test_white_row_vector() -> None:
    """An all-white row is 128 + 128 + 128 + 16 white pixels."""
    assert encode_row(WHITE_ROW).hex() == "187f7f7f0f"


def test_vertical_line_row_vector() -> None:
    """Black pixels [198, 202) encode as in the vertical-line capture."""
    row = [0] * 400
    row[198:202] = [1] * 4
    assert encode_row(row).hex() == "187f45837f45"


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_hashes(name: str) -> None:
    """Every vendored capture has its pinned size and SHA-256."""
    load_fixture(name)


# Fixture conformance


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_decodes(name: str) -> None:
    """Every capture is valid frames around a 240 x 400 raster."""
    frames, raster, trailer = split_capture(load_fixture(name))
    rows = decode_raster(raster)

    assert len(rows) == 240
    assert all(len(row) == 400 for row in rows)
    assert frames
    assert trailer
    validate_raster(rows)
    assert encode_raster(rows) == raster


def test_flashlabel_capture_rebuilds_exactly() -> None:
    """The FlashLabel capture is the builder's stream with its 0x13 rate frame.

    The builder pins the compression rate at 0x08; this capture was taken
    with 0x13, so only that one segment is replaced by the captured value.
    """
    data = load_fixture("y50p-flashlabel-label.bin")
    _, raster, _ = split_capture(data)
    segments = list(build_print_stream(decode_raster(raster)))
    assert segments[RATE_SEGMENT] == encode_frame(bytes.fromhex("053904010008"))

    segments[RATE_SEGMENT] = encode_frame(bytes.fromhex("053904010013"))

    assert b"".join(segments) == data


def test_horizontal_line_capture_rebuilds_exactly() -> None:
    """The horizontal-line capture is the builder's stream up to 05/21.

    The upstream snoop was stopped after the first trailer frame, so the
    capture ends there; it used the pinned 0x08 compression rate.
    """
    data = load_fixture("y50p-horizontal-line.bin")
    _, raster, trailer = split_capture(data)
    assert [payload(frame).hex() for frame in trailer] == ["0521010000"]

    segments = build_print_stream(decode_raster(raster))

    assert b"".join(segments[: RASTER_SEGMENT + 2]) == data


def test_vertical_line_capture_decodes_only() -> None:
    """The vertical-line capture starts mid-session with a status poll."""
    frames, raster, _ = split_capture(load_fixture("y50p-vertical-line.bin"))
    rows = decode_raster(raster)

    assert frames[0].raw == STATUS_FRAME
    expected = [0] * 400
    expected[198:202] = [1] * 4
    assert expected in rows


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_decodes_through_notification_decoder(name: str) -> None:
    """Frames around the raster decode incrementally in 20-byte pieces."""
    frames, raster, trailer = split_capture(load_fixture(name))
    stream = b"".join(frame.raw for frame in [*frames, *trailer])
    decoder = FrameDecoder()

    results = []
    for start in range(0, len(stream), 20):
        results += decoder.feed(stream[start : start + 20])

    assert results == [*frames, *trailer]


# Stream builder


def test_stream_segments() -> None:
    """The stream is framed preamble, rate, one raw raster, framed trailer."""
    segments = build_print_stream(WHITE_RASTER)

    assert len(segments) == SEGMENT_COUNT
    assert segments[RASTER_SEGMENT] == encode_raster(WHITE_RASTER)
    framed = [s for i, s in enumerate(segments) if i != RASTER_SEGMENT]
    assert [payload(decode_frame(s)).hex() for s in framed] == [
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
        "053904010008",
        "0521010000",
        "0537010000",
        "051a010000",
        *["050b010000"] * 10,
    ]


def test_stream_rejects_invalid_raster() -> None:
    """Nothing is built from an invalid raster."""
    with pytest.raises(RasterError):
        build_print_stream([WHITE_ROW] * 239)


# Frame encoding and strict decoding


def test_frame_round_trip() -> None:
    """A decoded frame exposes its header fields and raw bytes."""
    frame = decode_frame(encode_frame(bytes.fromhex("05410402003200")))

    assert frame == Frame(
        group=0x05,
        command=0x41,
        direction=0x04,
        data=bytes.fromhex("3200"),
        raw=encode_frame(bytes.fromhex("05410402003200")),
    )


@pytest.mark.parametrize(
    "payload_hex",
    [
        "",
        "050b0100",  # shorter than the payload header
        "050b000000",  # direction 00
        "050b050000",  # direction 05
        "050b010100",  # data length 1, no data
        "050b01000000",  # data length 0, one data byte
    ],
)
def test_encode_rejects_malformed_payload(payload_hex: str) -> None:
    """Malformed payloads are never framed."""
    with pytest.raises(FrameError):
        encode_frame(bytes.fromhex(payload_hex))


def test_encode_rejects_oversized_payload() -> None:
    """A payload longer than a u16 length cannot be framed."""
    data = bytes(0x10000 - 5)
    with pytest.raises(FrameError):
        encode_frame(bytes((5, 0x0B, 1)) + (0xFFFF).to_bytes(2, "little") + data)


def corrupt(index: int, value: int) -> bytes:
    """Return the status frame with one byte replaced."""
    frame = bytearray(STATUS_FRAME)
    frame[index] = value
    return bytes(frame)


def reframe(payload_bytes: bytes) -> bytes:
    """Frame a payload with a valid CRC but no payload validation."""
    return (
        protocol.FRAME_START
        + len(payload_bytes).to_bytes(2, "little")
        + payload_bytes
        + crc32(payload_bytes).to_bytes(4, "little")
        + b"\xa1"
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(STATUS_FRAME[:-1], id="truncated"),
        pytest.param(STATUS_FRAME + b"\x00", id="trailing-byte"),
        pytest.param(corrupt(0, 0x1B), id="bad-magic"),
        pytest.param(corrupt(1, 0x02), id="version-2"),
        pytest.param(corrupt(2, 0x06), id="length-too-long"),
        pytest.param(corrupt(2, 0x04), id="length-too-short"),
        pytest.param(corrupt(13, 0xA2), id="bad-terminator"),
        pytest.param(corrupt(9, 0x2E), id="bad-crc"),
        pytest.param(corrupt(5, 0x0C), id="payload-changed"),
        pytest.param(reframe(bytes.fromhex("0000")), id="length-below-header"),
        pytest.param(reframe(bytes.fromhex("050b000000")), id="direction-00"),
        pytest.param(reframe(bytes.fromhex("050b070000")), id="direction-07"),
        pytest.param(reframe(bytes.fromhex("050b020200aa")), id="nested-too-long"),
        pytest.param(reframe(bytes.fromhex("050b020000aa")), id="nested-too-short"),
    ],
)
def test_decode_rejects_malformed_frame(raw: bytes) -> None:
    """Every malformed frame raises a protocol error."""
    with pytest.raises(FrameError):
        decode_frame(raw)


# Notification decoding


def status_reply(value: int) -> bytes:
    """Frame a 05/0b integer status reply."""
    return reply_frame(0x05, 0x0B, bytes((0x03, 0x01, 0x00, value)))


def test_decoder_buffers_fragments() -> None:
    """A frame split across notifications is decoded once complete."""
    frame = status_reply(0x04)
    decoder = FrameDecoder()

    for byte in frame[:-1]:
        assert decoder.feed(bytes((byte,))) == []

    assert decoder.feed(frame[-1:]) == [decode_frame(frame)]
    assert decoder.feed(b"") == []


def test_decoder_splits_coalesced_frames() -> None:
    """Several frames in one notification are decoded in order."""
    frames = [status_reply(0), STATUS_FRAME, status_reply(1)]

    results = FrameDecoder().feed(b"".join(frames))

    assert results == [decode_frame(frame) for frame in frames]


def test_decoder_coalesced_and_fragmented() -> None:
    """A notification can finish one frame and start the next."""
    first, second = status_reply(0), status_reply(6)
    decoder = FrameDecoder()

    assert decoder.feed(first[:7]) == []
    assert decoder.feed(first[7:] + second[:3]) == [decode_frame(first)]
    assert decoder.feed(second[3:]) == [decode_frame(second)]


def test_decoder_reports_leading_noise() -> None:
    """Bytes before a frame start are surfaced, then the frame decodes."""
    results = FrameDecoder().feed(b"\x00\xa1\x1a\x02" + STATUS_FRAME)

    assert results == [
        MalformedCandidate(b"\x00\xa1\x1a\x02", "no frame start"),
        decode_frame(STATUS_FRAME),
    ]


def test_decoder_keeps_split_frame_start() -> None:
    """A trailing 1a may begin the next frame and is kept."""
    decoder = FrameDecoder()

    assert decoder.feed(b"\xff\x1a") == [MalformedCandidate(b"\xff", "no frame start")]
    assert decoder.feed(STATUS_FRAME[1:]) == [decode_frame(STATUS_FRAME)]


def test_decoder_resyncs_after_bad_crc() -> None:
    """A candidate with a bad CRC is surfaced and the next frame decodes."""
    bad = corrupt(9, 0x00)

    results = FrameDecoder().feed(bad + STATUS_FRAME)

    assert len(results) == 2
    assert isinstance(results[0], MalformedCandidate)
    assert results[0].raw == bad
    assert "CRC" in results[0].reason
    assert results[1] == decode_frame(STATUS_FRAME)


def test_decoder_resyncs_on_start_inside_bad_candidate() -> None:
    """A real frame hidden inside a rejected candidate is still found."""
    # Declares an 8-byte payload, so the candidate swallows the real frame's
    # start; its terminator is wrong and the decoder resyncs at the next 1a 01.
    bogus = bytes.fromhex("1a010800ffff")
    stream = bogus + STATUS_FRAME + status_reply(0)

    results = FrameDecoder().feed(stream)

    assert results == [
        MalformedCandidate(bogus, "terminator is 0x54, expected 0xa1"),
        decode_frame(STATUS_FRAME),
        decode_frame(status_reply(0)),
    ]


def test_decoder_rejects_short_length_without_waiting() -> None:
    """A length below the payload header is rejected immediately."""
    results = FrameDecoder().feed(bytes.fromhex("1a010400") + STATUS_FRAME)

    assert results == [
        MalformedCandidate(
            bytes.fromhex("1a010400"),
            "payload length 4 is shorter than its header",
        ),
        decode_frame(STATUS_FRAME),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(corrupt(13, 0x00), id="terminator"),
        pytest.param(reframe(bytes.fromhex("050b090000")), id="direction"),
        pytest.param(reframe(bytes.fromhex("050b020300aa")), id="nested-length"),
    ],
)
def test_decoder_skips_malformed_candidates(bad: bytes) -> None:
    """Malformed candidates are surfaced and never returned as frames."""
    results = FrameDecoder().feed(bad + status_reply(0))

    assert [type(result) for result in results] == [MalformedCandidate, Frame]
    assert results[0].raw == bad
    assert results[1] == decode_frame(status_reply(0))


def test_decoder_waits_for_declared_length() -> None:
    """An incomplete candidate is held, not rejected."""
    decoder = FrameDecoder()

    assert decoder.feed(STATUS_FRAME[:4]) == []
    assert decoder.feed(STATUS_FRAME[4:]) == [decode_frame(STATUS_FRAME)]


# Reply and event envelopes


def test_integer_reply() -> None:
    """Discriminator 03 carries a little-endian integer."""
    frame = decode_frame(reply_frame(0x05, 0x0B, bytes.fromhex("0302003412")))

    reply = decode_reply(frame)

    assert reply == Reply(
        group=0x05, command=0x0B, discriminator=0x03, value=0x1234, frame=frame
    )


def test_bytes_reply() -> None:
    """Discriminator 01 carries bytes."""
    frame = decode_frame(reply_frame(0x01, 0x04, b"\x01\x03\x00Y50"))

    assert decode_reply(frame).value == b"Y50"


def test_unknown_discriminator_stays_raw() -> None:
    """Unknown discriminators keep their value bytes undecoded."""
    frame = decode_frame(reply_frame(0x05, 0x11, bytes.fromhex("0201000a")))

    reply = decode_reply(frame)

    assert (reply.discriminator, reply.value) == (0x02, b"\x0a")


@pytest.mark.parametrize(
    "data_hex",
    [
        pytest.param("", id="empty"),
        pytest.param("0301", id="short-header"),
        pytest.param("030200aa", id="value-too-short"),
        pytest.param("030100aabb", id="value-too-long"),
        pytest.param("030000", id="empty-integer"),
    ],
)
def test_malformed_reply_envelope(data_hex: str) -> None:
    """Nested envelopes must be complete and exact."""
    frame = decode_frame(reply_frame(0x05, 0x0B, bytes.fromhex(data_hex)))

    with pytest.raises(FrameError):
        decode_reply(frame)


def test_decode_reply_rejects_other_directions() -> None:
    """Only direction-02 frames are replies."""
    with pytest.raises(FrameError):
        decode_reply(decode_frame(STATUS_FRAME))


def test_event_is_preserved_raw() -> None:
    """Unsolicited events keep their data uninterpreted."""
    raw = encode_frame(bytes.fromhex("050f030300010203"))
    frame = decode_frame(raw)

    event = decode_envelope(frame)

    assert event == Event(group=0x05, command=0x0F, data=b"\x01\x02\x03", frame=frame)
    assert event.frame.raw == raw


def test_envelope_of_reply_and_other_directions() -> None:
    """Replies are decoded; requests and sets stay plain frames."""
    reply = decode_frame(status_reply(0))
    request = decode_frame(STATUS_FRAME)

    assert isinstance(decode_envelope(reply), Reply)
    assert decode_envelope(request) is request


# Status decoding


def test_status_from_reply() -> None:
    """A 05/0b integer reply yields its status."""
    reply = decode_reply(decode_frame(status_reply(0x06)))

    assert decode_status(reply) == PrinterStatus(0x06)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(reply_frame(0x05, 0x0B, bytes.fromhex("01010000")), id="bytes"),
        pytest.param(reply_frame(0x05, 0x0C, bytes.fromhex("03010000")), id="command"),
        pytest.param(reply_frame(0x01, 0x0B, bytes.fromhex("03010000")), id="group"),
    ],
)
def test_status_requires_integer_status_reply(raw: bytes) -> None:
    """Only a 05/0b integer reply is a status."""
    with pytest.raises(FrameError):
        decode_status(decode_reply(decode_frame(raw)))


@pytest.mark.parametrize(
    ("raw", "flags", "unknown_bits"),
    [
        (0x00, ("ready",), 0),
        (0x01, ("printing",), 0),
        (0x02, ("cover_open",), 0),
        (0x04, ("paper_out",), 0),
        (0x06, ("cover_open", "paper_out"), 0),
        (0x08, ("undervoltage",), 0),
        (0x10, ("overheat",), 0),
        (0x1F, ("printing", "cover_open", "paper_out", "undervoltage", "overheat"), 0),
        (0x20, (), 0x20),
        (0x85, ("printing", "paper_out"), 0x80),
        (0x1234, ("paper_out", "overheat"), 0x1220),
    ],
)
def test_status_flags(raw: int, flags: tuple[str, ...], unknown_bits: int) -> None:
    """Known flags are named in bit order and unknown bits are preserved."""
    status = PrinterStatus(raw)

    assert status.raw == raw
    assert status.flags == flags
    assert status.unknown_bits == unknown_bits


# Raster validation and row-RLE


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(None, id="none"),
        pytest.param("0" * 240, id="string"),
        pytest.param([WHITE_ROW] * 239, id="239-rows"),
        pytest.param([WHITE_ROW] * 241, id="241-rows"),
        pytest.param([], id="no-rows"),
        pytest.param([WHITE_ROW] * 239 + [[0] * 399], id="399-wide"),
        pytest.param([[0] * 401] + [WHITE_ROW] * 239, id="401-wide"),
        pytest.param([WHITE_ROW] * 239 + [bytes(400)], id="bytes-row"),
        pytest.param([WHITE_ROW] * 239 + [[True] + [0] * 399], id="bool"),
        pytest.param([WHITE_ROW] * 239 + [[False] + [0] * 399], id="false"),
        pytest.param([WHITE_ROW] * 239 + [[1.0] + [0] * 399], id="float"),
        pytest.param([WHITE_ROW] * 239 + [[2] + [0] * 399], id="two"),
        pytest.param([WHITE_ROW] * 239 + [[-1] + [0] * 399], id="negative"),
        pytest.param([WHITE_ROW] * 239 + [["1"] + [0] * 399], id="str-pixel"),
        pytest.param([WHITE_ROW] * 239 + [[255] + [0] * 399], id="gray"),
    ],
)
def test_invalid_rasters_rejected(rows: object) -> None:
    """Only exactly 240 rows of 400 literal 0/1 integers are accepted."""
    with pytest.raises(RasterError):
        validate_raster(rows)
    with pytest.raises(RasterError):
        encode_raster(rows)


def test_encode_row_rejects_invalid_row() -> None:
    """Row encoding validates its row too."""
    with pytest.raises(RasterError):
        encode_row([0] * 399)


def test_tuple_raster_accepted() -> None:
    """Tuples are sequences of literal integers too."""
    validate_raster(tuple((0, 1) * 200 for _ in range(240)))


def test_run_lengths() -> None:
    """Runs split at 128 pixels and at colour changes."""
    row = [1] * 129 + [0] + [1] * 270

    assert encode_row(row).hex() == "18" + "ff80" + "00" + "ff" + "ff" + "8d"


def test_raster_round_trip() -> None:
    """Encoding then decoding returns the same raster."""
    rows = [[(x * y + x) % 3 % 2 for x in range(400)] for y in range(240)]

    assert decode_raster(encode_raster(rows)) == rows


def test_decode_prefix_reports_consumed_length() -> None:
    """A row marker value inside run data never ends the raster early."""
    row = [0] * 25 + [1] * 375  # first run is 0x18, the row marker value
    encoded = encode_raster([row] * 240)
    assert encoded[1] == 0x18

    rows, consumed = decode_raster_prefix(encoded + STATUS_FRAME)

    assert rows == [row] * 240
    assert consumed == len(encoded)


WHITE_ENCODED = encode_raster(WHITE_RASTER)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"", "ends before row 0", id="empty"),
        pytest.param(WHITE_ENCODED[:-5], "ends before row 239", id="239-rows"),
        pytest.param(WHITE_ENCODED[:-1], "row 239 is incomplete", id="incomplete-row"),
        pytest.param(WHITE_ENCODED[:-2], "row 239 is incomplete", id="short-row"),
        pytest.param(
            b"\x19" + WHITE_ENCODED[1:], "expected row marker", id="bad-first-marker"
        ),
        pytest.param(
            WHITE_ENCODED[:5] + b"\x7f\x7f\x7f\x0f" + WHITE_ENCODED[5:],
            "row 1 starts with 0x7f",
            id="missing-marker",
        ),
        pytest.param(
            b"\x18\x7f\x7f\x7f\x10" + WHITE_ENCODED[5:],
            "row 0 overruns",
            id="overrun",
        ),
        pytest.param(WHITE_ENCODED + b"\x18", "1 bytes follow", id="extra-marker"),
        pytest.param(WHITE_ENCODED + WHITE_ENCODED[:5], "5 bytes follow", id="241-rows"),
    ],
)
def test_malformed_raster_rejected(data: bytes, message: str) -> None:
    """Malformed row-RLE is rejected, never normalised."""
    with pytest.raises(RasterError, match=message):
        decode_raster(data)


# Boundaries


def test_module_imports_only_pure_stdlib() -> None:
    """The protocol module does no I/O and depends on nothing else."""
    tree = ast.parse(Path(protocol.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)

    assert imported == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "struct",
        "zlib",
    }
