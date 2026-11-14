"""Render the native shell's full terminal with one PTK renderer."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import UIContent, UIControl

from .mouse import decode_mouse
from .session import Session
from .vt import ALT, CTRL, SHIFT, SPECIAL_KEYS, Terminal

DEFAULT_SIZE = (80, 24)
FRAME_INTERVAL = 1 / 60
INPUT_TIMEOUT = 0.05
WHEEL_ROWS = 3
COPY_LIMIT = 1024 * 1024
WHEEL_BUTTONS = frozenset(("wheelup", "wheeldown", "wheelleft", "wheelright"))
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
        self.bindings.add(Keys.Vt100MouseEvent, eager=True)(self._mouse)
        self.bindings.add("s-pageup", eager=True)(self._page)
        self.bindings.add("s-pagedown", eager=True)(self._page)

    def is_focusable(self):
        """The native terminal is the sole input target in the base application."""
        return True

    def get_key_bindings(self):
        """Return bindings that forward input to the native terminal."""
        return self.bindings

    def _paste(self, event):
        """Encode paste using the child terminal's current bracketed-paste mode."""
        try:
            self.owner.paste(event.data)
        except Exception as exc:
            self.owner._failed(exc)

    def _mouse(self, event):
        """Decode host cell coordinates before PTK can fold wide glyph columns."""
        try:
            mouse = decode_mouse(event.data)
            if mouse is not None:
                self.owner.mouse(mouse)
        except Exception as exc:
            self.owner._failed(exc)
        # Scroll/selection changes invalidate explicitly. Hover must not redraw.
        return NotImplemented

    def _page(self, event):
        """Browse primary history; alternate-screen programs retain these keys."""
        try:
            if self.owner.vt.alternate:
                self._key(event)
            else:
                direction = -1 if event.key_sequence[-1].key == Keys.ShiftPageUp else 1
                self.owner.scroll(direction * max(1, self.owner.size[1] - 1))
        except Exception as exc:
            self.owner._failed(exc)

    def _key(self, event):
        """Forward control/text bytes and encode special keys for the child modes."""
        try:
            self.owner.live()
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
        self.drag_origin = None
        self.dragged = False
        self.pressed_buttons = []
        self.native_buttons = {}
        self.pending_copy = None
        self.session = Session(
            [shell, "-i"], str(cwd), self._output, self._exited, self._failed
        )
        self.vt = Terminal(*self.size, reply=self.session.send)
        self.control = TerminalControl(self)
        self.window = Window(self.control, wrap_lines=False)
        self.app = Application(
            layout=Layout(self.window),
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=FRAME_INTERVAL,
            after_render=self._after_render,
        )
        self.app.ttimeoutlen = INPUT_TIMEOUT

    def live(self):
        """Return to live input without replaying any history or selection text."""
        self.vt.scroll_bottom()
        self.vt.clear_selection()
        self.drag_origin = None
        self.dragged = False
        self.app.invalidate()

    def paste(self, text):
        """Paste through the same native encoding after returning to live output."""
        self.live()
        self.session.send(self.vt.encode_paste(text))

    def scroll(self, rows):
        """Move the native VT viewport without changing the child input stream."""
        self._release_mouse()
        self.vt.scroll(rows)
        if self.drag_origin is not None:
            self.dragged = True
        self.app.invalidate()

    def _release_mouse(self):
        """Finish delivered presses before leaving the application's live view."""
        for button, (x, y, modifiers) in tuple(self.native_buttons.items()):
            self.session.send(self.vt.encode_mouse("release", button, x, y, modifiers))
        self.native_buttons.clear()

    def _send_mouse(self, action, button, x, y, modifiers):
        """Remember only presses actually delivered to the native consumer."""
        data = self.vt.encode_mouse(action, button, x, y, modifiers)
        if data:
            self.session.send(data)
            if action == "press" and button not in WHEEL_BUTTONS:
                self.native_buttons[button] = (x, y, modifiers)
            elif action == "move":
                for held in self.native_buttons:
                    self.native_buttons[held] = (x, y, modifiers)
        if action == "release":
            self.native_buttons.pop(button, None)

    def mouse(self, event):
        """Route mouse input using VT modes and the visible viewport, never shell state."""
        x, y = event.x, event.y
        if x >= self.size[0] or y >= self.size[1]:
            if event.action == "press" or (
                self.drag_origin is None and not self.pressed_buttons
            ):
                return
            # Finish a captured drag even when its release is beyond the pane.
            x, y = min(x, self.size[0] - 1), min(y, self.size[1] - 1)
        button = event.button
        if event.action == "press" and button not in WHEEL_BUTTONS:
            if button not in self.pressed_buttons:
                self.pressed_buttons.append(button)
        if event.action == "release" and button is None:
            button = self.pressed_buttons[-1] if self.pressed_buttons else None
        if event.action == "release" and button in self.pressed_buttons:
            self.pressed_buttons.remove(button)

        # Modifier changes cannot move an in-progress drag to another consumer.
        if event.action in ("move", "release") and button in self.native_buttons:
            self._send_mouse(event.action, button, x, y, event.modifiers)
            return
        if event.action == "release" and self.drag_origin is None:
            return

        # A local drag remains local if output changes the application's modes.
        if self.drag_origin is not None and button not in WHEEL_BUTTONS:
            if button in ("left", None) and event.action in ("move", "release"):
                self.dragged |= (x, y) != self.drag_origin
                if self.dragged:
                    self.vt.update_selection(x, y)
                    self.app.invalidate()
                if event.action == "release":
                    if self.dragged:
                        self.copy_selection()
                    else:
                        self.vt.clear_selection()
                    self.drag_origin = None
                    self.dragged = False
            return

        native = (
            self.drag_origin is None
            and self.vt.viewport_active
            and self.vt.mouse_reporting
            and not event.modifiers & SHIFT
        )
        if native:
            if (
                event.action == "move"
                and button is not None
                and button not in self.native_buttons
            ):
                return
            self._send_mouse(event.action, button, x, y, event.modifiers)
            return
        if button in WHEEL_BUTTONS:
            if event.action != "press" or button in ("wheelleft", "wheelright"):
                return
            direction = -1 if button == "wheelup" else 1
            if (
                self.vt.alternate
                and self.vt.alternate_scroll
                and not event.modifiers & SHIFT
            ):
                self.live()
                key = "up" if direction < 0 else "down"
                self.session.send(self.vt.encode_key(key) * WHEEL_ROWS)
            else:
                self.scroll(direction * WHEEL_ROWS)
            return
        if event.action == "press" and button == "left":
            self._release_mouse()
            self.vt.begin_selection(x, y)
            self.drag_origin = (x, y)
            self.dragged = False
            self.app.invalidate()
        elif event.action == "press" and button == "middle":
            text = self.app.clipboard.get_data().text
            if text:
                self.paste(text)

    def copy_selection(self):
        """Copy a user-selected native range without reconstructing wrapped text."""
        text = self.vt.selection_text()
        if not text:
            return
        self.app.clipboard.set_text(text)
        encoded = text.encode("utf-8")
        self.pending_copy = None
        if len(encoded) <= COPY_LIMIT:
            self.pending_copy = base64.b64encode(encoded).decode("ascii")
        self.app.invalidate()

    def resize(self, cols, rows):
        """Keep the emulated grid and controlling PTY on the same pane dimensions."""
        if self.size != (cols, rows):
            self.vt.resize(cols, rows)
            self.session.resize(cols, rows)
            self.size = (cols, rows)

    def _output(self, data):
        """Only the VT receives child output; PTK remains the sole outer renderer."""
        alternate = self.vt.alternate
        self.vt.feed(data)
        if self.vt.alternate != alternate:
            self.drag_origin = None
            self.dragged = False
            self.pressed_buttons.clear()
            self.native_buttons.clear()
        elif not self.vt.mouse_reporting:
            self.native_buttons.clear()
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
        if self.pending_copy is not None:
            # Use PTK's output queue after rendering. Child output never bypasses VT.
            app.output.write_raw(f"\x1b]52;c;{self.pending_copy}\x07")
            app.output.flush()
            self.pending_copy = None
        frame = self.control.frame
        if self.dump_state is None or frame is None:
            return
        value = {
            "cols": self.size[0],
            "rows": self.size[1],
            "lines": ["".join(text for _, text in line) for line in frame.lines],
            "cursor": [frame.cursor_x, frame.cursor_y],
            "alternate": frame.alternate,
            "viewport_active": frame.viewport_active,
            "viewport_offset": frame.viewport_offset,
            "scrollback_rows": frame.scrollback_rows,
            "mouse_reporting": self.vt.mouse_reporting,
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
