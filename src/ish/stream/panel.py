"""Render optional PTK widgets inside a caller-owned bottom rectangle.

The application's output is a DummyOutput. Only completed PTK cells leave this
module; host modes, queries and shell output belong to the stream coordinator.
No panel or shortcut is installed by importing this module.
"""

from __future__ import annotations

import asyncio
import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import DummyInput
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import Attrs, Style
from prompt_toolkit.widgets import TextArea

DEFAULT_COLUMNS = 80
DEFAULT_ROWS = 5
POPUP_MIN_ROWS = 3
KEY_FLUSH_SECONDS = 0.5
CLOSING_INPUT = re.compile(rb"[\r\n\x03\x04]")
BAR_TEXT = "ish stream | native shell | F2 popup"
POPUP_TITLE = "Plugin popup | Enter accepts | Esc cancels"
STYLE = {"bar": "fg:ansicyan", "popup-title": "bold", "popup-input": ""}


@dataclass(frozen=True)
class Cell:
    """An immutable PTK-rendered cell, independent of the shell screen."""

    text: str
    width: int
    attrs: Attrs


@dataclass(frozen=True)
class Frame:
    """One completed panel frame; coordinates are local until render()."""

    cells: tuple[tuple[Cell, ...], ...]
    columns: int
    cursor: tuple[int, int] | None
    popup: bool

    @property
    def lines(self) -> tuple[str, ...]:
        """Expose displayed text without terminal control sequences."""
        return tuple("".join(cell.text for cell in row) for row in self.cells)

    def render(self, top: int) -> bytes:
        """Paint only the rectangle with CUP/SGR; never alter host modes.

        ``top`` is the first one-based host row. The caller owns saving and
        restoring the shell cursor. Full-width rows are followed by explicit
        cursor moves, preventing subsequent text from consuming pending wrap.
        """
        if top < 1:
            raise ValueError("Panel top must be a positive terminal row")
        stream = StringIO()
        output = Vt100_Output(
            stream,
            lambda: Size(rows=len(self.cells), columns=self.columns),
            enable_bell=False,
            enable_cpr=False,
        )
        output.reset_attributes()
        previous = None
        for offset, row in enumerate(self.cells):
            output.cursor_goto(top + offset, 1)
            for cell in row:
                if cell.attrs != previous:
                    output.set_attributes(cell.attrs, ColorDepth.DEPTH_24_BIT)
                    previous = cell.attrs
                output.write(cell.text)
        output.reset_attributes()
        if self.cursor is not None:
            column, row = self.cursor
            output.cursor_goto(top + row, column + 1)
        else:
            output.cursor_goto(top, 1)
        output.flush()
        return stream.getvalue().encode("utf-8")


class RoutedInput(DummyInput):
    """Leave application lifetime to the coordinator, without reading stdin."""

    @property
    def closed(self) -> bool:
        """Keep PTK running while explicitly routed key events arrive."""
        return False


class ViewportOutput(DummyOutput):
    """Supply PTK panel dimensions without sending anything to a terminal."""

    def __init__(self, columns: int, rows: int):
        """Store the current panel size for normal PTK layout."""
        self.size = Size(rows=rows, columns=columns)

    def get_size(self) -> Size:
        """Return only the reserved rectangle's dimensions."""
        return self.size


class Panel:
    """An explicitly created plugin panel with native PTK popup editing."""

    def __init__(
        self,
        on_frame: Callable[[Frame], None],
        *,
        on_focus: Callable[[bool], None] | None = None,
        on_result: Callable[[str | None], None] | None = None,
        on_remainder: Callable[[bytes], None] | None = None,
        bar: str = BAR_TEXT,
    ):
        """Keep UI callbacks separate from all shell/host input and output."""
        self.on_frame, self.on_focus, self.on_result = on_frame, on_focus, on_result
        self.on_remainder = on_remainder
        self.bar = bar
        self.visible = False
        self.popup = False
        self.app = None
        self.task = None
        self.frame = None
        self.output = None
        self._parser = None
        self._decoder = None
        self._flush_handle = None
        self._generation = 0
        self._field = None
        self._closed = False

    async def start(self, cols: int = DEFAULT_COLUMNS, rows: int = DEFAULT_ROWS):
        """Start the real PTK event loop, initially hidden, without host I/O."""
        if self.task is not None or self._closed:
            raise RuntimeError("Panel can only be started once")
        self._validate_size(cols, rows)
        self.output = ViewportOutput(cols, rows)
        self._field = TextArea(
            height=1,
            multiline=False,
            wrap_lines=False,
            style="class:popup-input",
            accept_handler=self._accept,
        )
        bindings = KeyBindings()

        @bindings.add("escape")
        @bindings.add("c-c", eager=True)
        @bindings.add("c-d", eager=True)
        def cancel(event):
            self.close_popup()

        @bindings.add("escape", Keys.Any)
        def unmapped_meta(event):
            # Specific PTK Meta bindings take precedence. An unknown Alt key
            # still belongs to the popup; it is not a standalone Escape close.
            pass

        popup = ConditionalContainer(
            HSplit(
                [
                    Window(
                        FormattedTextControl(POPUP_TITLE),
                        height=1,
                        style="class:popup-title",
                    ),
                    self._field,
                ]
            ),
            Condition(lambda: self.popup),
        )
        content = HSplit(
            [
                popup,
                Window(),
                Window(
                    FormattedTextControl(lambda: self.bar),
                    height=1,
                    style="class:bar",
                ),
            ]
        )
        started = asyncio.Event()
        self.app = Application(
            layout=Layout(content),
            input=RoutedInput(),
            output=self.output,
            full_screen=True,
            mouse_support=False,
            style=Style.from_dict(STYLE),
            key_bindings=bindings,
            after_render=self._rendered,
            terminal_size_polling_interval=None,
        )
        self.task = asyncio.create_task(
            self.app.run_async(
                pre_run=started.set, set_exception_handler=False, handle_sigint=False
            )
        )
        self.task.add_done_callback(lambda task: started.set())
        await started.wait()
        if self.task.done():
            await self.task

    @staticmethod
    def _validate_size(cols, rows):
        if cols < 1 or rows < 1:
            raise ValueError("Panel dimensions must be positive")

    def resize(self, cols: int, rows: int):
        """Relayout widgets without acquiring or resizing the shell screen."""
        self._validate_size(cols, rows)
        if self.app is None or self._closed:
            raise RuntimeError("Panel is not running")
        self.output.size = Size(rows=rows, columns=cols)
        if rows < POPUP_MIN_ROWS:
            self.close_popup()
        self.app.invalidate()

    def show(self):
        """Allow frames after the coordinator has acquired a prompt region."""
        if self.app is None or self._closed:
            raise RuntimeError("Panel is not running")
        self.visible = True
        self.app.invalidate()

    def hide(self):
        """Synchronously revoke UI input and suppress all further frames."""
        self.visible = False
        self.close_popup()

    def open_popup(self) -> bool:
        """Give PTK focus only while a sufficiently large panel is visible."""
        if (
            not self.visible
            or self._closed
            or self.popup
            or self.output.size.rows < POPUP_MIN_ROWS
        ):
            return False
        self._reset_input()
        self.app.key_processor.reset()
        self._field.text = ""
        self.popup = True
        self.app.layout.focus(self._field)
        if self.on_focus:
            self.on_focus(True)
        self.app.invalidate()
        return True

    def close_popup(self, result: str | None = None) -> bool:
        """Release popup focus once; stale closes cannot reopen it."""
        if not self.popup:
            return False
        self.popup = False
        self._reset_input()
        if self.on_focus:
            self.on_focus(False)
        if self.on_result:
            self.on_result(result)
        self.app.invalidate()
        return True

    def feed(self, data: bytes) -> bool:
        """Consume popup keys and return untouched bytes after accept/cancel.

        Only CR/LF/Ctrl+C/Ctrl+D can synchronously finish this popup. Feed up to
        each such byte so PTK, including its bracketed-paste parser, decides
        whether it is a key or literal data. The remaining original bytes are
        never decoded or offered to another popup; the coordinator holds them
        until its native-return boundary.
        """
        if not self.popup or not self.visible or self._closed:
            return False
        generation = self._generation
        offset = 0
        with set_app(self.app):
            for closing in CLOSING_INPUT.finditer(data):
                end = closing.end()
                self._parser.feed(self._decoder.decode(data[offset:end]))
                offset = end
                if not self.popup or generation != self._generation:
                    if offset < len(data) and self.on_remainder:
                        self.on_remainder(data[offset:])
                    return True
            if offset < len(data):
                self._parser.feed(self._decoder.decode(data[offset:]))
        if self.popup and generation == self._generation:
            if self._flush_handle is not None:
                self._flush_handle.cancel()
            self._flush_handle = asyncio.get_running_loop().call_later(
                KEY_FLUSH_SECONDS, self._flush_keys, generation
            )
        return True

    def _reset_input(self):
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._generation += 1
        generation = self._generation
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def key_received(key):
            if self.popup and self.visible and generation == self._generation:
                self.app.key_processor.feed(key)
                self.app.key_processor.process_keys()

        self._parser = Vt100Parser(key_received)

    def _flush_keys(self, generation):
        self._flush_handle = None
        if self.popup and self.visible and generation == self._generation:
            with set_app(self.app):
                self._parser.flush()

    def _accept(self, buffer):
        self.close_popup(buffer.text)
        return True

    def _rendered(self, app):
        if not self.visible or self._closed:
            return
        screen = app.renderer.last_rendered_screen
        if screen is None:
            return
        size = self.output.size
        rows = []
        for row in range(size.rows):
            cells = []
            column = 0
            while column < size.columns:
                cell = screen.data_buffer[row][column]
                width = max(1, cell.width)
                text = cell.char if column + width <= size.columns else " "
                attrs = app.renderer.style.get_attrs_for_style_str(cell.style)
                cells.append(Cell(text, min(width, size.columns - column), attrs))
                column += width
            rows.append(tuple(cells))
        cursor = None
        if self.popup:
            point = screen.get_cursor_position(app.layout.current_window)
            cursor = (
                min(max(0, point.x), size.columns - 1),
                min(max(0, point.y), size.rows - 1),
            )
        self.frame = Frame(tuple(rows), size.columns, cursor, self.popup)
        self.on_frame(self.frame)

    async def close(self):
        """Stop PTK and its key parser; host cleanup is external."""
        if self._closed:
            return
        self.hide()
        self._closed = True
        self._reset_input()
        if self.task is not None:
            if self.app.is_running and not self.app.is_done:
                self.app.exit()
            await self.task
