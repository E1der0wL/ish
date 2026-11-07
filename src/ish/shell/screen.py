"""Mirror the text cells written by an interactive PTY without answering the terminal."""

from __future__ import annotations

import codecs
import re
import unicodedata
from copy import copy
from dataclasses import dataclass
from threading import Lock

from pyte import Screen, Stream, charsets, modes
from pyte.screens import Char
from wcwidth import wcwidth

from .constants import ALT_CLEAR_ON_LEAVE, ALT_SAVE_CURSOR, ALTERNATE_SCREENS, CSI, ESC

_MAX_CELLS = 256 * 1024
_MAX_CONTROL = 256
_MAX_CELL_CHARS = 32
_ESC = ESC.decode("ascii")
_CSI = CSI.decode("ascii")
_STRING_STARTS = "]PX^_"
_C1_STRINGS = {"\x90": "P", "\x98": "X", "\x9d": "]", "\x9e": "^", "\x9f": "_"}
_SPECIAL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CSI_END = re.compile(r"[@-~]|[\x18\x1a\x1b]")
_ESC_END = re.compile(r"[0-~]|[\x18\x1a\x1b]")
_STRING_END = re.compile(r"\x1b\\|[\x18\x1a\x9c]")
_OSC_END = re.compile(r"\x1b\\|[\x07\x18\x1a\x9c]")
_CSI_ARGS = re.compile(r"(\??)([0-9;]*)([@-~])")
_CHARSETS = re.compile(r"[()][0-~]")
_SAVE_CURSOR_MODE = 1048
_BULK_MIN = 8
_TAB_WIDTH = 8
_ASCII_CELLS = {chr(code): Char(data=chr(code)) for code in range(32, 127)}
_SCREEN_MODES = {6, 7, 25}  # Origin, autowrap and cursor visibility.
_ANSI_MODES = {4, 20}  # Insert and automatic newline.


@dataclass(frozen=True, slots=True)
class ScreenSnapshot:
    """Visible PTY text after the last processed write, with a zero-based cell cursor.

    Each row includes trailing spaces. Wide glyphs occupy two columns in one
    string; a Python character index need not be a terminal column.
    """

    lines: tuple[str, ...]
    rows: int
    columns: int
    cursor_row: int
    cursor_column: int
    alternate: bool


class _Grid(Screen):
    """Keep one saved cursor and bound the combining marks stored in a single cell."""

    def save_cursor(self):
        """Keep the single DECSC save slot for this buffer."""
        self.savepoints.clear()
        super().save_cursor()

    def restore_cursor(self):
        """DECRC may be repeated before the next save."""
        saved = self.savepoints[:]
        if saved:
            self.savepoints[0] = saved[0]._replace(cursor=copy(saved[0].cursor))
        super().restore_cursor()
        self.savepoints[:] = saved

    def resize_grid(self, rows: int, columns: int) -> None:
        """Preserve the app's saved cursor across pyte's internal save/restore."""
        if (self.lines, self.columns) == (rows, columns):
            return
        old_columns = self.columns
        saved = self.savepoints[:]
        self.resize(rows, columns)
        self.savepoints[:] = saved
        self.tabstops = {stop for stop in self.tabstops if stop < columns}
        new_stops = max(
            _TAB_WIDTH, ((old_columns + _TAB_WIDTH - 1) // _TAB_WIDTH) * _TAB_WIDTH
        )
        self.tabstops.update(range(new_stops, columns, _TAB_WIDTH))
        self.ensure_hbounds()
        self.ensure_vbounds()

    def draw(self, text: str) -> None:
        """Append ordinary glyphs or bounded combining marks at their base cell."""
        if text.isascii():
            charset = self.g1_charset if self.charset else self.g0_charset
            if (
                len(text) >= _BULK_MIN
                and charset is charsets.LAT1_MAP
                and modes.IRM not in self.mode
                and text.isprintable()
            ):
                self._draw_ascii(text)
            else:
                super().draw(text)
            return
        for char in text:
            if unicodedata.combining(char):
                y, x = self.cursor.y, self.cursor.x - 1
                if x < 0:
                    y, x = y - 1, self.columns - 1
                if y < 0:
                    continue
                line = self.buffer[y]
                if not line[x].data and x:
                    x -= 1  # Combining marks attach to a wide glyph, not its stub.
                cell = line[x]
                if len(cell.data) < _MAX_CELL_CHARS:
                    line[x] = cell._replace(
                        data=unicodedata.normalize("NFC", cell.data + char)
                    )
            else:
                # An unsupported zero-width codepoint must not drop subsequent text.
                if (
                    wcwidth(char) == 2
                    and self.columns > 1
                    and self.cursor.x == self.columns - 1
                    and modes.DECAWM in self.mode
                ):
                    self.carriage_return()
                    self.linefeed()
                super().draw(char)

    def _draw_ascii(self, text: str) -> None:
        """Use immutable plain cells for adjacent single-width text, respecting wrap and margins."""
        start = 0
        while start < len(text):
            if self.cursor.x == self.columns:
                if modes.DECAWM not in self.mode:
                    self.buffer[self.cursor.y][self.columns - 1] = _ASCII_CELLS[
                        text[-1]
                    ]
                    break
                self.carriage_return()
                self.linefeed()
            end = min(len(text), start + self.columns - self.cursor.x)
            self.buffer[self.cursor.y].update(
                zip(
                    range(self.cursor.x, self.cursor.x + end - start),
                    map(_ASCII_CELLS.__getitem__, text[start:end]),
                    strict=True,
                )
            )
            self.cursor.x += end - start
            self.dirty.add(self.cursor.y)
            start = end

    def scroll_region(self, distance: int) -> None:
        """Move existing rows within the scroll margins, leaving the cursor in place."""
        top, bottom = self.margins or (0, self.lines - 1)
        rows = range(top, bottom + 1)
        for row in rows if distance > 0 else reversed(rows):
            source = row + distance
            if top <= source <= bottom and source in self.buffer:
                self.buffer[row] = self.buffer[source]
            else:
                self.buffer.pop(row, None)

    def text_rows(self) -> tuple[str, ...]:
        """Render sparse cells without indexing or populating an absent cell."""
        rows = []
        blank = " " * self.columns
        for y in range(self.lines):
            line = self.buffer.get(y)
            if not line:
                rows.append(blank)
                continue
            cells = [" "] * self.columns
            for x, cell in line.items():
                if not 0 <= x < self.columns or not cell.data:
                    continue
                if wcwidth(cell.data[0]) == 2:
                    stub = line.get(x + 1)
                    if x + 1 >= self.columns or stub is None or stub.data:
                        continue  # A clipped or partly overwritten wide glyph.
                    cells[x + 1] = ""
                cells[x] = cell.data
            rows.append("".join(cells))
        return tuple(rows)


class TerminalScreen:
    """Observe visible text in a primary and alternate grid, from a blank starting point."""

    def __init__(self, rows: int, columns: int, encoding: str = "utf-8"):
        """Start independent visible buffers and a decoder for interrupted characters."""
        self._check_size(rows, columns)
        self._lock = Lock()
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._grid = _Grid(columns, rows)
        self._other = _Grid(columns, rows)
        self._alternate = False
        self._stream = Stream(self._grid)
        self._pending = ""
        self._discard_until: re.Pattern[str] | None = None

    @staticmethod
    def _check_size(rows: int, columns: int) -> None:
        if rows < 1 or columns < 1 or rows * columns > _MAX_CELLS:
            raise ValueError(
                f"Screen dimensions must be positive and fit {_MAX_CELLS} cells"
            )

    def resize(self, rows: int, columns: int) -> None:
        """Resize both grids to the PTY geometry; reject oversized grids before changing either."""
        self._check_size(rows, columns)
        with self._lock:
            self._grid.resize_grid(rows, columns)
            self._other.resize_grid(rows, columns)

    def snapshot(self) -> ScreenSnapshot:
        """Copy the current text and cursor atomically, without querying the terminal."""
        with self._lock:
            grid = self._grid
            return ScreenSnapshot(
                grid.text_rows(),
                grid.lines,
                grid.columns,
                grid.cursor.y,
                min(grid.cursor.x, grid.columns - 1),
                self._alternate,
            )

    def _mode(self, private: bool, mode: int, enable: bool) -> None:
        if private and mode in ALTERNATE_SCREENS:
            if enable == self._alternate:
                return
            grid, other = self._grid, self._other
            if enable and mode == ALT_SAVE_CURSOR:
                grid.save_cursor()
                other.buffer.clear()
            elif not enable and mode == ALT_CLEAR_ON_LEAVE:
                grid.buffer.clear()
            # The visible buffer and DECSC slot change; terminal modes, margins
            # and the cursor itself continue unless this transition restores it.
            grid.buffer, other.buffer = other.buffer, grid.buffer
            grid.savepoints, other.savepoints = other.savepoints, grid.savepoints
            self._alternate = enable
            if not enable and mode == ALT_SAVE_CURSOR:
                grid.restore_cursor()
        elif private and mode == _SAVE_CURSOR_MODE:
            if enable:
                self._grid.save_cursor()
            else:
                self._grid.restore_cursor()
        elif mode in (_SCREEN_MODES if private else _ANSI_MODES):
            apply = self._grid.set_mode if enable else self._grid.reset_mode
            apply(mode, private=private)

    def _control(self, body: str, csi: bool) -> None:
        if not csi:
            if body == "c":
                for grid in (self._grid, self._other):
                    grid.reset()
                    grid.savepoints.clear()
                self._alternate = False
            elif _CHARSETS.fullmatch(body):
                self._grid.define_charset(body[1], mode=body[0])
            elif body in Stream.escape or body == "#8":
                self._stream.feed(_ESC + body)
            return
        match = _CSI_ARGS.fullmatch(body)
        if match is None:
            return
        private, fields, final = match.groups()
        if final in "hl":
            for field in fields.split(";"):
                self._mode(bool(private), int(field or "0"), final == "h")
        elif not private and final in ("s", "u") and not fields:
            if final == "s":
                self._grid.save_cursor()
            else:
                self._grid.restore_cursor()
        elif not private and final in ("S", "T") and ";" not in fields:
            count = int(fields or "1") or 1
            self._grid.scroll_region(count if final == "S" else -count)
        elif not private and final in Stream.csi and final not in "cmn":
            # Ignore attributes and device queries. A passive observer cannot
            # reply, and extra/invalid arguments must not abort the real session.
            if fields.count(";") > (1 if final in "Hfr" else 0):
                return
            if final in "JK" and fields not in ("", "0", "1", "2"):
                return
            self._stream.feed(_CSI + body)

    def feed(self, data: bytes) -> None:
        """Mirror an output chunk, dropping non-text string payloads without buffering them."""
        with self._lock:
            text = self._pending + self._decoder.decode(data)
            self._pending = ""
            index = 0
            while index < len(text):
                if self._discard_until is not None:
                    end = self._discard_until.search(text, index)
                    if end is None:
                        # ST may be split between ESC and backslash.
                        if text.endswith(_ESC):
                            self._pending = _ESC
                        return
                    index = end.start() if end[0] == _ESC else end.end()
                    self._discard_until = None
                    continue
                special = _SPECIAL.search(text, index)
                stop = special.start() if special else len(text)
                if stop > index:
                    self._grid.draw(text[index:stop])
                if special is None:
                    return
                index = stop + 1
                char = special[0]
                if char == _ESC:
                    if index == len(text):
                        self._pending = _ESC
                        return
                    if text[index] == _ESC:
                        continue
                    introducer = text[index]
                    if introducer in _STRING_STARTS or introducer == "[":
                        index += 1
                else:
                    introducer = _C1_STRINGS.get(char)
                    if char == "\x9b":
                        introducer = "["
                    elif introducer is None:
                        event = Stream.basic.get(char)
                        if event:
                            getattr(self._grid, event)()
                        continue
                if introducer in _STRING_STARTS:
                    self._discard_until = _OSC_END if introducer == "]" else _STRING_END
                    continue
                csi = introducer == "["
                terminator = _CSI_END if csi else _ESC_END
                end = terminator.search(text, index)
                if end is None:
                    if len(text) - stop <= _MAX_CONTROL:
                        self._pending = text[stop:]
                    else:
                        self._discard_until = terminator
                    return
                if end[0] == _ESC:
                    index = end.start()
                    continue
                if end.end() - stop <= _MAX_CONTROL and end[0] not in "\x18\x1a":
                    self._control(text[index : end.end()], csi)
                index = end.end()
