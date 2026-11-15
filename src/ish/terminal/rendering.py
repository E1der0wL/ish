"""Compose native cells, PTK widgets and graphics through one PTK renderer.

The two small PTK overrides target the pinned prompt-toolkit version. VT cells
keep their native widths; PTK still owns layout, diffing, focus and output.
"""

from __future__ import annotations

import hashlib

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import Container, Window
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.renderer import Renderer

from .graphics import (
    GraphicsCapabilityError,
    GraphicsLimitError,
    Rect,
    create_presenter,
)

LINK_END = "\x1b]8;;\x1b\\"
UNDERLINE_RESET = "\x1b[59m"
OVERLINE_RESET = "\x1b[55m"
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
MAX_GRAPHICS_FAILURES = 3


class CellEscape(str):
    """Metadata applied after PTK sets attributes and before its cell text."""


class NativeChar(Char):
    """One native grapheme whose width is not recalculated by PTK."""

    __slots__ = ("cell",)

    def __init__(self, cell):
        """Retain metadata in the same object that participates in PTK diffing."""
        identity = repr(
            (
                cell.width,
                cell.underline,
                cell.underline_color,
                cell.hyperlink,
                cell.overline,
            )
        )
        suffix = hashlib.blake2s(identity.encode(), digest_size=8).hexdigest()
        # Char.__init__ applies editor visualisations (e.g. NBSP underline).
        # Native terminal cells already have their display meaning resolved.
        self.char = cell.text
        self.style = f"{cell.style} class:vt-{suffix}"
        self.width = cell.width
        self.cell = cell


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
        visible, mapping = {}, {}
        for row, cells in enumerate(frame.cells[: write_position.height]):
            visible[row] = (row, 0)
            target = new_screen.data_buffer[ypos + row]
            for cell in cells:
                if cell.x >= width:
                    continue
                if cell.x + cell.width > width:
                    # A clipped wide grapheme must not overwrite the neighbour.
                    target[xpos + cell.x] = Char(" ", cell.style)
                else:
                    head = NativeChar(cell)
                    target[xpos + cell.x] = head
                    for tail in range(1, cell.width):
                        target[xpos + cell.x + tail] = NativeTail(head)
            for col in range(width):
                mapping[row, col] = (ypos + row, xpos + col)
        if options.get("has_focus"):
            new_screen.set_cursor_position(
                self, Point(xpos + frame.cursor_x, ypos + frame.cursor_y)
            )
            new_screen.show_cursor = frame.cursor_visible and not options.get(
                "always_hide_cursor", False
            )
        new_screen.height = max(new_screen.height, ypos + write_position.height)
        return visible, mapping

    def _apply_style(self, new_screen, write_position, parent_style):
        """Native program colours are not overridden by PTK window styles."""


class TransactionOutput:
    """Delegate PTK output while publishing each composed frame in one flush."""

    def __init__(self, output):
        """Wrap the existing output; do not create another stdout writer."""
        self.output = output
        self.active = False
        self.pending = None

    def __getattr__(self, name):
        """Reuse the complete PTK output implementation."""
        return getattr(self.output, name)

    def write_raw(self, data):
        """Delay cell metadata until PTK's attribute change has completed."""
        if isinstance(data, CellEscape):
            self.pending = data
        else:
            self.output.write_raw(data)

    def write(self, data):
        """Apply metadata immediately before the corresponding visible text."""
        if self.pending is not None:
            self.output.write_raw(str(self.pending))
            self.pending = None
        self.output.write(data)

    def flush(self):
        """Suppress intermediate PTK flushes inside a presentation."""
        if not self.active:
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


class TerminalRenderer(Renderer):
    """Extend the pinned PTK renderer with native metadata and graphics."""

    def __init__(self, owner, previous):
        """Preserve PTK's style, mode and CPR configuration."""
        self.owner = owner
        self.graphics = create_presenter(None)
        self.transaction = TransactionOutput(previous.output)
        self.after_text = ""
        self.finishing = False
        self.graphics_error = None
        self._host_failures = 0
        self._rejected_content = None
        self._limited_scene = None
        self._focus_enabled = False
        self._cursor_color = None
        self._cursor_changed = False
        super().__init__(
            previous.style,
            self.transaction,
            full_screen=True,
            mouse_support=previous.mouse_support,
            cpr_not_supported_callback=previous.cpr_not_supported_callback,
        )

    def compose(self, screen):
        """Attach metadata to visible cells and plan graphics under the floats."""
        profile = self.owner.profile
        protocol = profile.image_protocol
        if protocol != self.graphics.protocol:
            self.clear_graphics()
            self.graphics = create_presenter(protocol)
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
        for y, row in screen.data_buffer.items():
            for x, char in row.items():
                payload = LINK_END if profile.hyperlinks else ""
                if getattr(profile, "extended_styles", False):
                    payload += UNDERLINE_RESET + OVERLINE_RESET
                elif profile.overline:
                    payload += OVERLINE_RESET
                if isinstance(char, NativeChar):
                    cell = char.cell
                    if (
                        profile.hyperlinks
                        and cell.hyperlink
                        and not any(
                            ord(c) < 32 or ord(c) == 127 for c in cell.hyperlink
                        )
                    ):
                        payload = f"\x1b]8;;{cell.hyperlink}\x1b\\" + (
                            UNDERLINE_RESET + OVERLINE_RESET
                            if getattr(profile, "extended_styles", False)
                            else ""
                        )
                    if getattr(profile, "extended_styles", False):
                        payload += f"\x1b[4:{cell.underline}m"
                    if profile.overline and cell.overline:
                        payload += "\x1b[53m"
                    if cell.underline_color is not None and getattr(
                        profile, "extended_styles", False
                    ):
                        red, green, blue = cell.underline_color
                        payload += f"\x1b[58:2::{red}:{green}:{blue}m"
                else:
                    if getattr(profile, "extended_styles", False):
                        underline = self._attrs_for_style[char.style].underline
                        payload += f"\x1b[4:{int(underline)}m"
                screen.zero_width_escapes[y][x] = CellEscape(payload)
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
        scene = (
            content,
            self.graphics.scene_key(
                frame, origin=origin, clip=clip, occluders=occluders
            ),
        )
        if (
            self._host_failures >= MAX_GRAPHICS_FAILURES
            or content == self._rejected_content
            or scene == self._limited_scene
        ):
            self.clear_graphics()
            return
        try:
            plan = self.graphics.plan(
                frame,
                self.owner.vt.image,
                origin=origin,
                clip=clip,
                occluders=occluders,
            )
        except (GraphicsLimitError, GraphicsCapabilityError) as exc:
            # Resource policy must not terminate an otherwise usable shell.
            # Structural/native errors still propagate instead of being hidden.
            self.graphics_error = str(exc)
            self._limited_scene = scene
            self.clear_graphics()
            return
        self.graphics_error = None
        self._limited_scene = None
        self.output.write_raw(plan.before_text)
        self.after_text = plan.after_text
        if plan.repaint:
            self.invalidate_screen()

    def clear_graphics(self):
        """Restore text when a raster backend erases its previously occupied cells."""
        cleanup = self.graphics.close()
        if cleanup:
            self.output.write_raw(cleanup)
            self.invalidate_screen()
        self.after_text = ""

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
        self.graphics_error = message[:256]
        frame = self.owner.control.frame
        if frame is not None:
            self._rejected_content = self._image_content(frame)
        return True

    def invalidate_screen(self):
        """Repaint after optional probes or an external presentation reset."""
        self._last_screen = None

    def enter_input_modes(self):
        """Enable owned modes only after PTK has entered its alternate screen."""
        if not self._focus_enabled:
            self.output.write_raw("\x1b[?1004h")
            self._focus_enabled = True
        self.output.write_raw(self.owner.host.enter_input_modes())
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

    def render(self, app, layout, is_done=False):
        """Publish text, images and effects in one ordered presentation."""
        self.finishing = is_done
        synchronized = self.owner.profile.synchronized_output and not is_done
        self.transaction.active = True
        try:
            if synchronized:
                self.output.write_raw(SYNC_START)
            self.output.write_raw(LINK_END)
            super().render(app, layout, is_done=is_done)
            if not is_done:
                self.output.write_raw(LINK_END + UNDERLINE_RESET + OVERLINE_RESET)
                self.output.write_raw(self.after_text)
                self.cursor_color()
            self.owner.publish_effects(self.output)
            if is_done:
                self.restore_metadata()
        finally:
            if synchronized:
                self.output.write_raw(SYNC_END)
            self.transaction.pending = None
            self.transaction.active = False
            self.transaction.output.flush()

    def reset(self, *args, **kwargs):
        """Delete only this renderer's graphics before restoring the terminal."""
        self.leave_input_modes()
        self.restore_cursor()
        if not self.finishing:
            self.restore_metadata()
        self.transaction.output.write_raw(
            self.graphics.close() + LINK_END + UNDERLINE_RESET + OVERLINE_RESET
        )
        super().reset(*args, **kwargs)
