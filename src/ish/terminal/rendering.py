"""Compose native cells, PTK widgets and graphics through one PTK renderer.

The two small PTK overrides target the pinned prompt-toolkit version. VT cells
keep their native widths; PTK still owns layout, diffing, focus and output.
"""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache, partial

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Container, Window
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.output import ColorDepth, vt100
from prompt_toolkit.renderer import Renderer
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.styles.base import DEFAULT_ATTRS

from .graphics import (
    GraphicsCapabilityError,
    GraphicsLimitError,
    Rect,
    create_presenter,
)
from .vt import PALETTE_CLASS

LINK_END = "\x1b]8;;\x1b\\"
UNDERLINE_RESET = "\x1b[59m"
OVERLINE_RESET = "\x1b[55m"


class RestoringOutput:
    """Retain PTK output while accounting for the outer modes it changes."""

    def __init__(self, output, owner):
        """Do not allow constructor/reset cursor writes before the first probe."""
        self.output, self.owner = output, owner
        self.host = owner.host
        self.frame_size = None

    def get_size(self):
        """Use one host size throughout a paint, including layout and mouse mapping."""
        return self.frame_size or self.output.get_size()

    @property
    def ready(self):
        """Only the active renderer may alter cursors, including repeated resets."""
        renderer = getattr(getattr(self.owner, "app", None), "renderer", None)
        return self.owner.presentation_ready and getattr(
            renderer, "_screen_owned", False
        )

    def __getattr__(self, name):
        """Delegate terminal operations to the existing output implementation."""
        return getattr(self.output, name)

    def show_cursor(self):
        """PTK's show operation also resets cursor blinking."""
        if self.ready:
            self.host.change_modes(12, 25)
            self.output.show_cursor()

    def hide_cursor(self):
        """Account for cursor visibility without changing it during setup."""
        if self.ready:
            self.host.change_modes(25)
            self.output.hide_cursor()

    def reset_cursor_shape(self):
        """Only reset a cursor after the initial observation has finished."""
        if self.ready:
            self.output.reset_cursor_shape()

    def reset_cursor_key_mode(self):
        """PTK requests ordinary outer cursor keys, independently of the child."""
        self.host.change_modes(1)
        self.output.reset_cursor_key_mode()

    def enable_autowrap(self):
        """Retain the original wrap setting for cleanup."""
        self.host.change_modes(7)
        self.output.enable_autowrap()

    def disable_autowrap(self):
        """Retain the original wrap setting for cleanup."""
        self.host.change_modes(7)
        self.output.disable_autowrap()

    def enable_bracketed_paste(self):
        """Track PTK's paste framing mode."""
        self.host.change_modes(2004)
        self.output.enable_bracketed_paste()

    def disable_bracketed_paste(self):
        """Track PTK's paste framing cleanup."""
        self.host.change_modes(2004)
        self.output.disable_bracketed_paste()

    def enable_mouse_support(self):
        """Track the modes emitted by the pinned PTK VT output."""
        self.host.change_modes(1000, 1003, 1006, 1015)
        self.output.enable_mouse_support()

    def disable_mouse_support(self):
        """Track PTK's mouse cleanup before applying observed startup values."""
        self.host.change_modes(1000, 1003, 1006, 1015)
        self.output.disable_mouse_support()


class CellEscape(str):
    """Metadata applied after PTK sets attributes and before its cell text."""


class NativeChar(Char):
    """One native grapheme whose width is not recalculated by PTK."""

    __slots__ = ("format_key",)

    @classmethod
    def from_cell(cls, cell, formats, narrow):
        """Reuse immutable single-column glyphs only within this paint."""
        metadata = (
            cell.width,
            cell.underline,
            cell.underline_color,
            cell.hyperlink,
            cell.overline,
        )
        if cell.width != 1:
            # Keep every wide glyph's head and tails tied to its own location.
            return cls(cell, metadata, formats)
        key = (cell.style, cell.text, metadata)
        char = narrow.get(key)
        if char is None:
            char = narrow[key] = cls(cell, metadata, formats)
        return char

    def __init__(self, cell, key, formats):
        """Share style identities within this paint, including URI-only changes."""
        cached = formats.get(key)
        if cached is None:
            identity = repr(key).encode()
            suffix = hashlib.blake2s(identity, digest_size=8).hexdigest()
            cached = formats[key] = (key, suffix)
        self.format_key, suffix = cached
        # Char.__init__ applies editor visualisations (e.g. NBSP underline).
        # Native terminal cells already have their display meaning resolved.
        self.char = cell.text
        self.style = f"{cell.style} class:vt-{suffix}"
        self.width = cell.width

    def escape(self, profile, link, reset):
        """Encode host-supported metadata after PTK has selected the cell style."""
        _, underline, color, target, overline = self.format_key
        if (
            profile.hyperlinks
            and target
            and not any(ord(c) < 32 or ord(c) == 127 for c in target)
        ):
            link = f"\x1b]8;;{target}\x1b\\"
        payload = link + reset
        if profile.extended_styles:
            payload += f"\x1b[4:{underline}m"
        if profile.overline and overline:
            payload += "\x1b[53m"
        if profile.extended_styles and color is not None:
            red, green, blue = color
            payload += f"\x1b[58:2::{red}:{green}:{blue}m"
        return CellEscape(payload)


class NativeTail(Char):
    """A wide cell's continuation, tied to the exact painted head."""

    __slots__ = ("head",)

    def __init__(self, head):
        """Retain ownership so a popup cannot leave half a wide glyph visible."""
        self.char, self.style, self.width = "", head.style, 0
        self.head = head


class NativeWindow(Window):
    """A PTK window that copies physical VT cells without text reflow."""

    def __init__(self, content):
        """Keep standard PTK focus and window registration around the grid."""
        super().__init__(content, wrap_lines=False)
        self.position = None
        self._coordinates = lru_cache(maxsize=1)(self._grid_coordinates)

    @staticmethod
    def _grid_coordinates(x, y, width, height):
        """Map the unwrapped VT grid to PTK coordinates for one window geometry."""
        return {row: (row, 0) for row in range(height)}, {
            (row, col): (y + row, x + col)
            for row in range(height)
            for col in range(width)
        }

    def _copy_body(
        self,
        ui_content,
        new_screen,
        write_position,
        move_x,
        width,
        vertical_scroll=0,
        horizontal_scroll=0,
        **options,
    ):
        """Map native columns directly to PTK screen coordinates."""
        frame = self.content.frame
        self.position = write_position
        if frame is None or not frame.cells:
            return super()._copy_body(
                ui_content,
                new_screen,
                write_position,
                move_x,
                width,
                vertical_scroll,
                horizontal_scroll,
                **options,
            )
        xpos, ypos = write_position.xpos + move_x, write_position.ypos
        formats = {}
        narrow = {}
        for row, cells in enumerate(frame.cells[: write_position.height]):
            target = new_screen.data_buffer[ypos + row]
            for cell in cells:
                if cell.x >= width:
                    continue
                if cell.x + cell.width > width:
                    # A clipped wide grapheme must not overwrite the neighbour.
                    target[xpos + cell.x] = Char(" ", cell.style)
                else:
                    head = NativeChar.from_cell(cell, formats, narrow)
                    target[xpos + cell.x] = head
                    if cell.width == 2:
                        target[xpos + cell.x + 1] = NativeTail(head)
        if options.get("has_focus"):
            new_screen.set_cursor_position(
                self,
                Point(
                    xpos + min(width - 1, frame.cursor_x),
                    ypos + min(write_position.height - 1, frame.cursor_y),
                ),
            )
            new_screen.show_cursor = frame.cursor_visible and not options.get(
                "always_hide_cursor", False
            )
        new_screen.height = max(new_screen.height, ypos + write_position.height)
        return self._coordinates(
            xpos, ypos, width, min(len(frame.cells), write_position.height)
        )

    def _apply_style(self, new_screen, write_position, parent_style):
        """Native program colours are not overridden by PTK window styles."""


class PaneStyle(DynamicStyle):
    """Keep native indices distinct in PTK's attribute comparison and screen diff."""

    def get_attrs_for_style_str(self, style_str, default=DEFAULT_ATTRS):
        """Carry a native cell's palette references through PTK's style cache."""
        attrs = super().get_attrs_for_style_str(style_str, default)
        _, marker, suffix = style_str.partition(PALETTE_CLASS)
        if not marker:
            return attrs
        fg, sep, bg = suffix.partition(" ")[0].partition("_")
        if not sep or not all(
            not s or s.isascii() and s.isdigit() and len(s) <= 3 and int(s) < 256
            for s in (fg, bg)
        ):
            return attrs
        return attrs._replace(
            color=f"{fg}/{attrs.color}" if fg else attrs.color,
            bgcolor=f"{bg}/{attrs.bgcolor}" if bg else attrs.bgcolor,
        )


class TransactionOutput:
    """Delegate PTK output while publishing each composed frame in one flush."""

    PALETTE_LIMITS = {ColorDepth.DEPTH_4_BIT: 16, ColorDepth.DEPTH_8_BIT: 256}
    COLOR_CACHE_SIZE = 1024

    def __init__(self, output):
        """Wrap the existing output; do not create another stdout writer."""
        self.output = output
        self.active = False
        self.pending = None
        self._applied_format = None
        self._palette = None
        self._depth = None
        self._indices = {}
        self._nearest = None

    def __getattr__(self, name):
        """Reuse the complete PTK output implementation."""
        return getattr(self.output, name)

    def use_palette(self, palette, depth):
        """Index only observed host colors representable at the selected depth."""
        if palette is self._palette and depth == self._depth:
            return
        self._palette, self._depth = palette, depth
        self._indices.clear()
        colors = palette[: self.PALETTE_LIMITS.get(depth, 0)]
        for index, rgb in enumerate(colors):
            if rgb is not None:
                self._indices.setdefault(bytes(rgb).hex(), index)
        self._nearest = None
        if colors and None not in colors:
            self._nearest = lru_cache(maxsize=self.COLOR_CACHE_SIZE)(
                partial(self._nearest_index, colors)
            )

    @staticmethod
    def _nearest_index(palette, color, exclude=None):
        """Quantize a PTK RGB value against this host's complete indexed palette."""
        try:
            red, green, blue = bytes.fromhex(color)
        except ValueError:
            return None
        indices = range(len(palette))
        if exclude is not None:
            indices = (index for index in indices if palette[index] != palette[exclude])
        return min(
            indices,
            key=lambda index: (
                (palette[index][0] - red) ** 2
                + (palette[index][1] - green) ** 2
                + (palette[index][2] - blue) ** 2
            ),
            default=exclude,
        )

    def _color_index(self, color):
        """Preserve exact matches and leave named/default colors to PTK."""
        index = self._indices.get(color)
        if index is None and self._nearest is not None and color and len(color) == 6:
            return self._nearest(color)
        return index

    @staticmethod
    def _indexed_sgr(index, background):
        """Use ordinary ANSI for the first 16 colors, including on 4-bit hosts."""
        if index < 16:
            base = 30 if index < 8 else 90
            return str(base + (10 if background else 0) + index % 8)
        return f"{48 if background else 38};5;{index}"

    def _channel(self, color, depth):
        """Use the native index when representable, otherwise quantize its RGB."""
        if color and "/" in color:
            number, color = color.split("/", 1)
            index = int(number)
            if depth != ColorDepth.DEPTH_1_BIT and (
                depth != ColorDepth.DEPTH_4_BIT or index < 16
            ):
                return color, index
        return color, self._color_index(color)

    def set_attributes(self, attrs, color_depth):
        """Use the observed host palette for indexed colors, retaining PTK styling."""
        self._applied_format = None
        foreground, fg = self._channel(attrs.color, color_depth)
        background, bg = self._channel(attrs.bgcolor, color_depth)
        if foreground != attrs.color or background != attrs.bgcolor:
            attrs = attrs._replace(color=foreground, bgcolor=background)
        if fg is None and bg is None:
            self.output.set_attributes(attrs, color_depth)
            return
        if (
            self._depth == ColorDepth.DEPTH_4_BIT
            and self._nearest is not None
            and fg == bg
            and attrs.color != attrs.bgcolor
        ):
            # Like PTK's 16-color output, keep distinct requested colors visible.
            # Only change an approximation, retaining any exact theme color.
            if attrs.bgcolor in self._indices:
                fg = self._nearest(attrs.color, bg)
            else:
                bg = self._nearest(attrs.bgcolor, fg)
        self.output.set_attributes(
            attrs._replace(
                color=attrs.color if fg is None else None,
                bgcolor=attrs.bgcolor if bg is None else None,
            ),
            color_depth,
        )
        colors = []
        if fg is not None:
            colors.append(self._indexed_sgr(fg, False))
        if bg is not None:
            colors.append(self._indexed_sgr(bg, True))
        self.output.write_raw("\x1b[" + ";".join(colors) + "m")

    def reset_attributes(self):
        """PTK can reset SGR between cells, including when moving to another row."""
        self._applied_format = None
        self.output.reset_attributes()

    def write_raw(self, data):
        """Delay cell metadata until PTK's attribute change has completed."""
        if isinstance(data, CellEscape):
            self.pending = data
        else:
            self._applied_format = None
            self.output.write_raw(data)

    def write(self, data):
        """Apply metadata immediately before the corresponding visible text."""
        pending = self.pending
        if pending is not None:
            if pending != self._applied_format:
                self.output.write_raw(str(pending))
                self._applied_format = pending
            self.pending = None
        self.output.write(data)

    def flush(self):
        """Suppress intermediate PTK flushes inside a presentation."""
        if not self.active:
            self.output.flush()

    def finish(self):
        """Forget screen-local formatting even if a frame was interrupted."""
        self.pending = self._applied_format = None
        self.active = False
        self.output.flush()


class Composition(Container):
    """Finish PTK layout before choosing graphics visibility and cell metadata."""

    def __init__(self, content, owner):
        """Keep the layout tree available to PTK's normal traversal."""
        self.content, self.owner = content, owner

    def reset(self):
        """Reset the contained PTK layout."""
        self.content.reset()

    def preferred_width(self, max_available_width):
        """Reuse PTK width negotiation."""
        return self.content.preferred_width(max_available_width)

    def preferred_height(self, width, max_available_height):
        """Reuse PTK height negotiation."""
        return self.content.preferred_height(width, max_available_height)

    def get_children(self):
        """Expose the contained layout for focus and invalidation."""
        return [self.content]

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style="", **kwargs
    ):
        """Composite all floats before any output is emitted."""
        self.owner.plugins.begin_layout()
        self.content.write_to_screen(
            screen, mouse_handlers, write_position, parent_style, **kwargs
        )
        screen.draw_all_floats()
        self.owner.app.renderer.compose(screen)


class ScreenApplication(Application):
    """Keep ownership across resizes and retire mouse input when the screen closes."""

    def _on_resize(self):
        """Coalesce notifications, including a shrink and return to the old size."""
        self.renderer.resize_pending = True
        self.invalidate()

    def retire_mouse(self):
        """Discard reports already decoded for a closing popup or screen."""
        if not self.renderer._screen_owned:
            return
        queue = self.key_processor.input_queue
        retire = getattr(self.input, "retire_mouse", None)
        if retire is not None:
            retire()
        if (
            not self.renderer.mouse_support()
            and self.renderer.release_mouse()
            and self.is_done
        ):
            finish = getattr(self.input, "finish_mouse_release", None)
            if finish is not None:
                queue.extend(finish(self.renderer.output))
        if queue:
            kept = [key for key in queue if key.key != Keys.Vt100MouseEvent]
            queue.clear()
            queue.extend(kept)

    def exit(self, *args, **kwargs):
        """Switch off capture at the close decision, before PTK's final repaint."""
        super().exit(*args, **kwargs)
        self.retire_mouse()


class TerminalApplication(ScreenApplication):
    """Publish matching VT and PTY sizes together when the outer viewport changes."""

    CHILD_PAINT_WAIT = 0.16
    RESIZE_BURST_SECONDS = 0.012

    def __init__(self, *, prepare_resize, **kwargs):
        """Leave initial sizing and normal keyboard-driven paints immediate."""
        self._prepare_resize = prepare_resize
        self._resize_handle = self._paint_timer = None
        self._prepared_size = None
        self._waiting_for_child = False
        self._resize_frame_ready = False
        self.painting_prepared = False
        super().__init__(**kwargs)

    def _queue_resize(self):
        """Run at the first event-loop opportunity and read the latest size there."""
        if self._resize_handle is None:
            self._resize_handle = asyncio.get_running_loop().call_soon(
                self._apply_resize
            )

    def _on_resize(self):
        """Do not wait for an ordinary PTK redraw to notify a resizing child."""
        if (
            not self.is_running
            or self._running_in_terminal
            or self.renderer._last_size is None
        ):
            return super()._on_resize()
        self.renderer.resize_pending = True
        if not self._waiting_for_child and not self._resize_frame_ready:
            self._queue_resize()

    def _apply_resize(self):
        """Notify the child before composing any cells at the new geometry."""
        self._resize_handle = None
        if self.is_done or not self.is_running or self._running_in_terminal:
            return
        size = self.output.get_size()
        if self._paint_timer is not None:
            self._paint_timer.cancel()
            self._paint_timer = None
        self._prepared_size = size
        self._waiting_for_child, reading = self._prepare_resize(size)
        if self._waiting_for_child:
            self._allow_child_paint(reading)
        else:
            self._paint_resize()

    def _allow_child_paint(self, reading):
        """Start the static-screen fallback only while the PTY reader can deliver output."""
        if reading and self._paint_timer is None:
            self._paint_timer = asyncio.get_running_loop().call_later(
                self.CHILD_PAINT_WAIT, self._paint_resize
            )
        elif not reading and self._paint_timer is not None:
            self._paint_timer.cancel()
            self._paint_timer = None

    def _paint_resize(self):
        """Present the prepared grid once before negotiating another size."""
        self._paint_timer = None
        self._waiting_for_child = False
        self._resize_frame_ready = True
        self._redraw()

    def resize_output_received(self, *, frame_closed):
        """Collect an unframed resize burst once; a declared frame end can paint now."""
        if not self._waiting_for_child:
            if not self._resize_frame_ready or not frame_closed:
                return self._resize_frame_ready
        if self._paint_timer is not None:
            self._paint_timer.cancel()
        self._waiting_for_child = False
        self._resize_frame_ready = True
        loop = asyncio.get_running_loop()
        if frame_closed:
            self._paint_timer = loop.call_soon(self._paint_resize)
        else:
            self._paint_timer = loop.call_later(
                self.RESIZE_BURST_SECONDS, self._paint_resize
            )
        return True

    def _redraw(self, render_as_done=False):
        """Measure once; both a SIGWINCH and an output-driven paint can see a resize first."""
        if render_as_done:
            for handle in (self._resize_handle, self._paint_timer):
                if handle is not None:
                    handle.cancel()
            self._resize_handle = self._paint_timer = self._prepared_size = None
            self._waiting_for_child = False
            self._resize_frame_ready = False
        elif self._running_in_terminal or not self.is_running:
            return super()._redraw(render_as_done)
        elif self._resize_frame_ready and self._paint_timer is not None:
            # An ordinary invalidate must not expose the middle of this burst.
            return
        size = self.output.get_size()
        if self.renderer._last_size is not None and not render_as_done:
            if self._waiting_for_child:
                if self._paint_timer is None:
                    _, reading = self._prepare_resize(self._prepared_size)
                    self._allow_child_paint(reading)
                return
            if size != (self._prepared_size or self.renderer._last_size):
                self._queue_resize()
                if not self._resize_frame_ready:
                    return
            elif self._resize_handle is not None and not self._resize_frame_ready:
                return
        self.painting_prepared = self._resize_frame_ready
        self._resize_frame_ready = False
        self.output.frame_size = size
        try:
            super()._redraw(render_as_done)
        finally:
            self.output.frame_size = None
            self.painting_prepared = False


class RestoringRenderer(Renderer):
    """Share PTK screen ownership and observed-state restoration across UIs."""

    SYNC_START = "\x1b[?2026h"
    SYNC_END = "\x1b[?2026l"

    def __init__(self, owner, previous, *, output=None, sync_resize=False):
        """Keep one broker as the source of initial host state."""
        self.owner = owner
        self.resize_pending = False
        self._sync_resize = sync_resize
        self._screen_owned = False
        super().__init__(
            previous.style,
            previous.output if output is None else output,
            full_screen=True,
            mouse_support=previous.mouse_support
            & Condition(lambda: not owner.app.is_done),
            cpr_not_supported_callback=previous.cpr_not_supported_callback,
        )

    def acquire_screen(self):
        """Save the primary cursor before normalizing inherited host modes."""
        if self._screen_owned:
            return
        self._screen_owned = True
        self.owner.host.change_attributes()
        if not self._in_alternate_screen:
            self.output.enter_alternate_screen()
            self._in_alternate_screen = True
        self._cursor_key_mode_reset = False
        self.output.write_raw(self.owner.host.enter_modes())

    def restore_cursor_shape(self):
        """Apply the observed cursor after PTK resets its own cursor changes."""
        if self._screen_owned:
            self.output.write_raw(self.owner.host.restore_cursor_shape())

    def release_mouse(self):
        """Flush tracking shutdown without releasing the rest of the screen."""
        if self._mouse_support_enabled:
            self.output.disable_mouse_support()
            self._mouse_support_enabled = False
            self.output.write_raw(self.owner.host.mouse_mode(False))
            self.output.flush()
            return True
        return False

    def repaint_viewport(self):
        """Anchor PTK's relative diff after a host shrink, clamp or reflow."""
        self.output.cursor_goto(1, 1)
        self._cursor_pos = Point(0, 0)
        self._last_screen = None

    def render(self, app, layout, is_done=False):
        """Paint one geometry without releasing host modes or publishing an empty frame."""
        if not self.owner.presentation_ready:
            return
        self.acquire_screen()
        destination = app.output
        size = destination.get_size()
        resized = (
            not is_done
            and self._last_size is not None
            and (self.resize_pending or self._last_size != size)
        )
        self.resize_pending = False
        synchronize = (
            resized and self._sync_resize and self.owner.host.can_open_sync_frame
        )
        destination.frame_size = size
        try:
            if synchronize:
                self.output.write_raw(self.SYNC_START)
            if resized:
                self.repaint_viewport()
            super().render(app, layout, is_done=is_done)
        finally:
            destination.frame_size = None
            if synchronize:
                self.output.write_raw(self.SYNC_END)
                self.output.flush()

    def reset(self, *args, **kwargs):
        """Restore owned state on exit and PTK's external-command suspension."""
        if not self._screen_owned:
            return super().reset(*args, **kwargs)
        if self.owner.app.is_done:
            # Covers EOF/cancellation and any reports PTK read before this last
            # reset. Leave the descriptor and the restored caller's input alone.
            self.owner.app.retire_mouse()
        self.output.write_raw(self.owner.host.restore_modes(before_screen=True))
        super().reset(*args, **kwargs)
        self.restore_cursor_shape()
        self.output.write_raw(self.owner.host.restore_modes())
        self.output.write_raw(self.owner.host.restore_attributes())
        self._screen_owned = False
        self.output.flush()


class TerminalRenderer(RestoringRenderer):
    """Extend the pinned PTK renderer with native metadata and graphics."""

    ERASE_RECTANGLE = "\x1b[{top};{left};{bottom};{right}$z"
    FORMAT_RESETS = (
        ("hyperlinks", LINK_END),
        ("extended_styles", UNDERLINE_RESET),
        ("overline", OVERLINE_RESET),
    )
    MAX_ERROR_CHARS = 256
    MIN_STYLE_CACHE_SIZE = 4096
    MAX_GRAPHICS_FAILURES = 3

    def __init__(self, owner, previous):
        """Preserve PTK's style, mode and CPR configuration."""
        self.owner = owner
        self.graphics = create_presenter(None)
        self._graphics_background = None
        self.transaction = TransactionOutput(previous.output)
        self.after_text = ""
        self.finishing = False
        self.graphics_error = None
        self.graphics_failure_count = 0
        self._host_failures = 0
        self._rejected_content = None
        self._limited_scene = None
        self._focus_enabled = False
        self._cursor_color = None
        self._cursor_changed = False
        self._format_used = set()
        super().__init__(owner, previous, output=self.transaction)
        self.style = PaneStyle(lambda: previous.style)

    def compose(self, screen):
        """Attach metadata to visible cells and plan graphics under the floats."""
        profile = self.owner.profile
        protocol = profile.image_protocol
        background = profile.appearance.background
        if (
            protocol != self.graphics.protocol
            or background != self._graphics_background
        ):
            self.clear_graphics()
            self.graphics = create_presenter(protocol, background=background)
            self._graphics_background = background
            self._host_failures = 0
            self._rejected_content = self._limited_scene = None
            self.graphics_error = None
        for row in screen.data_buffer.values():
            for x, char in tuple(row.items()):
                if isinstance(char, NativeChar) and char.width > 1:
                    if any(
                        not isinstance(row[x + step], NativeTail)
                        or row[x + step].head is not char
                        for step in range(1, char.width)
                    ):
                        row[x] = Char(" ", char.style)
            for x, char in tuple(row.items()):
                if isinstance(char, NativeTail) and not any(
                    row[x - step] is char.head for step in range(1, char.head.width)
                ):
                    row[x] = Char(" ", char.style)
        self._format_cells(screen, profile)
        frame = self.owner.control.frame
        position = self.owner.window.position
        if self.finishing or frame is None or position is None or protocol is None:
            self.clear_graphics()
            return
        cw, ch = profile.cell_width, profile.cell_height
        origin = (position.xpos * cw, position.ypos * ch)
        clip = Rect(*origin, position.width * cw, position.height * ch)
        occluders = tuple(
            Rect(p.xpos * cw, p.ypos * ch, p.width * cw, p.height * ch)
            for p in self.owner.plugins.opaque_positions
        )
        content = self._image_content(frame)
        viewport = {"origin": origin, "clip": clip, "occluders": occluders}
        if (
            self._host_failures >= self.MAX_GRAPHICS_FAILURES
            or content == self._rejected_content
            or (
                self._limited_scene is not None
                and (content, self.graphics.scene_key(frame, **viewport))
                == self._limited_scene
            )
        ):
            self.clear_graphics()
            return
        try:
            plan = self.graphics.plan(frame, self.owner.vt.image, **viewport)
        except (GraphicsLimitError, GraphicsCapabilityError) as exc:
            # Resource policy must not terminate an otherwise usable shell.
            # Structural/native errors still propagate instead of being hidden.
            self.graphics_error = str(exc)
            self.graphics_failure_count += 1
            self._limited_scene = content, self.graphics.scene_key(frame, **viewport)
            self.clear_graphics()
            return
        self.graphics_error = None
        self._limited_scene = None
        self.output.write_raw(plan.before_text)
        self.after_text = plan.after_text
        if plan.repaint:
            self.invalidate_screen()

    def _format_cells(self, screen, profile):
        """Encode each distinct format once after PTK has finished its overlays."""
        if not (profile.hyperlinks or profile.extended_styles or profile.overline):
            # A painted cell still supersedes any zero-width escape from a widget.
            for y, escapes in screen.zero_width_escapes.items():
                for x in escapes.keys() & screen.data_buffer[y].keys():
                    del escapes[x]
            return
        reset = (UNDERLINE_RESET if profile.extended_styles else "") + (
            OVERLINE_RESET if profile.overline else ""
        )
        default_link = LINK_END if profile.hyperlinks else ""
        widget = {}
        native = {}
        for y, row in screen.data_buffer.items():
            escapes = screen.zero_width_escapes[y]
            for x, char in row.items():
                if isinstance(char, NativeChar):
                    key = char.format_key
                    if key not in native:
                        native[key] = char.escape(profile, default_link, reset)
                    escapes[x] = native[key]
                else:
                    if char.style not in widget:
                        payload = default_link + reset
                        if profile.extended_styles:
                            underline = self._attrs_for_style[char.style].underline
                            payload += f"\x1b[4:{int(underline)}m"
                        widget[char.style] = CellEscape(payload)
                    escapes[x] = widget[char.style]

    def clear_graphics(self):
        """Restore text when a raster backend erases its previously occupied cells."""
        cleanup = self.graphics.close()
        if cleanup:
            self.output.write_raw(cleanup)
            self.invalidate_screen()
        self.after_text = ""

    def clear_exposed(self, size):
        """Queue default-background gutters outside the last painted TUI bounds."""
        old = self._last_size
        if old is None or not self._screen_owned:
            return False
        regions = []
        if size.columns > old.columns:
            regions.append((1, old.columns + 1, min(old.rows, size.rows), size.columns))
        if size.rows > old.rows:
            regions.append((old.rows + 1, 1, size.rows, size.columns))
        if not regions:
            return False
        self.output.reset_attributes()
        self._last_style = None
        for top, left, bottom, right in regions:
            self.output.write_raw(
                self.ERASE_RECTANGLE.format(
                    top=top, left=left, bottom=bottom, right=right
                )
            )
        return True

    def repaint_viewport(self):
        """Rebuild host image placements together with the text in the resized frame."""
        self.clear_graphics()
        super().repaint_viewport()

    @staticmethod
    def _image_content(frame):
        """Identify a new native image generation or actual geometry change."""
        return (
            frame.image_generation,
            frame.cols,
            frame.rows,
            frame.cell_width,
            frame.cell_height,
        )

    def reject_image(self, image_id, message):
        """Bound recovery of owned host assets without retrying every repaint."""
        if not self.graphics.invalidate(image_id):
            return False
        self._host_failures += 1
        self.graphics_failure_count += 1
        self.graphics_error = message[: self.MAX_ERROR_CHARS]
        frame = self.owner.control.frame
        if frame is not None:
            self._rejected_content = self._image_content(frame)
        return True

    def invalidate_screen(self):
        """Repaint after optional probes or an external presentation reset."""
        self._last_screen = None

    @property
    def mouse_captured(self):
        """Read tracking ownership from PTK's renderer, its only mutable owner."""
        return self._mouse_support_enabled

    def enter_input_modes(self):
        """Enable owned modes only after PTK has entered its alternate screen."""
        if not self._focus_enabled:
            self.owner.host.change_modes(1004)
            self.output.write_raw("\x1b[?1004h")
            self._focus_enabled = True
        self.output.write_raw(
            self.owner.host.enter_input_modes(mouse=self._mouse_support_enabled)
        )
        self.output.write_raw(self.owner.host.enter_metadata_modes())

    def leave_input_modes(self):
        """Pop screen-local keyboard state before PTK leaves that screen."""
        self.output.write_raw(self.owner.host.leave_input_modes())
        if self._focus_enabled:
            self.output.write_raw("\x1b[?1004l")
            self._focus_enabled = False

    def cursor_color(self):
        """Present a child cursor override only when its host state is restorable."""
        if not self.owner.profile.cursor_color_control:
            return
        frame = self.owner.control.frame
        color = (
            frame.cursor_color
            if frame is not None and not self.owner.plugins.popups
            else None
        )
        if color is None:
            self.restore_cursor()
        elif color != self._cursor_color:
            self.output.write_raw(self.owner.host.set_cursor_color(color))
            self._cursor_color = color
            self._cursor_changed = True

    def restore_cursor(self):
        """Restore the observed initial cursor, including Kitty's dynamic color."""
        if not self._cursor_changed:
            return
        self.output.write_raw(self.owner.host.restore_cursor())
        self._cursor_color = None
        self._cursor_changed = False

    def restore_metadata(self):
        """Restore owned presentation even when the final render raised."""
        self.owner.restore_cwd(self.output)
        self.output.write_raw(self.owner.host.leave_metadata_modes())

    def format_reset(self):
        """Reset only enabled or previously written optional cell formats."""
        self._format_used.update(
            name for name, _ in self.FORMAT_RESETS if getattr(self.owner.profile, name)
        )
        return "".join(
            sequence
            for name, sequence in self.FORMAT_RESETS
            if name in self._format_used
        )

    def render(self, app, layout, is_done=False):
        """Publish text, images and effects in one ordered presentation."""
        if not self.owner.presentation_ready:
            return
        self.acquire_screen()
        self.enter_input_modes()
        self.finishing = is_done
        self.transaction.use_palette(
            self.owner.profile.appearance.palette, app.color_depth
        )
        synchronized = self.owner.profile.synchronized_output and not is_done
        self.transaction.active = True
        try:
            if self._mouse_support_enabled and not self.owner.mouse_enabled:
                self.owner.release_mouse_capture(clear=True)
            if synchronized:
                self.owner.host.change_modes(2026)
                self.output.write_raw(self.SYNC_START)
            self.output.write_raw(self.format_reset())
            super().render(app, layout, is_done=is_done)
            if not is_done:
                self.output.write_raw(
                    self.owner.host.mouse_mode(self._mouse_support_enabled)
                )
                self.output.write_raw(self.format_reset())
                self.output.write_raw(self.after_text)
                self.cursor_color()
            self.owner.publish_effects(self.output)
            if is_done:
                self.restore_metadata()
        finally:
            if synchronized:
                self.output.write_raw(self.SYNC_END)
            self.transaction.finish()
            self._trim_styles()

    def _trim_styles(self):
        """Bound PTK's derived lookup tables without invalidating the screen diff."""
        cols, rows = self.owner.size
        # Keep room for the old and new screen even when every cell has a
        # different color. PTK repopulates each cleared dict on its next lookup.
        limit = max(self.MIN_STYLE_CACHE_SIZE, 2 * cols * rows)
        caches = [self._attrs_for_style, self._style_string_has_style]
        output_caches = getattr(self.transaction.output, "_escape_code_caches", None)
        if output_caches is not None:
            caches.extend(output_caches.values())
            # Pinned PTK's indexed-color fallbacks share these memo tables. The
            # quantization palette itself is separate and must remain intact.
            caches.extend(
                (
                    vt100._16_fg_colors._cache,
                    vt100._16_bg_colors._cache,
                    vt100._256_colors,
                )
            )
        for cache in caches:
            if cache is not None and len(cache) > limit:
                cache.clear()

    def reset(self, *args, **kwargs):
        """Delete only this renderer's graphics before restoring the terminal."""
        if not self._screen_owned:
            return super().reset(*args, **kwargs)
        if getattr(self.owner, "selection", None) is not None:
            self.owner.release_mouse_capture(clear=True)
        self.leave_input_modes()
        self.restore_cursor()
        if not self.finishing:
            self.restore_metadata()
        self.transaction.output.write_raw(self.graphics.close() + self.format_reset())
        super().reset(*args, **kwargs)
        self._attrs_for_style = self._style_string_has_style = None
        self._format_used.clear()
        self.transaction.output.flush()
