"""Render the native shell's full terminal with one PTK renderer."""

from __future__ import annotations

import json
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import UIContent, UIControl

from .session import Session
from .vt import ALT, CTRL, SHIFT, SPECIAL_KEYS, Terminal

DEFAULT_SIZE = (80, 24)
FRAME_INTERVAL = 1 / 60
INPUT_TIMEOUT = 0.05
SKIP_KEYS = frozenset(
    (
        Keys.Any,
        Keys.CPRResponse,
        Keys.Vt100MouseEvent,
        Keys.WindowsMouseEvent,
        Keys.Ignore,
        Keys.BracketedPaste,
    )
)
MODIFIERS = {"c": CTRL, "s": SHIFT, "a": ALT}


class TerminalControl(UIControl):
    """Render VT cells without a shadow command-line buffer or shell parsing."""

    def __init__(self, owner: TerminalApp):
        """Bind terminal input without reserving application shortcut keys."""
        self.owner = owner
        self.frame = None
        self.bindings = KeyBindings()
        self.bindings.add(Keys.Any, eager=True)(self._key)
        for key in Keys:
            if key not in SKIP_KEYS:
                self.bindings.add(key, eager=True)(self._key)
        self.bindings.add(Keys.BracketedPaste, eager=True)(self._paste)

    def is_focusable(self):
        """The native terminal is the sole input target in the base application."""
        return True

    def get_key_bindings(self):
        """Return bindings that forward input to the native terminal."""
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
    """Display the native terminal while the shell owns command editing."""

    def __init__(self, shell: str, cwd: Path, *, dump_state: Path | None = None):
        """Create one full-size terminal pane with no built-in overlays."""
        self.dump_state = dump_state
        self.size = DEFAULT_SIZE
        self.session = Session(
            [shell, "-i"], str(cwd), self._output, self._exited, self._failed
        )
        self.vt = Terminal(*self.size, reply=self.session.send)
        self.control = TerminalControl(self)
        self.window = Window(self.control, wrap_lines=False)
        self.app = Application(
            layout=Layout(self.window),
            full_screen=True,
            mouse_support=False,
            min_redraw_interval=FRAME_INTERVAL,
            after_render=self._after_render,
        )
        self.app.ttimeoutlen = INPUT_TIMEOUT

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
            "child_pid": self.session.pid,
        }
        temporary = self.dump_state.with_name(self.dump_state.name + ".next")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.dump_state)

    async def run(self) -> int:
        """Run the full-screen UI and restore the host terminal on every exit path."""
        size = self.app.output.get_size()
        self.resize(max(1, size.columns), max(1, size.rows))
        try:
            return await self.app.run_async(
                pre_run=lambda: self.session.start(*self.size),
                set_exception_handler=False,
            )
        finally:
            await self.session.close()
            self.vt.close()
