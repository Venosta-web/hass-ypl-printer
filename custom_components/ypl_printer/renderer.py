"""Deterministic text renderer: label text to a 400 x 240 binary raster.

This module knows nothing about YPL encoding or Bluetooth. It validates label
text, lays it out with the bundled DejaVu Sans 2.37 font, and returns the fixed
media raster (spec §5.3-§5.7). Loading the font touches the filesystem, so
callers on the event loop must run it in an executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"

RASTER_WIDTH = 400
RASTER_HEIGHT = 240
MARGIN = 16
CONTENT_WIDTH = RASTER_WIDTH - 2 * MARGIN
CONTENT_HEIGHT = RASTER_HEIGHT - 2 * MARGIN
LINE_GAP = 4
MAX_FONT_SIZE = 160
MIN_FONT_SIZE = 12

# Pillow's top-left anchor for horizontal text: left edge, ascender line.
_ANCHOR = "la"
_THRESHOLD = 127

type Raster = list[list[int]]


class LabelTextError(ValueError):
    """Label text that the renderer refuses; raised before any rendering."""


class EmptyTextError(LabelTextError):
    """Label text is empty or consists only of spaces and line feeds."""


class UnsupportedCharacterError(LabelTextError):
    """Label text contains a character outside U+0020-U+007E and LF."""

    def __init__(self, position: int, codepoint: str) -> None:
        """Record the first rejected character."""
        super().__init__(f"unsupported character {codepoint} at {position}")
        self.position = position
        self.codepoint = codepoint


class TextDoesNotFitError(LabelTextError):
    """Label text does not fit the content box at the minimum font size."""

    def __init__(self, lines: int) -> None:
        """Record the number of lines that did not fit."""
        super().__init__(f"{lines} line(s) do not fit at size {MIN_FONT_SIZE}")
        self.lines = lines


class RenderError(RuntimeError):
    """The renderer produced output that violates the fixed media profile."""


@dataclass(frozen=True, slots=True)
class PlacedLine:
    """One line cell: its text and the top-left anchor it is drawn at."""

    text: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class LabelLayout:
    """The chosen font size and the position of every line cell."""

    font_size: int
    lines: tuple[PlacedLine, ...]


def normalize_text(text: str) -> str:
    """Turn CRLF and bare CR into LF, touching nothing else."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_text(text: str) -> str:
    """Return normalised label text, or raise a LabelTextError."""
    normalized = normalize_text(text)
    for position, char in enumerate(normalized):
        if char != "\n" and not " " <= char <= "~":
            raise UnsupportedCharacterError(position, f"U+{ord(char):04X}")
    if not normalized.strip(" \n"):
        raise EmptyTextError("label text is empty")
    return normalized


def layout_label(text: str) -> LabelLayout:
    """Validate label text and place it at the largest fitting font size."""
    lines = validate_text(text).split("\n")
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        if (layout := _try_layout(lines, size)) is not None:
            return layout
    raise TextDoesNotFitError(len(lines))


def render_label(text: str) -> Raster:
    """Render label text to 240 rows of 400 pixels, 1 black and 0 white."""
    layout = layout_label(text)
    font = _font(layout.font_size)
    image = Image.new("L", (RASTER_WIDTH, RASTER_HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "L"
    for line in layout.lines:
        if line.text:
            draw.text(
                (line.x, line.y),
                line.text,
                fill=0,
                font=font,
                anchor=_ANCHOR,
                stroke_width=0,
            )
    raster = binarize(image)
    validate_raster(raster)
    return raster


def binarize(image: Image.Image) -> Raster:
    """Threshold an 8-bit grayscale image: 0-127 becomes 1, 128-255 becomes 0."""
    if image.mode != "L":
        raise RenderError(f"expected an L image, got {image.mode}")
    width, height = image.size
    data = image.tobytes()
    return [
        [1 if value <= _THRESHOLD else 0 for value in data[row : row + width]]
        for row in range(0, width * height, width)
    ]


def validate_raster(raster: Raster) -> None:
    """Fail closed unless the raster is exactly 240 x 400 literal 0/1 ints."""
    if not isinstance(raster, list) or len(raster) != RASTER_HEIGHT:
        raise RenderError("raster must have exactly 240 rows")
    for row in raster:
        if not isinstance(row, list) or len(row) != RASTER_WIDTH:
            raise RenderError("raster rows must have exactly 400 pixels")
        for pixel in row:
            if type(pixel) is not int or pixel not in (0, 1):
                raise RenderError("raster pixels must be the integers 0 or 1")


def raster_sha256(raster: Raster) -> str:
    """Hash the raster as row-major bytes, one 0 or 1 byte per dot."""
    return hashlib.sha256(b"".join(bytes(row) for row in raster)).hexdigest()


@cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        FONT_PATH, size, layout_engine=ImageFont.Layout.BASIC
    )


def _try_layout(lines: list[str], size: int) -> LabelLayout | None:
    font = _font(size)
    ascent, descent = font.getmetrics()
    cell = ascent + descent
    block = len(lines) * cell + (len(lines) - 1) * LINE_GAP
    if block > CONTENT_HEIGHT:
        return None

    advances = [font.getlength(line) for line in lines]
    if any(advance > CONTENT_WIDTH for advance in advances):
        return None

    top = MARGIN + (CONTENT_HEIGHT - block) // 2
    placed = []
    for index, (line, advance) in enumerate(zip(lines, advances, strict=True)):
        x = MARGIN + int((CONTENT_WIDTH - advance) // 2)
        y = top + index * (cell + LINE_GAP)
        if not _ink_inside_content(font, line, x, y):
            return None
        placed.append(PlacedLine(line, x, y))
    return LabelLayout(size, tuple(placed))


def _ink_inside_content(
    font: ImageFont.FreeTypeFont, line: str, x: int, y: int
) -> bool:
    mask, (offset_x, offset_y) = font.getmask2(line, mode="L", anchor=_ANCHOR)
    if (ink := mask.getbbox()) is None:
        return True
    left, top, right, bottom = ink
    return (
        x + offset_x + left >= MARGIN
        and y + offset_y + top >= MARGIN
        and x + offset_x + right <= MARGIN + CONTENT_WIDTH
        and y + offset_y + bottom <= MARGIN + CONTENT_HEIGHT
    )
