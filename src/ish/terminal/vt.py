"""A small libghostty-vt binding for the separate full-screen prototype.

All methods belong to one event-loop thread. The native bridge owns VT state;
Python receives a bounded bulk snapshot, never one FFI call per terminal cell.
This text renderer deliberately does not expose images, links or clipboard IO.
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SHIFT = 1
CTRL = 2
ALT = 4
MAX_CELLS = 262_144
MAX_BUFFER = 32 * 1024 * 1024
LIBRARY_ENV = "ISH_GHOSTTY_LIB"
DEFAULT_LIBRARY = (
    Path(__file__).resolve().parents[3] / ".cache/ghostty-vt/install/lib/libish_vt.so"
)
HEADER = struct.Struct("<6I")
# The native snapshot carries color presence separately from RGB values.
CELL = struct.Struct("<HB6BI")
COLOR_FG = 1
COLOR_BG = 2
COUNT = struct.Struct("<I")
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
        *(f"f{number}" for number in range(1, 13)),
    }
)


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


class TerminalError(RuntimeError):
    """The native VT library could not complete an operation."""


def _dimensions(cols: int, rows: int) -> None:
    if not 0 < cols <= 65_535 or not 0 < rows <= 65_535 or cols * rows > MAX_CELLS:
        raise ValueError(f"Terminal dimensions exceed {MAX_CELLS} cells: {cols}x{rows}")


def _check(status: int) -> None:
    if status:
        raise TerminalError(f"libghostty-vt bridge failed (status {status})")


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


def _load() -> ctypes.CDLL:
    path = Path(os.environ.get(LIBRARY_ENV, DEFAULT_LIBRARY))
    try:
        library = ctypes.CDLL(str(path))
    except OSError as error:
        raise TerminalError(
            f"Cannot load {path}. Run tools/build_ghostty_vt.py in Linux/WSL, "
            f"or set {LIBRARY_ENV} to the built libish_vt.so."
        ) from error
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    unsigned = ctypes.c_uint
    output = [ctypes.POINTER(pointer), ctypes.POINTER(size)]
    signatures = {
        "ish_vt_new": ([unsigned, unsigned], pointer),
        "ish_vt_free": ([pointer], None),
        "ish_vt_feed": ([pointer, ctypes.c_char_p, size], ctypes.c_int),
        "ish_vt_resize": ([pointer, unsigned, unsigned], ctypes.c_int),
        "ish_vt_replies": ([pointer, *output], ctypes.c_int),
        "ish_vt_frame": ([pointer, *output], ctypes.c_int),
        "ish_vt_key": (
            [
                pointer,
                ctypes.c_char_p,
                ctypes.c_char_p,
                size,
                ctypes.c_uint32,
                unsigned,
                *output,
            ],
            ctypes.c_int,
        ),
        "ish_vt_paste": ([pointer, ctypes.c_char_p, size, *output], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
    return library


class Terminal:
    """Own one libghostty-vt terminal; close explicitly when its PTY closes."""

    def __init__(self, cols: int, rows: int, reply: Callable[[bytes], None]):
        """Create a grid and a receiver for terminal-generated PTY responses."""
        _dimensions(cols, rows)
        self._library = _load()
        self._handle = self._library.ish_vt_new(cols, rows)
        if not self._handle:
            raise TerminalError("Cannot allocate libghostty-vt terminal")
        self._reply = reply
        self.cols = cols
        self.rows = rows

    def _open(self) -> int:
        if not self._handle:
            raise TerminalError("Terminal is closed")
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
            raise TerminalError("Invalid native buffer")
        return ctypes.string_at(pointer, size.value) if size.value else b""

    def _deliver_replies(self) -> None:
        data = self._bytes("ish_vt_replies")
        if data:
            self._reply(data)

    def feed(self, data: bytes) -> None:
        """Parse arbitrary PTY output, then enqueue any generated PTY replies."""
        _check(self._library.ish_vt_feed(self._open(), data, len(data)))
        self._deliver_replies()

    def resize(self, cols: int, rows: int) -> None:
        """Resize the VT; the session separately resizes the child PTY."""
        _dimensions(cols, rows)
        _check(self._library.ish_vt_resize(self._open(), cols, rows))
        self.cols, self.rows = cols, rows
        self._deliver_replies()

    def frame(self) -> Frame:
        """Copy the visible grid once and coalesce equal styles for PTK."""
        data = self._bytes("ish_vt_frame")
        cols, rows, cursor_x, cursor_y, visible, alternate = HEADER.unpack_from(data)
        _dimensions(cols, rows)
        offset = HEADER.size
        lines = []
        for _ in range(rows):
            (count,) = COUNT.unpack_from(data, offset)
            offset += COUNT.size
            if count > cols:
                raise TerminalError("Invalid native row size")
            runs: list[tuple[str, str]] = []
            fragments: list[str] = []
            previous_style = ""
            for _ in range(count):
                *attributes, length = CELL.unpack_from(data, offset)
                offset += CELL.size
                end = offset + length
                if end > len(data):
                    raise TerminalError("Truncated native cell")
                text = data[offset:end].decode("utf-8")
                offset = end
                style = _style(*attributes)
                if fragments and previous_style != style:
                    runs.append((previous_style, "".join(fragments)))
                    fragments = []
                previous_style = style
                fragments.append(text)
            if fragments:
                runs.append((previous_style, "".join(fragments)))
            lines.append(runs)
        if offset != len(data):
            raise TerminalError("Unexpected native frame data")
        return Frame(
            lines, cursor_x, cursor_y, bool(visible), bool(alternate), cols, rows
        )

    def encode_key(self, key: str, modifiers: int = 0) -> bytes:
        """Encode a named key or text with the terminal's current input modes.

        A terminal frontend cannot recover physical keys from arbitrary text.
        Multiple text characters are therefore plain UTF-8, without modifiers.
        C0 bytes already decoded by PTK retain their original meaning.
        """
        self._open()
        if modifiers & ~(SHIFT | CTRL | ALT):
            raise ValueError("Unsupported key modifiers")
        text = key.encode("utf-8")
        if len(text) > MAX_BUFFER:
            raise ValueError("Input is too large")
        if key in SPECIAL_KEYS:
            return self._bytes("ish_vt_key", text, b"", 0, 0, modifiers)
        if len(key) != 1:
            if modifiers:
                raise ValueError("Modifiers require one key")
            return text
        point = ord(key)
        if point < 32 or point == 127:
            return (b"\x1b" if modifiers & ALT else b"") + text
        return self._bytes("ish_vt_key", b"", text, len(text), point, modifiers)

    def encode_paste(self, text: str | bytes) -> bytes:
        """Encode an explicit user paste, honoring native bracketed-paste mode."""
        data = text.encode("utf-8") if isinstance(text, str) else text
        if len(data) > MAX_BUFFER:
            raise ValueError("Paste is too large")
        return self._bytes("ish_vt_paste", data, len(data))

    def close(self) -> None:
        """Release native state; repeated closes are harmless."""
        if self._handle:
            self._library.ish_vt_free(self._handle)
            self._handle = None

    def __enter__(self) -> Terminal:
        """Keep the terminal alive for a with block."""
        self._open()
        return self

    def __exit__(self, *_: object) -> None:
        """Release native state on leaving a with block."""
        self.close()
