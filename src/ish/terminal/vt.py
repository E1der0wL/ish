"""A small libghostty-vt binding for the separate full-screen prototype.

All methods belong to one event-loop thread. The native bridge owns VT state;
Python receives a bounded bulk snapshot, never one FFI call per terminal cell.
Images are read by generation separately from the cell snapshot.
"""

from __future__ import annotations

import ctypes
import io
import math
import os
import struct
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from wcwidth import iter_graphemes

from ish.lang import Message, i18n
from ish.runtime.distribution import bundled_vt_library

from .engine import EngineError, check_library
from .identity import IDENTITY, LEGACY_F3, TERM, legacy_control
from .inputs import InputPipeline

SHIFT = 1
CTRL = 2
ALT = 4
KEY_REPORT_EVENTS = 2
MAX_CELLS = 262_144
MAX_BUFFER = 32 * 1024 * 1024
DEFAULT_SCROLLBACK_LINES = 10_000
DEFAULT_SCROLLBACK_BYTES = 32 * 1024 * 1024
LIBRARY_ENV = "ISH_GHOSTTY_LIB"
DEFAULT_LIBRARY = bundled_vt_library() or (
    Path(__file__).resolve().parents[3] / ".cache/ghostty-vt/install/lib/libish_vt.so"
)
COLOR_FG = 1
COLOR_BG = 2
STYLE_FLAGS = (
    (1, "bold"),
    (2, "italic"),
    (4, "dim"),
    (8, "blink"),
    (16, "reverse"),
    (32, "hidden"),
    (64, "strike"),
)
SPECIAL_KEYS = frozenset(
    {
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
        "delete",
        "backspace",
        "enter",
        "tab",
        "escape",
        *(f"f{number}" for number in range(1, 26)),
        *(f"kp{number}" for number in range(10)),
        "kpdecimal",
        "kpdivide",
        "kpmultiply",
        "kpsubtract",
        "kpadd",
        "kpenter",
        "kpequal",
        "kpseparator",
        "kpleft",
        "kpright",
        "kpup",
        "kpdown",
        "kppageup",
        "kppagedown",
        "kphome",
        "kpend",
        "kpinsert",
        "kpdelete",
        "kpbegin",
    }
)


class Cell(NamedTuple):
    """One native grapheme at an exact grid position; wide tails are omitted."""

    x: int
    text: str
    width: int
    style: str
    flags: int
    fg: tuple[int, int, int] | None
    bg: tuple[int, int, int] | None
    underline: int
    underline_color: tuple[int, int, int] | None
    hyperlink: str | None

    @property
    def overline(self) -> bool:
        """Native SGR 53 decoration, independent of PTK's basic style vocabulary."""
        return bool(self.flags & 128)


@dataclass(frozen=True, slots=True)
class _RenderedRow:
    """An exact native row record and its immutable Python rendering."""

    record: bytes
    runs: tuple[tuple[str, str], ...]
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class SelectionState:
    """Native gesture results; the frontend owns only its input timer."""

    click_count: int
    dragged: bool
    autoscroll: int
    behavior: str


@dataclass(frozen=True)
class Image:
    """An immutable copy of one native image generation, normalized to RGBA."""

    id: int
    generation: int
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class ImageContext:
    """Live native cursor and styling at a complete input protocol boundary."""

    cols: int
    rows: int
    x: int
    y: int
    cell_width: int
    cell_height: int
    origin: int
    wraparound: int
    insert: int
    margins: int
    pending_wrap: int
    ground: int
    style: bytes


@dataclass(frozen=True)
class Placement:
    """A native source crop and destination rectangle in viewport pixels."""

    id: int
    image_id: int
    image_generation: int
    x: int
    y: int
    width: int
    height: int
    source_x: int
    source_y: int
    source_width: int
    source_height: int
    layer: int
    z: int


@dataclass(frozen=True)
class Effect:
    """A bounded native side effect for the sole outer output owner."""

    kind: str
    data: bytes = b""


@dataclass(frozen=True)
class Frame:
    """Visible terminal rows as PTK style/text runs and a zero-based cursor."""

    lines: list[list[tuple[str, str]]]
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    alternate: bool
    cols: int
    rows: int
    viewport_active: bool = True
    scrollback_rows: int = 0
    viewport_offset: int = 0
    cells: tuple[tuple[Cell, ...], ...] = ()
    cursor_shape: str = "block"
    cursor_blink: bool = False
    cell_width: int = 0
    cell_height: int = 0
    images: tuple[Placement, ...] = ()
    image_generation: int = 0
    cursor_color: tuple[int, int, int] | None = None

    @property
    def cursor_default(self) -> bool:
        """Whether cursor presentation should inherit the outer terminal default."""
        return self.cursor_color is None


class _DecodedImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
        ("data_len", ctypes.c_size_t),
    ]


_DecodePNG = ctypes.CFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(_DecodedImage),
)
_PNG_CALLBACKS: list[object] = []


def _install_png_decoder(library: ctypes.CDLL) -> None:
    """Install once before native state exists; the callback owns no VT state."""

    @_DecodePNG
    def decode(_userdata, allocator, data, length, output):
        """Copy a bounded PNG into the allocator supplied by the VT engine."""
        try:
            from PIL import Image as PillowImage

            if not 0 < length <= MAX_BUFFER:
                return False
            with warnings.catch_warnings():
                warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
                png = PillowImage.open(io.BytesIO(ctypes.string_at(data, length)))
            with png:
                if (
                    png.format != "PNG"
                    or not 0 < png.width * png.height <= (MAX_BUFFER - 16) // 4
                ):
                    return False
                rgba = png.convert("RGBA").tobytes()
                target = library.ish_vt_image_alloc(allocator, len(rgba))
                if not target:
                    return False
                ctypes.memmove(target, rgba, len(rgba))
                output.contents.width = png.width
                output.contents.height = png.height
                output.contents.data = target
                output.contents.data_len = len(rgba)
                return True
        except Exception:
            # Exceptions must never escape a C callback.
            return False

    _check(library.ish_vt_png_decoder(decode))
    _PNG_CALLBACKS.append(decode)


class TerminalError(RuntimeError):
    """The native VT library could not complete an operation."""


def _dimensions(cols: int, rows: int) -> None:
    if not 0 < cols <= 65_535 or not 0 < rows <= 65_535 or cols * rows > MAX_CELLS:
        raise ValueError(
            i18n.get(Message.VT_DIMENSIONS, limit=MAX_CELLS, cols=cols, rows=rows)
        )


def _check(status: int) -> None:
    if status:
        if status == -102:
            raise TerminalError(i18n.get(Message.VT_STATE_LOST))
        raise TerminalError(i18n.get(Message.VT_BRIDGE_ERROR, status=status))


@lru_cache(maxsize=4096)
def _style(
    flags: int,
    colors: int,
    red: int,
    green: int,
    blue: int,
    bg_red: int,
    bg_green: int,
    bg_blue: int,
) -> str:
    parts = [
        f"fg:#{red:02x}{green:02x}{blue:02x}" if colors & COLOR_FG else "fg:default",
        f"bg:#{bg_red:02x}{bg_green:02x}{bg_blue:02x}"
        if colors & COLOR_BG
        else "bg:default",
    ]
    parts.extend(name for bit, name in STYLE_FLAGS if flags & bit)
    if flags & 0x700:
        parts.append("underline")
    return " ".join(parts)


@lru_cache(maxsize=1)
def _load() -> ctypes.CDLL:
    from .diagnostics import LogCallback, capture

    path = Path(os.environ.get(LIBRARY_ENV, DEFAULT_LIBRARY))
    try:
        library = ctypes.CDLL(str(path))
    except OSError as error:
        raise TerminalError(
            i18n.get(
                Message.VT_LIBRARY_LOAD, path=path, variable=LIBRARY_ENV, error=error
            )
        ) from error
    try:
        library._ish_engine = check_library(library)
    except EngineError as error:
        raise TerminalError(str(error)) from error
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    unsigned = ctypes.c_uint
    output = [ctypes.POINTER(pointer), ctypes.POINTER(size)]
    signatures = {
        "ish_vt_new": ([unsigned, unsigned], pointer),
        "ish_vt_free": ([pointer], None),
        "ish_vt_feed": ([pointer, ctypes.c_char_p, size], ctypes.c_int),
        "ish_vt_log": ([LogCallback], ctypes.c_int),
        "ish_vt_processing_error": ([pointer], ctypes.c_int),
        "ish_vt_keyboard_flags": (
            [pointer, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int,
        ),
        "ish_vt_default_cursor": ([pointer, unsigned, ctypes.c_bool], ctypes.c_int),
        "ish_vt_identity": (
            [pointer, ctypes.c_char_p, size, ctypes.c_char_p, size],
            ctypes.c_int,
        ),
        "ish_vt_search_set": ([pointer, ctypes.c_char_p, size], ctypes.c_int),
        "ish_vt_search_step": (
            [
                pointer,
                unsigned,
                ctypes.POINTER(unsigned),
                ctypes.POINTER(ctypes.c_uint64),
            ],
            ctypes.c_int,
        ),
        "ish_vt_search_select": (
            [pointer, ctypes.c_bool, ctypes.POINTER(ctypes.c_uint64)],
            ctypes.c_int,
        ),
        "ish_vt_osc_unknown": ([ctypes.c_char_p, size], ctypes.c_int),
        "ish_vt_resize": (
            [pointer, unsigned, unsigned, unsigned, unsigned],
            ctypes.c_int,
        ),
        "ish_vt_replies": ([pointer, *output], ctypes.c_int),
        "ish_vt_frame": ([pointer, *output], ctypes.c_int),
        "ish_vt_effects": ([pointer, *output], ctypes.c_int),
        "ish_vt_graphics": ([pointer, ctypes.c_uint64], ctypes.c_int),
        "ish_vt_sixel_supported": ([pointer, ctypes.c_bool], ctypes.c_int),
        "ish_vt_appearance": (
            [pointer, ctypes.c_char_p, size, ctypes.c_int],
            ctypes.c_int,
        ),
        "ish_vt_image_context": ([pointer, *output], ctypes.c_int),
        "ish_vt_unused_image_id": (
            [pointer, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int,
        ),
        "ish_vt_release_hold": ([pointer], ctypes.c_int),
        "ish_vt_image": ([pointer, ctypes.c_uint32, *output], ctypes.c_int),
        "ish_vt_image_exists": (
            [pointer, ctypes.c_uint32, ctypes.POINTER(ctypes.c_bool)],
            ctypes.c_int,
        ),
        "ish_vt_tick": (
            [pointer, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)],
            ctypes.c_int,
        ),
        "ish_vt_focus": ([pointer, unsigned, *output], ctypes.c_int),
        "ish_vt_png_decoder": ([_DecodePNG], ctypes.c_int),
        "ish_vt_image_alloc": ([pointer, size], pointer),
        "ish_vt_key": (
            [
                pointer,
                ctypes.c_char_p,
                ctypes.c_char_p,
                size,
                ctypes.c_uint32,
                unsigned,
                unsigned,
                *output,
            ],
            ctypes.c_int,
        ),
        "ish_vt_paste": ([pointer, ctypes.c_char_p, size, *output], ctypes.c_int),
        "ish_vt_state": ([pointer, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
        "ish_vt_scrollback_limit": (
            [pointer, ctypes.c_uint64, ctypes.c_uint64],
            ctypes.c_int,
        ),
        "ish_vt_scroll": ([pointer, unsigned, ctypes.c_int64], ctypes.c_int),
        "ish_vt_select_output": (
            [pointer, unsigned, unsigned, ctypes.POINTER(ctypes.c_bool)],
            ctypes.c_int,
        ),
        "ish_vt_select_last_output": (
            [pointer, ctypes.POINTER(ctypes.c_bool)],
            ctypes.c_int,
        ),
        "ish_vt_mouse": (
            [
                pointer,
                unsigned,
                unsigned,
                ctypes.c_int32,
                ctypes.c_int32,
                unsigned,
                unsigned,
                ctypes.c_int32,
                ctypes.c_int32,
                *output,
            ],
            ctypes.c_int,
        ),
        "ish_vt_begin_selection": ([pointer, unsigned, unsigned], ctypes.c_int),
        "ish_vt_update_selection": ([pointer, unsigned, unsigned], ctypes.c_int),
        "ish_vt_clear_selection": ([pointer], ctypes.c_int),
        "ish_vt_selection_text": ([pointer, *output], ctypes.c_int),
        "ish_vt_selection": (
            [
                pointer,
                unsigned,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_uint64,
                ctypes.c_bool,
                ctypes.c_uint64,
                ctypes.c_double,
                ctypes.c_bool,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int,
        ),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
    _install_png_decoder(library)
    _check(library.ish_vt_log(capture))
    return library


class Terminal:
    """Own one libghostty-vt terminal; close explicitly when its PTY closes."""

    DEFAULT_IMAGE_BYTES = 16 * 1024 * 1024
    IMAGE_PLACEHOLDER = "\U0010eeee"
    MOUSE_ACTIONS = {"press": 0, "release": 1, "move": 2}
    MOUSE_BUTTONS = {
        None: 0,
        "left": 1,
        "middle": 2,
        "right": 3,
        "wheelup": 4,
        "wheeldown": 5,
        "wheelright": 7,
        "wheelleft": 6,
    }
    HEADER = struct.Struct("<7I2Q5IQ4I")
    # The native snapshot carries color presence separately from RGB values.
    CELL = struct.Struct("<HB6B4I3BI")
    PLACEMENT = struct.Struct("<IIQii6Iii")
    IMAGE_HEADER = struct.Struct("<IIQ")
    IMAGE_CONTEXT = struct.Struct("<12I16s")
    EFFECT_HEADER = struct.Struct("<II")
    EFFECT_KINDS = {1: "bell", 2: "title", 3: "pwd", 4: "notification"}
    CURSOR_SHAPES = ("bar", "block", "underline", "block_hollow")
    KEY_ACTIONS = {"release": 0, "press": 1, "repeat": 2}
    SELECTION_ACTIONS = {
        "press": 0,
        "release": 1,
        "drag": 2,
        "autoscroll": 3,
        "reset": 4,
    }
    SELECTION_BEHAVIORS = ("cell", "word", "line", "output")
    SELECTION_AUTOSCROLL = (0, -1, 1)
    REPEAT_INTERVAL_NS = 500_000_000
    REPEAT_DISTANCE = 1.0
    COUNT = struct.Struct("<I")

    def __init__(
        self,
        cols: int,
        rows: int,
        reply: Callable[[bytes], None],
        *,
        scrollback_lines: int = DEFAULT_SCROLLBACK_LINES,
        scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES,
    ):
        """Create a grid and a receiver for terminal-generated PTY responses."""
        _dimensions(cols, rows)
        self._library = _load()
        self.engine = self._library._ish_engine
        self._handle = self._library.ish_vt_new(cols, rows)
        if not self._handle:
            raise TerminalError(i18n.get(Message.VT_ALLOCATION_ERROR))
        self._reply = reply
        self.cols = cols
        self.rows = rows
        self._rendered_columns = cols
        self._rendered_rows: list[_RenderedRow] = []
        self.cell_width = self.cell_height = 0
        self.graphics_enabled = False
        self._inputs = None
        try:
            name, version = TERM.encode("ascii"), IDENTITY.encode("ascii")
            _check(
                self._library.ish_vt_identity(
                    self._handle, name, len(name), version, len(version)
                )
            )
            self._inputs = InputPipeline(self, self.engine, reply=self._reply)
            self.set_scrollback_limit(scrollback_lines, scrollback_bytes)
        except Exception:
            self.close()
            raise

    def is_unknown_osc(self, body: bytes) -> bool:
        """Ask the pinned parser whether an OSC number has no native owner."""
        self._open()
        result = self._library.ish_vt_osc_unknown(body, len(body))
        if result < 0:
            _check(result)
        return result == 1

    def set_default_cursor(self, shape: str, *, blink: bool) -> None:
        """Set the native DECSCUSR reset defaults without changing the live cursor."""
        if shape not in self.CURSOR_SHAPES or type(blink) is not bool:
            raise ValueError(i18n.get(Message.VT_CURSOR_DEFAULT))
        _check(
            self._library.ish_vt_default_cursor(
                self._open(), self.CURSOR_SHAPES.index(shape), blink
            )
        )

    def _open(self) -> int:
        if not self._handle:
            raise TerminalError(i18n.get(Message.VT_CLOSED))
        return self._handle

    def _bytes(self, name: str, *arguments: object) -> bytes:
        pointer = ctypes.c_void_p()
        size = ctypes.c_size_t()
        _check(
            getattr(self._library, name)(
                self._open(), *arguments, ctypes.byref(pointer), ctypes.byref(size)
            )
        )
        if size.value > MAX_BUFFER or (size.value and not pointer.value):
            raise TerminalError(i18n.get(Message.VT_NATIVE_BUFFER))
        return ctypes.string_at(pointer, size.value) if size.value else b""

    def _deliver_replies(self) -> None:
        data = self._bytes("ish_vt_replies")
        if data:
            self._reply(data)

    def feed(self, data: bytes) -> None:
        """Frame bounded adapter controls and feed every other byte to native VT."""
        self._open()
        self._inputs.feed(data)

    def feed_raw(self, data: bytes) -> None:
        """Feed native bytes directly; adapters call this only at full boundaries."""
        _check(self._library.ish_vt_feed(self._open(), data, len(data)))
        self._deliver_replies()

    def register_protocol(self, name, *, kind, prefixes, factory):
        """Register a session-owned extension before the first input byte."""
        self._open()
        self._inputs.register(name, kind=kind, prefixes=prefixes, factory=factory)

    @property
    def protocol_errors(self):
        """Read bounded extension diagnostics without exposing handlers."""
        return self._inputs.protocol_errors

    @property
    def framing_drops(self):
        """Read the count of rejected oversized records, never their contents."""
        return self._inputs.framing_drops

    @property
    def protocol_failures(self):
        """Read protocol identifiers and counts without recording child payloads."""
        return self._inputs.protocol_failures

    @property
    def processing_error(self):
        """Read the engine's sticky semantic-failure flag."""
        result = self._library.ish_vt_processing_error(self._open())
        if result < 0:
            _check(result)
        return bool(result)

    def set_search(self, text: str) -> None:
        """Search native active screen and history without creating another grid."""
        data = text.encode("utf-8")
        _check(self._library.ish_vt_search_set(self._open(), data, len(data)))

    def search_step(self, budget: int = 8) -> tuple[bool, int]:
        """Make bounded progress; the caller yields between incomplete steps."""
        if type(budget) is not int or not 1 <= budget <= 64:
            raise ValueError(i18n.get(Message.VT_SEARCH_BUDGET))
        status, count = ctypes.c_uint(), ctypes.c_uint64()
        _check(
            self._library.ish_vt_search_step(
                self._open(), budget, ctypes.byref(status), ctypes.byref(count)
            )
        )
        return status.value == 2, count.value

    def select_match(self, *, previous: bool = False) -> int | None:
        """Select a native match and scroll it into view; next moves into history."""
        index = ctypes.c_uint64()
        _check(
            self._library.ish_vt_search_select(
                self._open(), previous, ctypes.byref(index)
            )
        )
        return None if index.value == (1 << 64) - 1 else index.value

    @property
    def input_routes(self):
        """Expose implementation choices without leaking provider objects."""
        return self._inputs.routes

    @property
    def image_errors(self):
        """Read supplemental image diagnostics through the common boundary."""
        return self._inputs.image_errors

    @property
    def image_failure_count(self):
        """Read rejected shim-image counts independently of retained details."""
        return self._inputs.image_failure_count

    @property
    def clipboard(self):
        """Keep the existing pre-input clipboard attachment API."""
        return self._inputs.clipboard

    @clipboard.setter
    def clipboard(self, broker):
        self._inputs.clipboard = broker

    def set_notifications(self, broker, *, before):
        """Attach an optional policy sink before any child input is accepted."""
        self._inputs.set_notifications(broker, before=before)

    def resize(
        self,
        cols: int,
        rows: int,
        cell_width: int | None = None,
        cell_height: int | None = None,
    ) -> None:
        """Resize the VT; the session separately resizes the child PTY."""
        _dimensions(cols, rows)
        cell_width = self.cell_width if cell_width is None else cell_width
        cell_height = self.cell_height if cell_height is None else cell_height
        if (
            not 0 <= cell_width <= 65_535
            or not 0 <= cell_height <= 65_535
            or bool(cell_width) != bool(cell_height)
        ):
            raise ValueError(i18n.get(Message.VT_CELL_DIMENSIONS))
        _check(
            self._library.ish_vt_resize(
                self._open(), cols, rows, cell_width, cell_height
            )
        )
        self.cols, self.rows = cols, rows
        self.cell_width, self.cell_height = cell_width, cell_height
        self._deliver_replies()

    def frame(self) -> Frame:
        """Copy the visible grid once and coalesce equal styles for PTK."""
        data = self._bytes("ish_vt_frame")
        (
            cols,
            rows,
            cursor_x,
            cursor_y,
            visible,
            alternate,
            active,
            history,
            offset_top,
            shape,
            blinking,
            cell_width,
            cell_height,
            image_count,
            image_generation,
            cursor_color_present,
            cursor_red,
            cursor_green,
            cursor_blue,
        ) = self.HEADER.unpack_from(data)
        _dimensions(cols, rows)
        offset = self.HEADER.size
        lines = []
        cell_rows = []
        previous_rows = self._rendered_rows if cols == self._rendered_columns else ()
        rendered_rows = []
        rows_by_record = None
        for row in range(rows):
            # The count and every variable-length field are part of the record.
            # An exact match therefore ends at the next native row boundary.
            previous = None
            if row < len(previous_rows):
                candidate = previous_rows[row]
                if data.startswith(candidate.record, offset):
                    previous = candidate
                else:
                    # Scrolling can move an unchanged record to a different row.
                    # Try one record length at this position, then decode normally
                    # on a miss. The length alone never permits reuse.
                    if rows_by_record is None:
                        rows_by_record = {item.record: item for item in previous_rows}
                    previous = rows_by_record.get(
                        data[offset : offset + len(candidate.record)]
                    )
            if previous is not None:
                offset += len(previous.record)
                lines.append(list(previous.runs))
                cell_rows.append(previous.cells)
                rendered_rows.append(previous)
                continue
            start = offset
            (count,) = self.COUNT.unpack_from(data, offset)
            offset += self.COUNT.size
            if count > cols:
                raise TerminalError(i18n.get(Message.VT_NATIVE_ROW))
            runs: list[tuple[str, str]] = []
            fragments: list[str] = []
            previous_style = ""
            native_cells = []
            for _ in range(count):
                (
                    flags,
                    colors,
                    red,
                    green,
                    blue,
                    bg_red,
                    bg_green,
                    bg_blue,
                    length,
                    x,
                    width,
                    underline_present,
                    ul_red,
                    ul_green,
                    ul_blue,
                    link_length,
                ) = self.CELL.unpack_from(data, offset)
                offset += self.CELL.size
                end = offset + length
                if (
                    end + link_length > len(data)
                    or x + width > cols
                    or width not in (1, 2)
                ):
                    raise TerminalError(i18n.get(Message.VT_NATIVE_CELL))
                text = data[offset:end].decode("utf-8")
                hyperlink = (
                    data[end : end + link_length].decode("utf-8", errors="replace")
                    if link_length
                    else None
                )
                offset = end + link_length
                style = _style(
                    flags, colors, red, green, blue, bg_red, bg_green, bg_blue
                )
                native_cells.append(
                    Cell(
                        x,
                        text,
                        width,
                        style,
                        flags,
                        (red, green, blue) if colors & COLOR_FG else None,
                        (bg_red, bg_green, bg_blue) if colors & COLOR_BG else None,
                        (flags >> 8) & 7,
                        (ul_red, ul_green, ul_blue) if underline_present else None,
                        hyperlink,
                    )
                )
                if fragments and previous_style != style:
                    runs.append((previous_style, "".join(fragments)))
                    fragments = []
                previous_style = style
                fragments.append(text)
            if fragments:
                runs.append((previous_style, "".join(fragments)))
            rendered = _RenderedRow(
                data[start:offset], tuple(runs), tuple(native_cells)
            )
            lines.append(runs)
            cell_rows.append(rendered.cells)
            rendered_rows.append(rendered)
        images = []
        if image_count > (len(data) - offset) // self.PLACEMENT.size:
            raise TerminalError(i18n.get(Message.VT_NATIVE_IMAGES))
        for _ in range(image_count):
            images.append(Placement(*self.PLACEMENT.unpack_from(data, offset)))
            offset += self.PLACEMENT.size
        if offset != len(data):
            raise TerminalError(i18n.get(Message.VT_NATIVE_FRAME))
        self._rendered_columns = cols
        self._rendered_rows = rendered_rows
        return Frame(
            lines,
            cursor_x,
            cursor_y,
            bool(visible),
            bool(alternate),
            cols,
            rows,
            bool(active),
            history,
            offset_top,
            tuple(cell_rows),
            self.CURSOR_SHAPES[shape] if shape < len(self.CURSOR_SHAPES) else "block",
            bool(blinking),
            cell_width,
            cell_height,
            tuple(images),
            image_generation,
            (cursor_red, cursor_green, cursor_blue) if cursor_color_present else None,
        )

    def set_appearance(
        self,
        appearance=None,
        *,
        foreground=None,
        background=None,
        cursor=None,
        palette=None,
        scheme=None,
    ) -> None:
        """Apply observed host defaults without replacing the child's OSC overrides.

        Unknown colors stay unknown; unknown palette entries use the native
        baseline. Rendering still inherits host defaults unless the child has
        explicitly overridden them, including an override of the same RGB.
        """
        if appearance is not None:
            foreground, background, cursor, palette, scheme = (
                getattr(appearance, name)
                for name in ("foreground", "background", "cursor", "palette", "scheme")
            )
        if scheme not in (None, "light", "dark"):
            raise ValueError(i18n.get(Message.VT_COLOR_SCHEME))
        palette = (None,) * 256 if palette is None else tuple(palette)
        if len(palette) != 256:
            raise ValueError(i18n.get(Message.VT_PALETTE_SIZE))
        encoded = bytearray()
        for color in (foreground, background, cursor, *palette):
            if color is None:
                encoded.extend(b"\0\0\0\0")
                continue
            if len(color) != 3 or not all(
                isinstance(channel, int) and 0 <= channel <= 255 for channel in color
            ):
                raise ValueError(i18n.get(Message.VT_RGB_COLOR))
            encoded.extend((1, *color))
        data = bytes(encoded)
        _check(
            self._library.ish_vt_appearance(
                self._open(), data, len(data), {None: -1, "light": 0, "dark": 1}[scheme]
            )
        )
        self._deliver_replies()

    def set_graphics(self, enabled: bool, max_bytes: int = DEFAULT_IMAGE_BYTES) -> None:
        """Enable only after the outer renderer confirms a supported image path."""
        if not 0 < max_bytes <= MAX_BUFFER:
            raise ValueError(i18n.get(Message.VT_IMAGE_LIMIT))
        if enabled:
            if not self.cell_width or not self.cell_height:
                raise ValueError(i18n.get(Message.VT_IMAGE_GEOMETRY))
            try:
                from PIL import Image as _PillowImage  # noqa: F401
            except ImportError as error:
                raise TerminalError(i18n.get(Message.VT_IMAGE_DECODER)) from error
        _check(self._library.ish_vt_graphics(self._open(), max_bytes if enabled else 0))
        self.graphics_enabled = bool(enabled)
        self._inputs.graphics_changed(self.graphics_enabled)
        if not enabled:
            self._inputs.cancel_images()

    def set_sixel_supported(self, enabled: bool) -> None:
        """Expose the selected input adapter through native device attributes."""
        _check(self._library.ish_vt_sixel_supported(self._open(), bool(enabled)))

    def image_context(self) -> ImageContext:
        """Query live native state even while presentation is held or scrolled."""
        return ImageContext(
            *self.IMAGE_CONTEXT.unpack(self._bytes("ish_vt_image_context"))
        )

    def unused_image_id(self) -> int:
        """Select an unused image identity without replacing a child image."""
        value = ctypes.c_uint32()
        _check(self._library.ish_vt_unused_image_id(self._open(), ctypes.byref(value)))
        return value.value

    def image(self, image_id: int) -> Image | None:
        """Copy presentation pixels; synchronized output retains its held view."""
        if not 0 <= image_id < 2**32:
            raise ValueError(i18n.get(Message.VT_IMAGE_ID))
        data = self._bytes("ish_vt_image", image_id)
        if not data:
            return None
        width, height, generation = self.IMAGE_HEADER.unpack_from(data)
        if len(data) != self.IMAGE_HEADER.size + width * height * 4:
            raise TerminalError(i18n.get(Message.VT_NATIVE_IMAGE))
        return Image(
            image_id, generation, width, height, data[self.IMAGE_HEADER.size :]
        )

    def image_exists(self, image_id: int) -> bool:
        """Validate live image storage independently of synchronized presentation."""
        if not 0 <= image_id < 2**32:
            raise ValueError(i18n.get(Message.VT_IMAGE_ID))
        exists = ctypes.c_bool()
        _check(
            self._library.ish_vt_image_exists(
                self._open(), image_id, ctypes.byref(exists)
            )
        )
        return exists.value

    def tick(self, now_ms: int) -> int | None:
        """Advance native image playback once and return its next deadline."""
        if not 0 <= now_ms < 2**64:
            raise ValueError(i18n.get(Message.VT_ANIMATION_TIME))
        deadline = ctypes.c_uint64()
        _check(self._library.ish_vt_tick(self._open(), now_ms, ctypes.byref(deadline)))
        return None if deadline.value == 2**64 - 1 else deadline.value

    def encode_focus(self, focused: bool) -> bytes:
        """Report actual frontend focus only when the child requested it."""
        return self._bytes("ish_vt_focus", bool(focused))

    def drain_events(self) -> tuple[Effect, ...]:
        """Consume native side effects once, without writing the outer terminal."""
        data = self._bytes("ish_vt_effects")
        events = []
        offset = 0
        while offset < len(data):
            kind, length = self.EFFECT_HEADER.unpack_from(data, offset)
            offset += self.EFFECT_HEADER.size
            if offset + length > len(data) or kind not in self.EFFECT_KINDS:
                raise TerminalError(i18n.get(Message.VT_NATIVE_EFFECT))
            events.append(
                Effect(self.EFFECT_KINDS[kind], data[offset : offset + length])
            )
            offset += length
        return tuple(events)

    def _state(self) -> ctypes.Array:
        values = (ctypes.c_uint64 * 8)()
        _check(self._library.ish_vt_state(self._open(), values))
        return values

    @property
    def viewport_active(self) -> bool:
        """Whether the native viewport is following the active terminal area."""
        return bool(self._state()[0])

    @property
    def alternate(self) -> bool:
        """Whether the native alternate screen is active."""
        return bool(self._state()[1])

    @property
    def alternate_scroll(self) -> bool:
        """Whether the child enabled alternate-scroll mode 1007."""
        return bool(self._state()[2])

    @property
    def mouse_reporting(self) -> bool:
        """Whether the child enabled a native mouse tracking mode."""
        return bool(self._state()[3])

    @property
    def synchronized_output(self) -> bool:
        """Whether the application currently holds its rendering transaction."""
        return bool(self._state()[6])

    @property
    def render_hold_serial(self) -> int:
        """Identify a native hold start even when end/start share one PTY read."""
        return int(self._state()[7])

    def release_render_hold(self) -> None:
        """End a stalled presentation hold without changing shell input state."""
        _check(self._library.ish_vt_release_hold(self._open()))

    def set_scrollback_limit(
        self, lines: int, max_bytes: int = DEFAULT_SCROLLBACK_BYTES
    ) -> None:
        """Set native approximate page-based limits; zero bytes erases history."""
        if not 0 <= lines < 2**63 or not 0 <= max_bytes < 2**63:
            raise ValueError(i18n.get(Message.VT_HISTORY_LIMIT))
        _check(self._library.ish_vt_scrollback_limit(self._open(), lines, max_bytes))

    def scroll(self, rows: int) -> None:
        """Move the native viewport by physical rows; negative values move up."""
        if not -(2**63) <= rows < 2**63:
            raise ValueError(i18n.get(Message.VT_SCROLL_DELTA))
        _check(self._library.ish_vt_scroll(self._open(), 2, rows))

    def scroll_top(self) -> None:
        """Show the oldest retained native history."""
        _check(self._library.ish_vt_scroll(self._open(), 0, 0))

    def scroll_prompts(self, count: int) -> None:
        """Navigate native semantic markers; absent markers are a no-op."""
        if not -(2**63) <= count < 2**63:
            raise ValueError(i18n.get(Message.VT_SCROLL_DELTA))
        _check(self._library.ish_vt_scroll(self._open(), 3, count))

    def scroll_bottom(self) -> None:
        """Follow the active terminal area without modifying terminal content."""
        _check(self._library.ish_vt_scroll(self._open(), 1, 0))

    def encode_mouse(
        self,
        action: str,
        button: str | None,
        x: int,
        y: int,
        modifiers: int = 0,
        *,
        pixel_position: tuple[int, int] | None = None,
    ) -> bytes:
        """Encode zero-based cell events using current native tracking and format.

        Mode 1016 emits bytes only when the caller supplies actual host pixel
        coordinates and confirmed cell geometry. A cell centre is not evidence.
        """
        if action not in self.MOUSE_ACTIONS or button not in self.MOUSE_BUTTONS:
            raise ValueError(i18n.get(Message.VT_MOUSE_EVENT))
        if modifiers & ~(SHIFT | CTRL | ALT):
            raise ValueError(i18n.get(Message.VT_MOUSE_MODIFIERS))
        if not -(2**31) <= x < 2**31 or not -(2**31) <= y < 2**31:
            raise ValueError(i18n.get(Message.VT_MOUSE_COORDINATES))
        pixel_x, pixel_y = pixel_position if pixel_position is not None else (0, 0)
        if pixel_position is not None:
            if not self.cell_width or not self.cell_height:
                raise ValueError(i18n.get(Message.VT_MOUSE_GEOMETRY))
            if not -(2**31) <= pixel_x < 2**31 or not -(2**31) <= pixel_y < 2**31:
                raise ValueError(i18n.get(Message.VT_MOUSE_PIXELS))
        return self._bytes(
            "ish_vt_mouse",
            self.MOUSE_ACTIONS[action],
            self.MOUSE_BUTTONS[button],
            x,
            y,
            modifiers,
            pixel_position is not None,
            pixel_x,
            pixel_y,
        )

    def _selection_point(self, x: int, y: int) -> None:
        if not 0 <= x < self.cols or not 0 <= y < self.rows:
            raise ValueError(i18n.get(Message.VT_SELECTION_POINT))

    def selection(
        self,
        action: str,
        x: int = 0,
        y: int = 0,
        *,
        position=None,
        time_ns: int | None = None,
        repeat_interval_ns: int = REPEAT_INTERVAL_NS,
        repeat_distance: float = REPEAT_DISTANCE,
        rectangle: bool = False,
    ) -> SelectionState:
        """Apply a native pointer gesture in viewport cell coordinates.

        Position is the unclipped pointer in cell units; an omitted position
        uses the cell centre. Negative/outside drag coordinates request native
        autoscroll. A tick scrolls one row and updates selection atomically.
        Reset cancels the gesture while preserving the installed selection.
        """
        if action not in self.SELECTION_ACTIONS:
            raise ValueError(i18n.get(Message.VT_SELECTION_ACTION))
        if not all(
            isinstance(value, int) and -(2**31) <= value < 2**31 for value in (x, y)
        ):
            raise ValueError(i18n.get(Message.VT_SELECTION_COORDINATES))
        position = (x + 0.5, y + 0.5) if position is None else position
        if len(position) != 2 or not all(math.isfinite(value) for value in position):
            raise ValueError(i18n.get(Message.VT_SELECTION_POINTER))
        if time_ns is not None and not 0 <= time_ns < 2**64:
            raise ValueError(i18n.get(Message.VT_SELECTION_TIME))
        if (
            not 0 <= repeat_interval_ns < 2**64
            or not math.isfinite(repeat_distance)
            or repeat_distance < 0
        ):
            raise ValueError(i18n.get(Message.VT_REPEAT_CLICK))
        result = (ctypes.c_uint32 * 4)()
        _check(
            self._library.ish_vt_selection(
                self._open(),
                self.SELECTION_ACTIONS[action],
                x,
                y,
                *position,
                time_ns or 0,
                time_ns is not None,
                repeat_interval_ns,
                repeat_distance,
                rectangle,
                result,
            )
        )
        return SelectionState(
            result[0],
            bool(result[1]),
            self.SELECTION_AUTOSCROLL[result[2]],
            self.SELECTION_BEHAVIORS[result[3]],
        )

    def begin_selection(self, x: int, y: int) -> None:
        """Track a native viewport cell as an anchor; no selection exists yet."""
        self._selection_point(x, y)
        _check(self._library.ish_vt_begin_selection(self._open(), x, y))

    def update_selection(self, x: int, y: int) -> None:
        """Select inclusively from the live native anchor to a viewport cell."""
        self._selection_point(x, y)
        _check(self._library.ish_vt_update_selection(self._open(), x, y))

    def clear_selection(self) -> None:
        """Clear the active native selection and release its tracked anchor."""
        _check(self._library.ish_vt_clear_selection(self._open()))

    def select_output(self, x: int, y: int) -> bool:
        """Select native command output containing one viewport cell."""
        self._selection_point(x, y)
        found = ctypes.c_bool()
        _check(
            self._library.ish_vt_select_output(self._open(), x, y, ctypes.byref(found))
        )
        return found.value

    def select_last_output(self) -> bool:
        """Select output at the last native OSC 133 C anchor, if retained."""
        found = ctypes.c_bool()
        _check(
            self._library.ish_vt_select_last_output(self._open(), ctypes.byref(found))
        )
        return found.value

    def selection_text(self) -> str:
        """Keep native selection formatting while hiding image cell metadata."""
        text = self._bytes("ish_vt_selection_text").decode("utf-8")
        if self.IMAGE_PLACEHOLDER not in text:
            return text
        # Match the snapshot's blank image cells without altering native ranges,
        # line wrapping or unrelated combining characters in selected text.
        return "".join(
            " " if grapheme.startswith(self.IMAGE_PLACEHOLDER) else grapheme
            for grapheme in iter_graphemes(text)
        )

    @property
    def keyboard_flags(self) -> int:
        """Read active native flags, including per-screen keyboard push/pop state."""
        flags = ctypes.c_uint32()
        _check(self._library.ish_vt_keyboard_flags(self._open(), ctypes.byref(flags)))
        return flags.value

    def encode_key(
        self,
        key: str,
        modifiers: int = 0,
        *,
        action: str = "press",
        input_text: str | None = None,
    ) -> bytes:
        """Encode a named key or text with the terminal's current input modes.

        A terminal frontend cannot recover physical keys from arbitrary text.
        Multiple text characters are therefore plain UTF-8, without modifiers.
        C0 bytes already decoded by PTK retain their original meaning.
        """
        self._open()
        if action not in self.KEY_ACTIONS:
            raise ValueError(i18n.get(Message.VT_KEY_ACTION))
        native_action = self.KEY_ACTIONS[action]
        if modifiers & ~(SHIFT | CTRL | ALT):
            raise ValueError(i18n.get(Message.VT_KEY_MODIFIERS))
        text = (input_text if input_text is not None and len(key) == 1 else key).encode(
            "utf-8"
        )
        if len(text) > MAX_BUFFER:
            raise ValueError(i18n.get(Message.VT_INPUT_LIMIT))
        if key in SPECIAL_KEYS:
            encoded = self._bytes(
                "ish_vt_key", text, b"", 0, 0, modifiers, native_action
            )
            if key == "f3" and self.keyboard_flags == 0:
                return LEGACY_F3.get(encoded, encoded)
        elif len(key) != 1:
            if modifiers or action != "press":
                raise ValueError(i18n.get(Message.VT_KEY_COUNT))
            return text
        else:
            point = ord(key)
            if point < 32 or point == 127:
                if action == "release":
                    return b""
                return (b"\x1b" if modifiers & ALT else b"") + text
            encoded = self._bytes(
                "ish_vt_key", b"", text, len(text), point, modifiers, native_action
            )
        if (
            modifiers
            and (
                encoded.startswith(b"\x1b[27;")
                and encoded.endswith(b"~")
                or encoded.startswith(b"\x1b[")
                and encoded.endswith(b"u")
            )
            and not self._inputs.modified_keys_requested
            and not self.keyboard_flags
        ):
            return legacy_control(key, alt=modifiers & ALT, ctrl=modifiers & CTRL)
        return encoded

    def encode_paste(self, text: str | bytes) -> bytes:
        """Encode an explicit user paste, honoring native bracketed-paste mode."""
        data = text.encode("utf-8") if isinstance(text, str) else text
        if len(data) > MAX_BUFFER:
            raise ValueError(i18n.get(Message.VT_PASTE_LIMIT))
        return self._bytes("ish_vt_paste", data, len(data))

    def close(self) -> None:
        """Release native state; repeated closes are harmless."""
        if self._handle:
            try:
                if self._inputs is not None:
                    self._inputs.close()
            finally:
                self._library.ish_vt_free(self._handle)
                self._handle = None
                self._rendered_rows.clear()

    def __enter__(self) -> Terminal:
        """Keep the terminal alive for a with block."""
        self._open()
        return self

    def __exit__(self, *_: object) -> None:
        """Release native state on leaving a with block."""
        self.close()
