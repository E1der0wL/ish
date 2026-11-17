"""Render the native shell's full terminal with one PTK renderer."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import struct
import termios
import time
from dataclasses import asdict, replace
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_session, set_app
from prompt_toolkit.application.run_in_terminal import in_terminal
from prompt_toolkit.cursor_shapes import CursorShape, DynamicCursorShapeConfig
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
from prompt_toolkit.key_binding.key_processor import KeyProcessor
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.output import ColorDepth

from ish.lang import Message, i18n
from ish.stream.settings import ESCAPE_DELAY, INTERACTIVE_ARGS, validate_escape_delay

from . import diagnostics
from .clipboard import ClipboardBroker
from .host import HostBroker, HostInput, directory_uri
from .mouse import DEFAULT_MOUSE, decode_mouse, validate_mouse_policy
from .plugins import Plugins
from .rendering import Composition, NativeWindow, RestoringOutput, TerminalRenderer
from .selection import SelectionController
from .session import Session
from .settings import (
    CLIPBOARD_POLICIES,
    COLOR_DEPTHS,
    DEFAULT_CLIPBOARD,
    DEFAULT_COLOR_DEPTH,
    validate_color_depth,
)
from .vt import (
    ALT,
    CTRL,
    DEFAULT_SCROLLBACK_BYTES,
    DEFAULT_SCROLLBACK_LINES,
    KEY_REPORT_EVENTS,
    SHIFT,
    SPECIAL_KEYS,
    Terminal,
)

DEFAULT_SIZE = (80, 24)
FRAME_INTERVAL = 1 / 60
WHEEL_ROWS = 3
MAX_EFFECT_BYTES = 96 * 1024 * 1024
PROBE_SECONDS = 0.75
RENDER_HOLD_SECONDS = 1.0
CURSOR_SHAPES = {
    "block": "BLOCK",
    "block_hollow": "BLOCK",
    "bar": "BEAM",
    "underline": "UNDERLINE",
}
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


class InputProcessor(KeyProcessor):
    """Keep one structured physical key with the UI that consumed its first part."""

    def __init__(self, owner, previous):
        """Reuse the pinned PTK registry, prefix matching and processing loop."""
        self.owner = owner
        super().__init__(previous._bindings)

    def feed(self, key_press, first=False):
        """Consume unsolicited reports before matching any key-binding prefix."""
        if key_press.key != Keys.CPRResponse:
            super().feed(key_press, first=first)

    def feed_multiple(self, key_presses, first=False):
        """Keep adjacent real keys when a host report arrives in the same batch."""
        super().feed_multiple(
            [key for key in key_presses if key.key != Keys.CPRResponse], first=first
        )

    def _call_handler(self, handler, key_sequence):
        """Claim only this handler's sequence, never other keys still buffered."""
        if not self.owner.mouse_captured and any(
            key.key == Keys.Vt100MouseEvent for key in key_sequence
        ):
            # Reports already queued before capture ended cannot click a new UI.
            return
        if any(self.owner.host.key_consumed(key) for key in key_sequence):
            # A prior handler can have changed focus halfway through Alt's
            # legacy fallback. Remove that physical key and requeue unrelated
            # buffered keys in their original order before any later input.
            pending = [
                key for key in self.key_buffer if not self.owner.host.key_consumed(key)
            ]
            self.key_buffer.clear()
            self.feed_multiple(pending, first=True)
            return
        if any(
            key.key not in (Keys.Ignore, Keys.Vt100MouseEvent, Keys.CPRResponse)
            for key in key_sequence
        ):
            self.owner.clipboard.cancel_paste()
        if not self.owner.app.layout.has_focus(self.owner.window):
            for key in key_sequence:
                self.owner.host.consume_key(key)
        return super()._call_handler(handler, key_sequence)


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
        self.bindings.add(Keys.Ignore, eager=True)(self._key)
        self.bindings.add("s-pageup", eager=True)(self._page)
        self.bindings.add("s-pagedown", eager=True)(self._page)
        self._widget_mouse = (
            load_mouse_bindings()
            .get_bindings_for_keys((Keys.Vt100MouseEvent,))[0]
            .handler
        )

    def is_focusable(self):
        """The native terminal is the sole input target in the base application."""
        return True

    def get_key_bindings(self):
        """Return bindings that forward input to the native terminal."""
        return merge_key_bindings([self.bindings, self.owner.plugins.bindings])

    def _paste(self, event):
        """Encode paste using the child terminal's current bracketed-paste mode."""
        try:
            if self.owner.stopping:
                return
            self.owner.paste(event.data)
        except Exception as exc:
            self.owner._failed(exc)

    def _mouse(self, event):
        """Decode host cell coordinates before PTK can fold wide glyph columns."""
        try:
            if self.owner.stopping:
                return
            mouse = decode_mouse(event.data)
            if mouse is not None:
                pixels = getattr(event.key_sequence[-1], "pixel_position", None)
                position = self.owner.window.position
                if position is not None:
                    inside = (
                        position.xpos <= mouse.x < position.xpos + position.width
                        and position.ypos <= mouse.y < position.ypos + position.height
                    )
                    if (
                        not inside
                        and not self.owner.selection.active
                        and not self.owner.native_buttons
                    ):
                        return self._widget_mouse(event)
                    mouse = replace(
                        mouse, x=mouse.x - position.xpos, y=mouse.y - position.ypos
                    )
                    if pixels is not None:
                        pixels = (
                            pixels[0] - position.xpos * self.owner.profile.cell_width,
                            pixels[1] - position.ypos * self.owner.profile.cell_height,
                        )
                self.owner.mouse(mouse, pixel_position=pixels)
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
            if self.owner.stopping:
                return
            if self.owner.host.handle_control(event.key_sequence[-1]):
                return
            if self.owner.host.handle_key(event.key_sequence[-1]):
                return
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

    def __init__(
        self,
        shell: str,
        cwd: Path,
        *,
        dump_state: Path | None = None,
        plugins=(),
        plugin_dir=None,
        library_dir=None,
        clipboard=DEFAULT_CLIPBOARD,
        mouse=DEFAULT_MOUSE,
        color_depth=DEFAULT_COLOR_DEPTH,
        features=None,
        shell_args=INTERACTIVE_ARGS,
        escape_delay=ESCAPE_DELAY,
        scrollback_lines=DEFAULT_SCROLLBACK_LINES,
        scrollback_bytes=DEFAULT_SCROLLBACK_BYTES,
    ):
        """Create one full-size terminal pane with no built-in overlays."""
        self.dump_state = dump_state
        self.launch_cwd = Path.cwd()
        self.cwd_changed = False
        self.size = DEFAULT_SIZE
        self.pressed_buttons = []
        self.native_buttons = {}
        self.mouse_policy = validate_mouse_policy(mouse)
        self.color_depth_policy = validate_color_depth(color_depth)
        preferred_color_depth = (
            COLOR_DEPTHS[self.color_depth_policy] or ColorDepth.from_env()
        )
        escape_delay = validate_escape_delay(escape_delay)
        if clipboard not in CLIPBOARD_POLICIES:
            raise ValueError(
                i18n.get(
                    Message.VT_SETTING_CHOICE,
                    name="clipboard",
                    choices=", ".join(CLIPBOARD_POLICIES),
                )
            )
        self.control_output = []
        self.control_bytes = 0
        self.effects = {}
        self.animation = None
        self.hold_timer = None
        self.hold_serial = 0
        self.pressed_keys = {}
        self.host_focused = True
        self.child_result = None
        self.finish_task = None
        self.stopping = False
        self.presentation_ready = False
        self.suspend_task = None
        self.plugin_names = tuple(plugins)
        self.plugin_dir, self.library_dir = plugin_dir, library_dir
        self.host = HostBroker(
            features=features,
            on_focus=self._host_focus,
            on_graphics_error=self._graphics_error,
            on_key=self._host_key,
            on_clipboard_reply=lambda sequence: self.clipboard.host_reply(sequence),
            on_appearance=self._appearance,
        )
        self.input = HostInput(create_input(), self.host)
        self.session = Session(
            [shell, *shell_args], str(cwd), self._output, self._exited, self._failed
        )
        self.vt = Terminal(
            *self.size,
            reply=self.session.send,
            scrollback_lines=scrollback_lines,
            scrollback_bytes=scrollback_bytes,
        )
        self.clipboard = ClipboardBroker(
            reply=self.session.send,
            emit=self._queue_control,
            profile=lambda: self.profile,
            allow_read=clipboard == "read-write",
            allow_write=clipboard != "off",
            failed=self._failed,
        )
        self.vt.clipboard = self.clipboard
        self.control = TerminalControl(self)
        self.window = NativeWindow(self.control)
        self.plugins = Plugins(self, self.window)
        controls = KeyBindings()
        controls.add(Keys.Ignore, eager=True, is_global=True)(self._control)
        self.app = Application(
            layout=Layout(
                Composition(self.plugins.body, self), focused_element=self.window
            ),
            input=self.input,
            output=RestoringOutput(get_app_session().output, self),
            key_bindings=merge_key_bindings([controls, self.plugins.bindings]),
            cursor=DynamicCursorShapeConfig(self._cursor_shape),
            color_depth=preferred_color_depth,
            full_screen=True,
            mouse_support=Condition(lambda: self.mouse_enabled),
            min_redraw_interval=FRAME_INTERVAL,
            after_render=self._after_render,
        )
        self.app.key_processor = InputProcessor(self, self.app.key_processor)
        self.app.renderer = TerminalRenderer(self, self.app.renderer)
        self.app.ttimeoutlen = escape_delay
        self.selection = SelectionController(
            self.vt, changed=self.app.invalidate, copied=self.copy_selection
        )

    @property
    def profile(self):
        """Read effective presentation choices from their sole owner."""
        return self.host.profile

    @property
    def mouse_enabled(self):
        """Choose capture from explicit policy, native modes and opted-in surfaces."""
        if self.stopping or self.mouse_policy == "off":
            return False
        return (
            self.mouse_policy == "on"
            or self.plugins.mouse_required
            or (self.app.layout.has_focus(self.window) and self.vt.mouse_reporting)
        )

    @property
    def mouse_captured(self):
        """Accept reports only while both policy and the outer writer own capture."""
        return self.mouse_enabled and self.app.renderer.mouse_captured

    def set_mouse_policy(self, policy):
        """End the current gesture before changing its input consumer."""
        policy = validate_mouse_policy(policy)
        if policy == self.mouse_policy:
            return
        self.release_mouse_capture(clear=True)
        self.mouse_policy = policy
        self.app.invalidate()

    def _appearance(self, appearance):
        """Apply observed defaults while the native engine retains child overrides."""
        self.vt.set_appearance(
            foreground=appearance.foreground,
            background=appearance.background,
            cursor=appearance.cursor,
            palette=appearance.palette,
            scheme=appearance.scheme,
        )
        self.app.renderer.invalidate_screen()
        self.app.invalidate()

    def _cursor_shape(self):
        """Display the focused VT cursor without changing popup editor policy."""
        if self.session.pid is None:
            # First paint precedes probing. Do not overwrite the value being
            # queried; the child starts only after the observation is committed.
            return CursorShape._NEVER_CHANGE
        frame = self.control.frame
        if frame is None or self.plugins.popups:
            return CursorShape.BEAM
        shape = CURSOR_SHAPES.get(frame.cursor_shape, "BLOCK")
        if frame.cursor_blink:
            shape = "BLINKING_" + shape
        return CursorShape[shape]

    def _host_focus(self, focused):
        """Forward focus only to the active terminal input consumer."""
        self.host_focused = focused
        if not focused:
            self.release_capture()
        if not self.plugins.popups:
            self.focus_terminal(focused)

    def _control(self, event):
        """Dispatch ordered host events even while a modal widget has focus."""
        try:
            key = event.key_sequence[-1]
            if not self.host.handle_control(key) and self.app.layout.has_focus(
                self.window
            ):
                self.host.handle_key(key)
        except Exception as exc:
            self._failed(exc)

    def focus_terminal(self, focused):
        """Use the native focus encoder and the existing ordered PTY queue."""
        focused = (
            focused and self.host_focused and self.app.layout.has_focus(self.window)
        )
        self.session.send(self.vt.encode_focus(focused))

    def _host_key(self, key):
        """Dispatch structured events in the same PTK queue as ordinary input."""
        if self.stopping or (
            key.action == "release" and key.codepoint not in self.pressed_keys
        ):
            return True
        self.live()
        data = self.vt.encode_key(key.key, key.modifiers, action=key.action)
        self.session.send(data)
        if key.action == "release":
            self.pressed_keys.pop(key.codepoint, None)
        elif data and key.release_events and self.vt.keyboard_flags & KEY_REPORT_EVENTS:
            # A delivered legacy stroke has no pending physical release. Both
            # its source and its consumer must have requested event reporting
            # when the press was delivered, not only when a popup opens later.
            self.pressed_keys[key.codepoint] = key
        return True

    def release_keys(self):
        """Finish delivered physical key events before changing their consumer."""
        for key in self.pressed_keys.values():
            self.session.send(
                self.vt.encode_key(key.key, key.modifiers, action="release")
            )
        self.pressed_keys.clear()

    def release_capture(self):
        """Finish delivered presses and cancel local drags before losing focus."""
        self.release_keys()
        self.release_mouse_capture()

    def release_mouse_capture(self, *, clear=False):
        """Finish mouse presses and local timers without consuming keyboard input."""
        self._release_mouse()
        self.pressed_buttons.clear()
        self.selection.reset()
        if clear:
            self.vt.clear_selection()
        self.clipboard.cancel_paste()

    def _graphics_error(self, image_id, message):
        """Keep host failures out of child input and bound graphics recovery."""
        if self.app.renderer.reject_image(image_id, message):
            self.app.invalidate()

    def _geometry(self):
        """Use kernel-reported pixels when available; never invent cell size."""
        try:
            rows, cols, pixels_x, pixels_y = struct.unpack(
                "HHHH", fcntl.ioctl(self.input.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            )
            return cols, rows, pixels_x, pixels_y
        except (OSError, ValueError):
            size = self.app.output.get_size()
            return size.columns, size.rows, 0, 0

    async def _start(self):
        """Negotiate optional output before enabling it in the child emulator."""
        try:
            size = self.app.output.get_size()
            self.app.output.write_raw(self.host.begin_probe(size.columns, size.rows))
            self.app.output.flush()
            try:
                await asyncio.wait_for(self.host.ready.wait(), PROBE_SECONDS)
            except TimeoutError:
                pass  # Conservative host capability fallback, never shell readiness.
            self.host.finish_probe()
            self.presentation_ready = True
            initial_cursor = self.profile.initial_cursor_style
            if initial_cursor is not None:
                self.vt.set_default_cursor(
                    initial_cursor.shape, blink=initial_cursor.blink
                )
                # DEFAULT_CURSOR_* applies on reset only. Initialize once before
                # the PTY starts, never by resetting a running child's cursor.
                self.vt.feed_raw(b"\x1b[0 q")
            self._appearance(self.profile.appearance)
            self.host.refresh_geometry(*self._geometry())
            self.vt.resize(
                *self.size, self.profile.cell_width, self.profile.cell_height
            )
            self.vt.set_graphics(self.profile.image_protocol is not None)
            # Resolve selected bar geometry before the child's first terminal
            # query. The pre-probe redraw was intentionally suppressed.
            self.app._redraw()
            self.session.start(*self.size)
            self.app.renderer.invalidate_screen()
            self.app.invalidate()
        except Exception as exc:
            self._failed(exc)

    def _animate(self):
        """Schedule the VT's next image frame without polling shell processes."""
        self.animation = None
        if self.profile.image_protocol is None:
            return
        now = int(time.monotonic() * 1000)
        next_frame = self.vt.tick(now)
        if next_frame is not None:
            self.app.invalidate()
            self.animation = asyncio.get_running_loop().call_later(
                max(0.001, (next_frame - now) / 1000), self._animate
            )

    def live(self):
        """Return to live input without replaying any history or selection text."""
        self.release_hold()
        self.clipboard.cancel_paste()
        self.selection.reset()
        self.vt.scroll_bottom()
        self.vt.clear_selection()
        self.app.invalidate()

    def paste(self, text):
        """Paste through the same native encoding after returning to live output."""
        self.live()
        self.session.send(self.vt.encode_paste(text))

    def scroll(self, rows):
        """Move the native VT viewport without changing the child input stream."""
        self._release_mouse()
        self.clipboard.cancel_paste()
        self.release_hold()
        self.vt.scroll(rows)
        self.app.invalidate()

    def _release_mouse(self):
        """Finish delivered presses before leaving the application's live view."""
        for button, (x, y, modifiers, pixels) in tuple(self.native_buttons.items()):
            self.session.send(
                self.vt.encode_mouse(
                    "release", button, x, y, modifiers, pixel_position=pixels
                )
            )
        self.native_buttons.clear()

    def _send_mouse(self, action, button, x, y, modifiers, pixel_position=None):
        """Remember only presses actually delivered to the native consumer."""
        data = self.vt.encode_mouse(
            action, button, x, y, modifiers, pixel_position=pixel_position
        )
        if data:
            self.session.send(data)
            if action == "press" and button not in WHEEL_BUTTONS:
                self.native_buttons[button] = (x, y, modifiers, pixel_position)
            elif action == "move":
                for held in self.native_buttons:
                    self.native_buttons[held] = (x, y, modifiers, pixel_position)
        if action == "release":
            self.native_buttons.pop(button, None)

    def mouse(self, event, *, pixel_position=None):
        """Route mouse input using VT modes and the visible viewport, never shell state."""
        if not self.mouse_captured:
            return
        x, y = event.x, event.y
        pointer = None
        if pixel_position is not None and self.profile.has_cell_size:
            pointer = (
                (pixel_position[0] - 1) / self.profile.cell_width,
                (pixel_position[1] - 1) / self.profile.cell_height,
            )
        local_x, local_y = x, y
        if x < 0 or y < 0 or x >= self.size[0] or y >= self.size[1]:
            if event.action == "press" or (
                not self.selection.active and not self.pressed_buttons
            ):
                return
            # Finish a captured drag even when its release is beyond the pane.
            x, y = max(0, min(x, self.size[0] - 1)), max(0, min(y, self.size[1] - 1))
            if pixel_position is not None:
                pixel_position = (
                    max(
                        1,
                        min(pixel_position[0], self.size[0] * self.profile.cell_width),
                    ),
                    max(
                        1,
                        min(pixel_position[1], self.size[1] * self.profile.cell_height),
                    ),
                )
        button = event.button
        if event.action == "press":
            self.clipboard.cancel_paste()
        if event.action == "press" and button not in WHEEL_BUTTONS:
            if button not in self.pressed_buttons:
                self.pressed_buttons.append(button)
        if event.action == "release" and button is None:
            button = self.pressed_buttons[-1] if self.pressed_buttons else None
        if event.action == "release" and button in self.pressed_buttons:
            self.pressed_buttons.remove(button)

        # Modifier changes cannot move an in-progress drag to another consumer.
        if event.action in ("move", "release") and button in self.native_buttons:
            self._send_mouse(
                event.action, button, x, y, event.modifiers, pixel_position
            )
            return
        if event.action == "release" and not self.selection.active:
            return

        # A local drag remains local if output changes the application's modes.
        if self.selection.active and button not in WHEEL_BUTTONS:
            if button in ("left", None) and event.action in ("move", "release"):
                if event.action == "release":
                    self.selection.release(local_x, local_y, position=pointer)
                else:
                    self.selection.drag(local_x, local_y, position=pointer)
            return

        native = (
            not self.selection.active
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
            self._send_mouse(
                event.action, button, x, y, event.modifiers, pixel_position
            )
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
            self.release_hold()
            self.selection.press(
                x, y, rectangle=bool(event.modifiers & ALT), position=pointer
            )
        elif event.action == "press" and button == "middle":
            self.clipboard.read_primary(self._paste_primary)

    def _paste_primary(self, text):
        """Deliver a correlated user paste only to the still-focused live session."""
        if (
            not self.stopping
            and self.host_focused
            and self.app.layout.has_focus(self.window)
            and not self.plugins.popups
            and not self.session.closed
            and self.session.returncode is None
        ):
            self.paste(text)

    def copy_selection(self):
        """Copy a user-selected native range without reconstructing wrapped text."""
        text = self.vt.selection_text()
        if not text:
            return
        self.app.clipboard.set_text(text)
        self.clipboard.copy_selection(text.encode("utf-8"))
        self.app.invalidate()

    def resize(self, cols, rows):
        """Keep the emulated grid and controlling PTY on the same pane dimensions."""
        previous = self.profile
        self.host.refresh_geometry(*self._geometry())
        changed_geometry = (previous.cell_width, previous.cell_height) != (
            self.profile.cell_width,
            self.profile.cell_height,
        )
        if self.session.pid is not None:
            self.app.renderer.enter_input_modes()
        if self.size != (cols, rows) or changed_geometry:
            # Finish the old capture while its pixel geometry is still valid.
            self._release_mouse()
            self.pressed_buttons.clear()
            self.selection.reset()
            self.clipboard.cancel_paste()
            self.vt.resize(
                cols, rows, self.profile.cell_width, self.profile.cell_height
            )
            self.session.resize(cols, rows)
            self.size = (cols, rows)
            if self.session.pid is not None:
                self.vt.set_graphics(self.profile.image_protocol is not None)

    def _output(self, data):
        """Only the VT receives child output; PTK remains the sole outer renderer."""
        alternate = self.vt.alternate
        self.vt.feed(data)
        # Title/cwd represent current state; ring at most once per presentation.
        for effect in self.vt.drain_events():
            self.effects[effect.kind] = effect
        serial = self.vt.render_hold_serial
        if self.vt.synchronized_output:
            if serial != self.hold_serial:
                if self.hold_timer is not None:
                    self.hold_timer.cancel()
                self.hold_timer = asyncio.get_running_loop().call_later(
                    RENDER_HOLD_SECONDS, self.release_hold
                )
                self.hold_serial = serial
        elif self.hold_timer is not None:
            self.hold_timer.cancel()
            self.hold_timer = None
        if self.vt.alternate != alternate:
            self.selection.reset()
            self.clipboard.cancel_paste()
            self.pressed_buttons.clear()
            self.native_buttons.clear()
        elif not self.vt.mouse_reporting:
            self.native_buttons.clear()
        self.app.invalidate()
        if self.animation is None and self.profile.image_protocol is not None:
            self._animate()

    def release_hold(self):
        """Bound a display hold; this never controls shell input readiness."""
        if self.hold_timer is not None:
            self.hold_timer.cancel()
            self.hold_timer = None
        if self.vt.synchronized_output:
            self.vt.release_render_hold()
            self.app.invalidate()

    def _exited(self, returncode):
        """Propagate the child result once its final output has been consumed."""
        self.child_result = returncode
        self.clipboard.cancel_paste()
        if self.app.is_running and not self.app.is_done:
            if self.clipboard.pending is not None:
                if self.finish_task is None:
                    self.finish_task = self.app.create_background_task(
                        self._finish_session(returncode)
                    )
            else:
                self.app.exit(result=returncode)

    async def _finish_session(self, returncode):
        """Keep the host reader alive through an outstanding clipboard result."""
        await self.clipboard.idle.wait()
        if self.app.is_running and not self.app.is_done:
            self.app.exit(result=returncode)

    def _failed(self, error):
        """Abort the application on transport errors rather than dropping input."""
        if self.app.is_running and not self.app.is_done:
            self.app.exit(exception=error)

    def stop(self, signum):
        """Leave the PTK application normally so its existing cleanup can run."""
        if self.stopping:
            return
        self.stopping = True
        self.release_capture()
        self.clipboard.cancel_paste()
        if self.app.is_running and not self.app.is_done:
            self.app.exit(result=128 + signum)

    def suspend(self):
        """Schedule one outer suspension without sending Ctrl+Z to the child."""
        if (
            self.stopping
            or not self.presentation_ready
            or not self.app.is_running
            or self.app.is_done
            or self.suspend_task is not None
        ):
            return
        self.suspend_task = self.app.create_background_task(self._suspend())

    async def _suspend(self):
        """Reuse PTK's terminal handoff and redraw around an OS process stop."""
        try:
            self.release_capture()
            self.clipboard.cancel_paste()
            self.session.set_output_enabled(False)
            with set_app(self.app):
                async with in_terminal():
                    # SIGSTOP cannot be ignored by an orphaned process group.
                    # The caught TSTP request has already restored the host.
                    if not self.stopping and not self.app.is_done:
                        os.kill(os.getpid(), signal.SIGSTOP)
            self.app.renderer.invalidate_screen()
            self.app.invalidate()
        except Exception as error:
            self._failed(error)
        finally:
            self.session.set_output_enabled(True)
            self.suspend_task = None

    def restore_cwd(self, output):
        """Restore the known launch directory, separately from the child --cwd."""
        if self.cwd_changed:
            uri = directory_uri(str(self.launch_cwd))
            output.write_raw(f"\x1b]7;{uri}\x1b\\")
            self.cwd_changed = False

    def publish_effects(self, output):
        """Serialize queued effects through the same presentation output."""
        for control in self.control_output:
            output.write_raw(control)
        self.control_output.clear()
        self.control_bytes = 0
        for effect in self.effects.values():
            value = effect.data.decode("utf-8", errors="replace")
            if effect.kind == "bell":
                output.bell()
            elif not any(ord(c) < 32 or ord(c) == 127 for c in value):
                if effect.kind == "title":
                    # Titles intentionally outlive the session. The host may
                    # ignore application titles; no restoration capability is
                    # required to publish this user-selected effect policy.
                    output.set_title(value)
                elif effect.kind == "pwd":
                    uri = directory_uri(value)
                    if uri is not None:
                        output.write_raw(f"\x1b]7;{uri}\x1b\\")
                        self.cwd_changed = True
        self.effects.clear()

    def _queue_control(self, sequence):
        """Queue normalized adapter output for the sole terminal renderer."""
        if self.control_bytes + len(sequence) > MAX_EFFECT_BYTES:
            raise BufferError(i18n.get(Message.VT_EFFECT_LIMIT))
        self.control_output.append(sequence)
        self.control_bytes += len(sequence)
        self.app.invalidate()

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
            "viewport_active": frame.viewport_active,
            "viewport_offset": frame.viewport_offset,
            "scrollback_rows": frame.scrollback_rows,
            "mouse_reporting": self.vt.mouse_reporting,
            "mouse_policy": self.mouse_policy,
            "color_depth_policy": self.color_depth_policy,
            "color_depth": self.app.color_depth.value,
            "mouse_capture": self.mouse_captured,
            "child_pid": self.session.pid,
            "host": asdict(self.profile),
            "features": {
                name: asdict(state) for name, state in self.host.feature_status.items()
            },
            "graphics_reason": self.host.graphics_reason,
            "graphics_error": self.app.renderer.graphics_error,
            "engine": asdict(self.vt.engine),
            "input_routes": asdict(self.vt.input_routes),
            "native_processing_error": self.vt.processing_error,
            "native_log_counts": diagnostics.snapshot(),
            "protocol_errors": self.vt.protocol_errors,
            "protocol_failures": self.vt.protocol_failures,
            "framing_drops": self.vt.framing_drops,
            "host_adapter": self.host.adapter.name,
            "graphics_enabled": self.vt.graphics_enabled,
            "image_errors": self.vt.image_errors,
            "clipboard_policy": {
                "read": self.clipboard.allow_read,
                "write": self.clipboard.allow_write,
            },
            "clipboard_pending": self.clipboard.pending is not None,
            "clipboard_provider": self.clipboard.provider_name,
            "clipboard_failures": self.clipboard.failures,
            "key_mappings": self.host.key_mappings,
            "host_control_drops": self.host.dropped_controls,
            "plugin_errors": self.plugins.errors,
            "images": [asdict(image) for image in frame.images],
            "popups": [
                [p.xpos, p.ypos, p.width, p.height]
                for p in self.plugins.opaque_positions
            ],
        }
        temporary = self.dump_state.with_name(self.dump_state.name + ".next")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.dump_state)

    async def run(self) -> int:
        """Run the full-screen UI and restore the host terminal on every exit path."""
        try:
            self.plugins.load(
                self.plugin_names,
                directory=self.plugin_dir,
                library_dir=self.library_dir,
            )
            self.clipboard.seal()
            size = self.app.output.get_size()
            self.resize(max(1, size.columns), max(1, size.rows))
            return await self.app.run_async(
                pre_run=lambda: self.app.create_background_task(self._start()),
                set_exception_handler=False,
            )
        finally:
            self.clipboard.close()
            self.selection.close()
            if self.animation is not None:
                self.animation.cancel()
            if self.hold_timer is not None:
                self.hold_timer.cancel()
            try:
                await self.plugins.close()
            finally:
                try:
                    await self.session.close()
                finally:
                    self.clipboard.close()
                    try:
                        await self.clipboard.aclose()
                    finally:
                        self.vt.close()
                        try:
                            self.app.renderer.leave_input_modes()
                            self.app.renderer.restore_cursor()
                            self.app.renderer.restore_cursor_shape()
                            self.app.renderer.restore_metadata()
                            self.app.output.flush()
                        finally:
                            self.input.close()
