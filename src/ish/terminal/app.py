"""Compose a libghostty terminal pane and focused popups with one PTK renderer."""

from __future__ import annotations

import json
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Frame, Label, TextArea

from .session import Session
from .vt import ALT, CTRL, SHIFT, SPECIAL_KEYS, Terminal

BAR_ROWS = 1
FRAME_INTERVAL = 1 / 60
KEY_TIMEOUT = 0.25
POPUP_TITLE = "ish-vt popup"
POPUP_DESCRIPTION = "This input stays in the popup."
SKIP_KEYS = frozenset(
    (
        Keys.Any,
        Keys.CPRResponse,
        Keys.Vt100MouseEvent,
        Keys.WindowsMouseEvent,
        Keys.Ignore,
        Keys.BracketedPaste,
        Keys.F2,
        Keys.F10,
    )
)
MODIFIERS = {"c": CTRL, "s": SHIFT, "a": ALT}
STYLE = Style.from_dict(
    {
        "status": "bg:#243447 #ffffff",
        "frame.border": "#82aaff",
        "frame.label": "bg:#243447 #ffffff bold",
        "text-area": "bg:#17212f #ffffff",
        "button.focused": "bg:#82aaff #000000",
    }
)


class TerminalControl(UIControl):
    """Render VT cells without a shadow command-line buffer or shell parsing."""

    def __init__(self, owner: TerminalApp):
        """Bind all terminal keys locally; popup buffers retain normal PTK editing."""
        self.owner = owner
        self.frame = None
        self.bindings = KeyBindings()
        self.bindings.add(Keys.Any, eager=True)(self._key)
        for key in Keys:
            if key not in SKIP_KEYS:
                self.bindings.add(key, eager=True)(self._key)
        self.bindings.add(Keys.BracketedPaste, eager=True)(self._paste)

    def is_focusable(self):
        """The terminal pane consumes input whenever no popup owns focus."""
        return not self.owner.popup_open

    def get_key_bindings(self):
        """Return pane-only bindings, excluding the explicit application shortcuts."""
        return self.bindings

    def _paste(self, event):
        """Encode paste using the child terminal's current bracketed-paste mode."""
        try:
            self.owner.session.send(self.owner.vt.encode_paste(event.data))
        except Exception as exc:
            self.owner._failed(exc)

    def _key(self, event):
        """Forward control/text bytes and encode special keys for the child modes."""
        try:
            self.owner.session.send(self._key_bytes(event))
        except Exception as exc:
            self.owner._failed(exc)

    def _key_bytes(self, event):
        """Translate PTK key events without inventing a shell editing buffer."""
        key = event.key_sequence[-1].key
        name = key.value if isinstance(key, Keys) else key
        if key == Keys.SIGINT:
            return b"\x03"
        parts = name.split("-")
        base = parts[-1]
        if base in SPECIAL_KEYS:
            modifiers = 0
            for part in parts[:-1]:
                modifiers |= MODIFIERS.get(part, 0)
            return self.owner.vt.encode_key(base, modifiers)
        if len(event.data) == 1:
            # C0 bytes retain Ctrl+C/D and CR/LF semantics. Printable keys use
            # the child's encoder. Multi-character input is already committed
            # text (or an unsupported outer key sequence), never a key name.
            return self.owner.vt.encode_key(event.data)
        return event.data.encode("utf-8")

    def create_content(self, width, height):
        """Resize the VT/PTY together and expose one immutable rendered frame."""
        width, height = max(1, width), max(1, height)
        self.owner.resize(width, height)
        self.frame = self.owner.vt.frame()
        frame = self.frame
        return UIContent(
            get_line=lambda row: frame.lines[row] if row < len(frame.lines) else [],
            line_count=height,
            cursor_position=Point(
                x=max(0, min(width - 1, frame.cursor_x)),
                y=max(0, min(height - 1, frame.cursor_y)),
            ),
            show_cursor=frame.cursor_visible,
        )


class TerminalApp:
    """Own screen composition and input focus while the shell owns command editing."""

    def __init__(self, shell: str, cwd: Path, *, dump_state: Path | None = None):
        """Create a terminal pane, persistent bar, and one independently editable popup."""
        self.shell = shell
        self.dump_state = dump_state
        self.popup_open = False
        self.size = (80, 23)
        self.session = Session(
            [shell, "-i"], str(cwd), self._output, self._exited, self._failed
        )
        self.vt = Terminal(*self.size, reply=self.session.send)
        self.control = TerminalControl(self)
        self.window = Window(self.control, wrap_lines=False)
        self.popup_input = TextArea(multiline=True, height=3)
        self.popup_button = Button("Close", handler=self.hide_popup)
        popup = Frame(
            HSplit([Label(POPUP_DESCRIPTION), self.popup_input, self.popup_button]),
            title=POPUP_TITLE,
        )
        status = Window(
            FormattedTextControl(self._status), height=BAR_ROWS, style="class:status"
        )
        body = FloatContainer(
            HSplit([self.window, status]),
            floats=[
                Float(
                    ConditionalContainer(popup, Condition(lambda: self.popup_open)),
                    width=54,
                    height=8,
                )
            ],
        )
        bindings = KeyBindings()
        bindings.add("f2", eager=True)(lambda event: self.toggle_popup())
        bindings.add("f10", eager=True)(lambda event: event.app.exit(result=0))
        popup_visible = Condition(lambda: self.popup_open)
        bindings.add("escape", filter=popup_visible)(lambda event: self.hide_popup())
        # Legacy terminals encode Alt+x as Escape followed by x. Consume that
        # combination inside the popup instead of closing it and leaking x.
        bindings.add("escape", Keys.Any, filter=popup_visible)(lambda event: None)
        bindings.add("c-c", filter=popup_visible, eager=True)(
            lambda event: self.hide_popup()
        )
        bindings.add("tab", filter=popup_visible)(self._popup_focus)
        bindings.add("s-tab", filter=popup_visible)(self._popup_focus)
        self.app = Application(
            layout=Layout(body, focused_element=self.window),
            key_bindings=bindings,
            style=STYLE,
            full_screen=True,
            mouse_support=False,
            min_redraw_interval=FRAME_INTERVAL,
            after_render=self._after_render,
        )
        self.app.ttimeoutlen = 0.05
        self.app.timeoutlen = KEY_TIMEOUT

    def _status(self):
        """Show UI state without guessing whether the shell is executing or editing."""
        focus = "popup" if self.popup_open else "terminal"
        return f" ish-vt | {Path(self.shell).name} | F2 popup | F10 quit | {focus} "

    def show_popup(self):
        """Move input focus to the popup while terminal output continues normally."""
        self.popup_open = True
        self.app.layout.focus(self.popup_input)
        self.app.invalidate()

    def hide_popup(self):
        """Return focus without replaying any popup input into the shell."""
        self.popup_open = False
        self.app.layout.focus(self.window)
        self.app.invalidate()

    def toggle_popup(self):
        """Toggle the single demonstration popup from either focus context."""
        if self.popup_open:
            self.hide_popup()
        else:
            self.show_popup()

    def _popup_focus(self, event):
        """Cycle inside the modal popup without exposing the terminal behind it."""
        target = (
            self.popup_button
            if self.app.layout.has_focus(self.popup_input)
            else self.popup_input
        )
        self.app.layout.focus(target)

    def resize(self, cols, rows):
        """Keep the emulated grid and controlling PTY on the same pane dimensions."""
        if self.size != (cols, rows):
            self.vt.resize(cols, rows)
            self.session.resize(cols, rows)
            self.size = (cols, rows)

    def _output(self, data):
        """Only the VT receives child output; PTK remains the sole outer renderer."""
        self.vt.feed(data)
        self.app.invalidate()

    def _exited(self, returncode):
        """Propagate the child result once its final output has been consumed."""
        if self.app.is_running and not self.app.is_done:
            self.app.exit(result=returncode)

    def _failed(self, error):
        """Abort the application on transport errors rather than dropping input."""
        if self.app.is_running and not self.app.is_done:
            self.app.exit(exception=error)

    def _after_render(self, app):
        """Publish optional diagnostic state after composition, never for readiness."""
        frame = self.control.frame
        if self.dump_state is None or frame is None:
            return
        value = {
            "cols": self.size[0],
            "rows": self.size[1],
            "lines": ["".join(text for _, text in line) for line in frame.lines],
            "cursor": [frame.cursor_x, frame.cursor_y],
            "alternate": frame.alternate,
            "popup": self.popup_open,
            "popup_text": self.popup_input.text,
            "child_pid": self.session.pid,
        }
        temporary = self.dump_state.with_name(self.dump_state.name + ".next")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.dump_state)

    async def run(self) -> int:
        """Run the full-screen UI and restore the host terminal on every exit path."""
        size = self.app.output.get_size()
        self.resize(max(1, size.columns), max(1, size.rows - BAR_ROWS))
        try:
            return await self.app.run_async(
                pre_run=lambda: self.session.start(*self.size),
                set_exception_handler=False,
            )
        finally:
            await self.session.close()
            self.vt.close()
