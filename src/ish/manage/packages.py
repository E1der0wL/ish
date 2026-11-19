"""Render the visible rows of a package catalog with PTK's checkbox key bindings."""

import asyncio
from concurrent.futures import Future
from threading import Event, Thread

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import CheckboxList

from ish.plugin.manager import PluginManager
from ish.runtime.settings import setting_path

from .scroll import FormPane


class CatalogReader:
    """Read one directory at a time, publishing only the newest requested result."""

    def __init__(self, publish):
        """Deliver completed metadata on the application's event loop."""
        self.publish = publish
        self.request = None
        self.task = None

    def cancel(self):
        """Retire queued results and let the worker stop between package reads."""
        if self.request is not None:
            self.request[-1].set()
            self.request = None

    def start(self, source, cache, app):
        """Coalesce replacements while a blocking filesystem call finishes."""
        self.cancel()
        self.request = source, cache, Event()
        if self.task is None or self.task.done():
            self.task = app.create_background_task(self._drain())

    async def _drain(self):
        try:
            while self.request is not None:
                request = self.request
                future = Future()
                # A stalled mount must never keep the CLI alive after PTK exits.
                Thread(
                    target=self._complete, args=(future, *request), daemon=True
                ).start()
                try:
                    directory, candidates = await asyncio.wrap_future(future)
                    error = None
                except Exception as exc:
                    directory, candidates, error = None, (), exc
                if self.request is request:
                    self.request = None
                    self.publish(directory, candidates, error)
        finally:
            self.cancel()
            self.task = None

    @classmethod
    def _complete(cls, future, *request):
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = cls.read(*request)
        except Exception as error:
            future.set_exception(error)
        else:
            future.set_result(result)

    @staticmethod
    def read(source, cache, stop=None):
        """Resolve the canonical path and read candidate metadata on the same worker."""
        if stop is not None and stop.is_set():
            return None, ()
        value, base, default = source
        directory = setting_path(value or None, base, name="plugin_dir") or default
        if cache is not None and cache[0] == directory:
            return cache
        manager = PluginManager(script_dir=directory)
        return directory, tuple(
            manager.iter_candidates(cancelled=stop.is_set if stop else None)
        )


class PackageList(CheckboxList):
    """Share the ordered package selection between both extension pages."""

    def __init__(self, rows, selection):
        """Let PTK position its cursor at the first visible selected package."""
        visible = {name for name, _ in rows}
        first = next((name for name in selection if name in visible), None)
        super().__init__(rows, default_values=[first] if first is not None else [])
        self.current_values = selection
        self.control = PackageRows(self, self.control.get_key_bindings())
        self.window = PackageWindow(self.control, self.container_style)


class PackageRows(UIControl):
    """Create checkbox text only for the indexed lines that PTK requests."""

    def __init__(self, owner, keys):
        """Keep the checkbox widget in charge of native selection and navigation."""
        self.owner = owner
        self.keys = keys

    def is_focusable(self):
        """Receive keyboard navigation when the user enters the list."""
        return True

    def get_key_bindings(self):
        """Reuse PTK's checkbox keys, including type-to-find."""
        return self.keys

    @property
    def selected_row(self):
        """Return the logical item highlighted by PTK's navigation."""
        return self.owner._selected_index

    def create_content(self, width, height):
        """Build only the requested lines, retaining absolute logical row indices."""
        owner = self.owner
        checked = set(owner.current_values)
        selected = self.selected_row

        def line(index):
            name, title = owner.values[index]
            style = owner.checked_style if name in checked else ""
            if index == selected:
                style += " " + owner.selected_style
            mark = owner.select_character if name in checked else " "
            box = owner.open_character + mark + owner.close_character
            return [(style, box), (style + " " + owner.default_style, " " + title)]

        # The parent FormPane can scroll the cursor offscreen with Alt or the mouse.
        # Keep Window's own scroll within the assigned slice in that case.
        first = owner.window.first
        cursor = max(first, min(selected, first + height - 1))
        return UIContent(
            get_line=line,
            line_count=len(owner.values),
            cursor_position=Point(x=1, y=cursor),
            show_cursor=cursor == selected,
        )

    def mouse_handler(self, event):
        """Toggle the package at the logical row reported by PTK's window."""
        if event.event_type == MouseEventType.MOUSE_UP:
            self.owner._selected_index = event.position.y
            self.owner._handle_enter()
            return None
        return NotImplemented


class PackageWindow(Window):
    """Expose full list geometry to the form while drawing a single viewport."""

    def __init__(self, control, style):
        """Follow the scroll position assigned by the enclosing form."""
        self.first = 0
        super().__init__(
            control,
            style=style,
            height=len(control.owner.values),
            dont_extend_height=True,
            get_vertical_scroll=lambda _: self.first,
        )

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
    ):
        """Announce the true cursor before the form computes its final scroll offset."""
        args = parent_style, erase_bg, z_index
        pane = FormPane.current()
        if pane is None:
            self.first = 0
            return super().write_to_screen(
                screen, mouse_handlers, write_position, *args
            )
        pos = write_position
        focused = get_app().layout.current_window is self
        cursor = Point(x=pos.xpos + 1, y=pos.ypos + self.content.selected_row)
        screen.visible_windows_to_write_positions[self] = pos
        if focused:
            screen.set_cursor_position(self, cursor)
        paint = super().write_to_screen

        def draw(rows):
            top = max(rows.start, pos.ypos)
            end = min(rows.stop, pos.ypos + pos.height)
            if end > top:
                self.first = top - pos.ypos
                area = WritePosition(pos.xpos, top, pos.width, end - top)
                paint(screen, mouse_handlers, area, *args)
                screen.visible_windows_to_write_positions[self] = pos
                if focused:
                    screen.set_cursor_position(self, cursor)

        pane.paint_viewport(draw)
