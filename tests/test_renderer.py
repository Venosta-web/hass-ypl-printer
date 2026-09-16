"""Tests for the deterministic text renderer (spec §5.3-§5.7, §9 tests 3-6)."""

import ast
import hashlib

from PIL import Image, ImageFont
import pytest

from custom_components.ypl_printer import renderer
from custom_components.ypl_printer.renderer import (
    EmptyTextError,
    RenderError,
    TextDoesNotFitError,
    UnsupportedCharacterError,
    layout_label,
    render_label,
    validate_text,
)

from .const import ACCEPTANCE_SPECIMEN

# The golden raster was produced with Home Assistant Core 2026.9.2, which pins
# Pillow 12.3.0 with FreeType 2.14.3. A different Pillow or FreeType that
# changes this hash is an explicit contract review (spec §5.6).
GOLDEN_PILLOW = "12.3.0"
GOLDEN_FREETYPE = "2.14.3"
GOLDEN_SPECIMEN_FONT_SIZE = 27
GOLDEN_SPECIMEN_RASTER_SHA256 = (
    "5da7023fde28d5e87a502093890bf72431203d1d22fe0c684c0a3fb411934360"
)

FONT_SIZE_BYTES = 757_076
FONT_SHA256 = "7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        renderer.FONT_PATH, size, layout_engine=ImageFont.Layout.BASIC
    )


def _ink_box(raster: list[list[int]]) -> tuple[int, int, int, int]:
    rows = [y for y, row in enumerate(raster) if any(row)]
    cols = [x for x in range(400) if any(row[x] for row in raster)]
    return cols[0], rows[0], cols[-1], rows[-1]


# --- 3. Newline normalisation and text rejection boundaries -----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("a\nb", "a\nb"),
        ("a\r\r\nb", "a\n\nb"),
        ("a\n\rb", "a\n\nb"),
        ("a\r\n", "a\n"),
    ],
)
def test_newlines_normalised_to_lf(text: str, expected: str) -> None:
    """CRLF and bare CR become LF before validation."""
    assert validate_text(text) == expected


@pytest.mark.parametrize(
    "text", [" ", "~", "".join(chr(c) for c in range(0x20, 0x7F))]
)
def test_basic_latin_boundaries_accepted(text: str) -> None:
    """U+0020 through U+007E are accepted."""
    assert validate_text(f"x{text}") == f"x{text}"


@pytest.mark.parametrize(
    ("text", "position", "codepoint"),
    [
        ("a\tb", 1, "U+0009"),
        ("\x00", 0, "U+0000"),
        ("ab\x1f", 2, "U+001F"),
        ("a\x7f", 1, "U+007F"),
        ("\x80", 0, "U+0080"),
        ("a b", 1, "U+00A0"),
        ("café", 3, "U+00E9"),
        (" ", 0, "U+2028"),
        ("ok \U0001f600", 3, "U+1F600"),
        ("\x0b\x0c", 0, "U+000B"),
        # Position counts after normalisation: CRLF collapses to one LF.
        ("a\r\n\r\nb\tc", 4, "U+0009"),
        # The first rejected code point wins.
        ("aé\tb", 1, "U+00E9"),
        # Invalid characters are reported even among only spaces.
        ("  \t ", 2, "U+0009"),
    ],
)
def test_unsupported_characters_rejected(
    text: str, position: int, codepoint: str
) -> None:
    """Controls, DEL and non-Basic-Latin code points are rejected."""
    with pytest.raises(UnsupportedCharacterError) as err:
        validate_text(text)

    assert err.value.position == position
    assert err.value.codepoint == codepoint


@pytest.mark.parametrize("text", ["", " ", "\n", "   \n  ", "\r\n", "\r", " \r \n"])
def test_empty_text_rejected(text: str) -> None:
    """Empty input and input of only spaces and line feeds is rejected."""
    with pytest.raises(EmptyTextError):
        validate_text(text)


def test_rejection_happens_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid text never reaches the font."""

    def no_font(size: int) -> None:
        raise AssertionError("font loaded for invalid text")

    monkeypatch.setattr(renderer, "_font", no_font)
    with pytest.raises(UnsupportedCharacterError):
        render_label("a\tb")
    with pytest.raises(EmptyTextError):
        render_label(" \n ")


# --- 4. Preservation of spaces and blank/trailing lines ----------------------


@pytest.mark.parametrize(
    "text", ["  a  ", "a    b", "\na", "a\n", "a\n\n\nb", "a\n  \n", "  \n\na"]
)
def test_accepted_text_preserved(text: str) -> None:
    """Every accepted character survives validation and layout unchanged."""
    assert validate_text(text) == text
    layout = layout_label(text)
    assert [line.text for line in layout.lines] == text.split("\n")


def test_trailing_blank_line_takes_a_line_cell() -> None:
    """A trailing LF adds a blank line cell that shrinks and shifts the block."""
    single = layout_label("HELLO")
    trailing = layout_label("HELLO\n")

    assert len(trailing.lines) == 2
    assert trailing.lines[1].text == ""
    assert trailing.font_size < single.font_size


def test_spaces_count_toward_centring() -> None:
    """Leading and trailing spaces are part of the logical advance."""
    plain = layout_label("ab\nabcdefghij").lines[0]
    leading = layout_label("  ab\nabcdefghij").lines[0]
    trailing = layout_label("ab  \nabcdefghij").lines[0]

    assert leading.x < plain.x
    assert trailing.x < plain.x
    assert leading.x == trailing.x


def test_blank_lines_draw_nothing() -> None:
    """A blank or space-only line cell leaves its pixels white."""
    layout = layout_label("X\n   \nX")
    raster = render_label("X\n   \nX")
    font = _font(layout.font_size)
    ascent, descent = font.getmetrics()
    middle = layout.lines[1]

    assert not any(any(row) for row in raster[middle.y : middle.y + ascent + descent])


# --- 5. Largest fitting size, centring, margins, overflow, no wrapping -------


@pytest.mark.parametrize(
    "text",
    [ACCEPTANCE_SPECIMEN, "W", "WWWWWWWWWWWWWWWWWWWWWWWWWWWWW", "a\nb\nc\nd\ne\nf"],
)
def test_largest_fitting_size_chosen(text: str) -> None:
    """The chosen size fits and the next size up does not."""
    lines = text.split("\n")
    layout = layout_label(text)

    assert renderer._try_layout(lines, layout.font_size) == layout
    if layout.font_size < renderer.MAX_FONT_SIZE:
        assert renderer._try_layout(lines, layout.font_size + 1) is None


def test_search_starts_at_160() -> None:
    """Short text uses the maximum size and never a larger one."""
    assert layout_label("i").font_size == 160


def test_specimen_font_size() -> None:
    """The acceptance specimen fits well above the minimum size."""
    assert layout_label(ACCEPTANCE_SPECIMEN).font_size == GOLDEN_SPECIMEN_FONT_SIZE


@pytest.mark.parametrize(
    "text", [ACCEPTANCE_SPECIMEN, "i", " a\nbbbbbbbbbbbbbbb \n\nc", "Mj\nMj\nMj"]
)
def test_lines_centred_with_floor_division(text: str) -> None:
    """Lines centre by logical advance; the block centres vertically."""
    layout = layout_label(text)
    font = _font(layout.font_size)
    ascent, descent = font.getmetrics()
    cell = ascent + descent
    count = len(layout.lines)
    block = count * cell + (count - 1) * 4
    top = 16 + (208 - block) // 2

    for index, line in enumerate(layout.lines):
        advance = font.getlength(line.text)
        assert advance == int(advance)
        assert line.x == 16 + (368 - int(advance)) // 2
        assert line.y == top + index * (cell + 4)


@pytest.mark.parametrize(
    "text", [ACCEPTANCE_SPECIMEN, "g", "Qj|", "|\n|\n|\n|\n|\n|\n|\n|\n|\n|\n|"]
)
def test_ink_stays_inside_margins(text: str) -> None:
    """Nothing is drawn in the 16 px margin on any edge."""
    left, top, right, bottom = _ink_box(render_label(text))

    assert left >= 16
    assert top >= 16
    assert right <= 383
    assert bottom <= 223


def test_eleven_lines_fit_twelve_do_not() -> None:
    """At size 12 a line cell is 15 px: 11 lines use 205 px, 12 need 224."""
    assert _font(12).getmetrics() == (12, 3)
    assert layout_label("\n".join("x" * 11)).font_size == 12

    with pytest.raises(TextDoesNotFitError) as err:
        layout_label("\n".join("x" * 12))
    assert err.value.lines == 12


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("M" * 60, 1),
        ("ok\n" + "M" * 60, 2),
        ("a\n" * 40, 41),
        (" " * 200, None),  # only spaces: empty, not overflow
    ],
)
def test_overflow_rejected(text: str, lines: int | None) -> None:
    """Text that does not fit at size 12 is rejected, never clipped."""
    if lines is None:
        with pytest.raises(EmptyTextError):
            layout_label(text)
        return
    with pytest.raises(TextDoesNotFitError) as err:
        render_label(text)
    assert err.value.lines == lines


def test_space_advance_counts_toward_overflow() -> None:
    """Trailing spaces alone can push a line past the content width."""
    text = "M" * 30
    assert layout_label(text).font_size >= 12
    with pytest.raises(TextDoesNotFitError):
        layout_label(text + " " * 60)


def test_no_wrapping() -> None:
    """A long line shrinks the font instead of wrapping onto another line."""
    text = "The quick brown fox jumps over the lazy dog again"
    layout = layout_label(text)

    assert len(layout.lines) == 1
    assert layout.lines[0].text == text
    assert layout.font_size < 20


def test_ink_bounds_checked_after_centring(monkeypatch: pytest.MonkeyPatch) -> None:
    """A size is rejected when centred ink would cross the content box."""
    monkeypatch.setattr(renderer, "_ink_inside_content", lambda *args: False)
    with pytest.raises(TextDoesNotFitError):
        layout_label("a")


# --- 6. Font hash, threshold, raster shape/domain, golden raster -------------


def test_bundled_font_is_unmodified_dejavu_sans_2_37() -> None:
    """The renderer loads the vendored, test-locked TTF and nothing else."""
    data = renderer.FONT_PATH.read_bytes()

    assert renderer.FONT_PATH.name == "DejaVuSans.ttf"
    assert renderer.FONT_PATH.parent.parent.name == "ypl_printer"
    assert len(data) == FONT_SIZE_BYTES
    assert hashlib.sha256(data).hexdigest() == FONT_SHA256


def test_threshold_127_black_128_white() -> None:
    """Gray 0..127 becomes 1 (black); 128..255 becomes 0 (white)."""
    image = Image.new("L", (256, 1))
    image.putdata(list(range(256)))

    (row,) = renderer.binarize(image)

    assert row[127] == 1
    assert row[128] == 0
    assert row == [1] * 128 + [0] * 128


def test_binarize_is_row_major() -> None:
    """Rows come out top to bottom, pixels left to right."""
    image = Image.new("L", (3, 2), 255)
    image.putpixel((2, 0), 0)
    image.putpixel((0, 1), 127)

    assert renderer.binarize(image) == [[0, 0, 1], [1, 0, 0]]


def test_binarize_rejects_non_grayscale() -> None:
    """Only 8-bit L images are thresholded; no implicit conversion."""
    with pytest.raises(RenderError):
        renderer.binarize(Image.new("1", (4, 4)))


def test_raster_shape_and_domain() -> None:
    """The render is 240 rows of 400 literal integers 0 or 1."""
    raster = render_label(ACCEPTANCE_SPECIMEN)

    assert type(raster) is list
    assert len(raster) == 240
    for row in raster:
        assert type(row) is list
        assert len(row) == 400
        assert all(type(pixel) is int and pixel in (0, 1) for pixel in row)
    assert any(1 in row for row in raster)


def _blank() -> list[list[int]]:
    return [[0] * 400 for _ in range(240)]


def _with_pixel(value: object) -> list[list]:
    raster = _blank()
    raster[5][7] = value
    return raster


@pytest.mark.parametrize(
    "raster",
    [
        _blank()[:-1],
        [*_blank(), [0] * 400],
        [[0] * 399, *_blank()[1:]],
        [*_blank()[:-1], [0] * 401],
        [tuple(row) for row in _blank()],
        tuple(_blank()),
        _with_pixel(True),
        _with_pixel(False),
        _with_pixel(2),
        _with_pixel(-1),
        _with_pixel(1.0),
        _with_pixel("1"),
        _with_pixel(None),
    ],
)
def test_malformed_raster_rejected(raster: list) -> None:
    """Wrong shapes, booleans and other values fail closed."""
    with pytest.raises(RenderError):
        renderer.validate_raster(raster)


def test_render_revalidates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renderer fault that breaks the raster profile fails closed."""
    monkeypatch.setattr(renderer, "binarize", lambda image: [[True] * 400] * 240)
    with pytest.raises(RenderError):
        render_label("a")


def test_raster_sha256_hashes_one_byte_per_dot() -> None:
    """The canonical hash input is 96,000 row-major bytes of 0 or 1."""
    raster = _blank()
    raster[0][1] = 1
    expected = bytearray(96_000)
    expected[1] = 1

    assert renderer.raster_sha256(raster) == hashlib.sha256(expected).hexdigest()


def test_golden_specimen_raster() -> None:
    """The acceptance specimen renders to the pinned raster."""
    import PIL
    from PIL import features

    runtime = f"Pillow {PIL.__version__}, FreeType {features.version('freetype2')}"
    golden = f"Pillow {GOLDEN_PILLOW}, FreeType {GOLDEN_FREETYPE}"

    digest = renderer.raster_sha256(render_label(ACCEPTANCE_SPECIMEN))

    assert digest == GOLDEN_SPECIMEN_RASTER_SHA256, (
        f"golden raster changed on {runtime} (pinned on {golden}): "
        "this is a renderer contract review"
    )


def test_render_is_repeatable() -> None:
    """Rendering the same text twice gives the same raster."""
    assert render_label(ACCEPTANCE_SPECIMEN) == render_label(ACCEPTANCE_SPECIMEN)


# --- Layer independence -------------------------------------------------------


def test_renderer_is_independent_of_protocol_and_transport() -> None:
    """The renderer imports only the standard library and Pillow."""
    tree = ast.parse(renderer.__loader__.get_source(renderer.__name__))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "renderer must not import sibling modules"
            modules.add(node.module)

    roots = {module.split(".")[0] for module in modules}
    assert roots <= {"__future__", "dataclasses", "functools", "hashlib", "pathlib", "PIL"}
