"""Opt-in PTK surfaces with explicit lifetime and focus ownership.

Plugins receive UI operations, not a second terminal writer or a command
dispatcher. No plugin, surface, or shortcut is installed by default.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from dataclasses import dataclass
from functools import wraps
from itertools import count

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Container,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
    to_container,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.screen import WritePosition

MAX_POPUPS = 16
MAX_BAR_ROWS = 8
MAX_ERRORS = 16


class Surface(Container):
    """A plugin's opaque rectangle, recorded by the same layout that paints it."""

    def __init__(self, content, owner):
        """Reuse any PTK container without giving it access to terminal output."""
        self.content, self.owner = to_container(content), owner

    def reset(self):
        """Reset the plugin's own widgets."""
        self.content.reset()

    def preferred_width(self, max_available_width):
        """Delegate width negotiation to PTK."""
        return self.content.preferred_width(max_available_width)

    def preferred_height(self, width, max_available_height):
        """Delegate height negotiation to PTK."""
        return self.content.preferred_height(width, max_available_height)

    def get_children(self):
        """Allow PTK to traverse widgets for focus and key bindings."""
        return [self.content]

    def is_modal(self):
        """Keep popup widget keys inside this surface."""
        return True

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style="", **kwargs
    ):
        """Record the exact painted bounds for graphics occlusion."""
        self.owner.opaque_positions.append(write_position)
        self.content.write_to_screen(
            screen, mouse_handlers, write_position, parent_style, **kwargs
        )


@dataclass
class Popup:
    """A single opt-in surface and its eventual result."""

    id: int
    floating: Float
    result: asyncio.Future
    previous_focus: object


class Plugins:
    """Manage plugin UI lifetimes without interpreting shell input or output."""

    def __init__(self, owner, terminal):
        """Start with a full terminal and no additional UI or reserved keys."""
        self.owner = owner
        self.bindings = KeyBindings()
        self.opaque_positions: list[WritePosition] = []
        self.popups: list[Popup] = []
        self._ids = count(1)
        self._bar = Window(height=0)
        self._bar_rows = 0
        self._tasks = set()
        self._cleanups = []
        self._closed = False
        self.errors = []
        self.body = FloatContainer(
            content=HSplit(
                [
                    terminal,
                    ConditionalContainer(
                        DynamicContainer(lambda: self._bar),
                        filter=Condition(lambda: self._bar_rows > 0),
                    ),
                ]
            ),
            floats=[],
        )

    def begin_layout(self):
        """Replace, rather than accumulate, the current frame's surface bounds."""
        self.opaque_positions.clear()

    def load(self, references):
        """Load only explicitly requested module:function plugin entry points."""
        for reference in references:
            module, separator, attribute = reference.partition(":")
            if not separator or not module or not attribute.isidentifier():
                raise ValueError("A terminal plugin must be module:setup")
            setup = getattr(importlib.import_module(module), attribute)
            cleanup = setup(self)
            if cleanup is not None:
                if not callable(cleanup):
                    raise TypeError(
                        "Plugin setup must return a cleanup callable or None"
                    )
                self._cleanups.append(cleanup)

    def bind(self, *keys):
        """Register an explicit plugin shortcut, enabled only while loaded."""
        if len(keys) != 1:
            raise ValueError(
                "Terminal UI shortcuts reserve one key, not input prefixes"
            )
        register = self.bindings.add(*keys, eager=True)

        def decorate(handler):
            @wraps(handler)
            def guarded(event):
                try:
                    for key in event.key_sequence:
                        self.owner.host.consume_key(key)
                    result = handler(event)
                    if inspect.isawaitable(result):
                        self.create_task(result)
                except Exception as error:
                    self._record_error(error)

            return register(guarded)

        return decorate

    def _record_error(self, error):
        self.errors.append(f"{type(error).__name__}: {error}")
        del self.errors[:-MAX_ERRORS]
        if self.popups:
            self.close_popup(self.popups[0])
        self.owner.app.invalidate()

    def set_bar(self, content=None, *, rows=1):
        """Reserve a bounded bottom region; removing it restores the full grid."""
        if self._closed:
            raise RuntimeError("Plugin UI is closed")
        if not 1 <= rows <= MAX_BAR_ROWS:
            raise ValueError("Invalid bar height")
        self._bar_rows = rows if content is not None else 0
        self._bar = (
            HSplit(
                [to_container(content)],
                height=lambda: Dimension.exact(
                    min(rows, max(0, self.owner.app.output.get_size().rows - 1))
                ),
            )
            if content is not None
            else Window(height=0)
        )
        self.owner.app.invalidate()

    def show_popup(self, content, *, width=40, height=8, left=None, top=None):
        """Open an opaque PTK surface and give its widgets exclusive focus."""
        if self._closed or len(self.popups) >= MAX_POPUPS:
            raise RuntimeError("Cannot open another popup")
        if width < 1 or height < 1 or any(v is not None and v < 0 for v in (left, top)):
            raise ValueError("Popup geometry must be positive")
        app = self.owner.app
        surface = Surface(content, self)
        floating = Float(
            content=surface, width=width, height=height, left=left, top=top
        )
        popup = Popup(
            next(self._ids),
            floating,
            asyncio.get_running_loop().create_future(),
            app.layout.current_window,
        )
        self.owner.release_capture()
        self.owner.focus_terminal(False)
        self.popups.append(popup)
        self.body.floats.append(floating)
        app.layout.focus(surface)
        app.invalidate()
        return popup

    def close_popup(self, popup, result=None):
        """Complete a live popup once; stale completions cannot affect new UI."""
        if popup not in self.popups:
            return False
        index = self.popups.index(popup)
        # Closing a parent also cancels its children instead of restoring dead focus.
        removed = self.popups[index:]
        del self.popups[index:]
        for current in reversed(removed):
            self.body.floats.remove(current.floating)
            if not current.result.done():
                if current is popup:
                    current.result.set_result(result)
                else:
                    current.result.cancel()
        target = popup.previous_focus
        try:
            self.owner.app.layout.focus(target)
        except ValueError:
            self.owner.app.layout.focus(self.owner.window)
        if not self.popups:
            self.owner.focus_terminal(True)
        self.owner.app.invalidate()
        return True

    def create_task(self, coroutine):
        """Tie an asynchronous UI operation to the application lifetime."""
        if self._closed:
            coroutine.close()
            raise RuntimeError("Plugin UI is closed")

        async def guarded():
            try:
                return await coroutine
            except Exception as error:
                self._record_error(error)

        task = self.owner.app.create_background_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self):
        """Cancel pending results and release plugin resources on application exit."""
        self._closed = True
        for popup in self.popups:
            if not popup.result.done():
                popup.result.cancel()
        self.popups.clear()
        self.body.floats.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except Exception as error:
                self._record_error(error)
        self._cleanups.clear()
