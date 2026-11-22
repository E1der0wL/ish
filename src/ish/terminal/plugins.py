"""Opt-in PTK surfaces with explicit lifetime and focus ownership.

Plugins receive UI operations, not a second terminal writer or a command
dispatcher. No plugin, surface, or shortcut is installed by default.
"""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
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

from ish.lang import Message, i18n


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
    mouse: bool = False


@dataclass
class _TaskScope:
    """Retain the first popup owned by a callback and its asynchronous descendants."""

    popup: Popup | None = None


class Plugins:
    """Manage plugin UI lifetimes without interpreting shell input or output."""

    API_VERSION = 1
    MAX_POPUPS = 16
    MAX_BAR_ROWS = 8
    MAX_ERRORS = 16

    def __init__(self, owner, terminal):
        """Start with a full terminal and no additional UI or reserved keys."""
        self.owner = owner
        self.manager = None
        self.bindings = KeyBindings()
        self.opaque_positions: list[WritePosition] = []
        self.popups: list[Popup] = []
        self._ids = count(1)
        self._bar = Window(height=0)
        self._bar_rows = 0
        self._bar_mouse = False
        self._tasks = set()
        self._cleanups = []
        self._closed = False
        self._loaded = False
        self._search_serial = 0
        self._search_task = None
        self._scope = ContextVar("terminal_plugin_scope", default=None)
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

    def load(self, names, *, directory=None, library_dir=None):
        """Reuse PluginManager for selected names, metadata and dependencies only."""
        if self._loaded or self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_ONCE))
        self._loaded = True
        names = tuple(dict.fromkeys(names))
        if not names:
            return
        from ish.plugin.manager import PluginManager

        self.manager = PluginManager(script_dir=directory, lib_dir=library_dir)
        self.manager.load(names=names)
        for name in names:
            module = self.manager.get(name)
            if module is None:
                raise ValueError(i18n.get(Message.VT_PLUGIN_MISSING, name=name))
            version = getattr(module, "TERMINAL_API", None)
            if type(version) is not int or version != self.API_VERSION:
                raise ValueError(i18n.get(Message.VT_PLUGIN_API, name=name))
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise TypeError(i18n.get(Message.VT_PLUGIN_SETUP, name=name))
            cleanup = setup(self)
            if cleanup is not None:
                if not callable(cleanup):
                    raise TypeError(i18n.get(Message.STARTUP_PLUGIN_CLEANUP))
                self._cleanups.append(cleanup)

    def settings_for(self, name):
        """Return this loaded package's typed options with defaults applied."""
        from ish.plugin.options import resolved

        info = self.manager.registry.get_info(name) if self.manager else None
        if info is None:
            raise KeyError(name)
        return resolved(info.settings, self.owner.plugin_settings.get(name, {}))

    def get(self, name):
        """Read an already loaded dependency from the shared plugin registry."""
        return self.manager.get(name) if self.manager is not None else None

    def register_protocol(self, name, *, kind, prefixes, factory):
        """Register a synchronous supplemental OSC codec before shell input.

        A factory returns handle/cancel/close methods; handle returns
        ish.terminal.extensions.ProtocolResult. It receives no terminal writer.
        Existing native commands and overlapping namespaces are rejected.
        """
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        return self.owner.vt.register_protocol(
            name, kind=kind, prefixes=prefixes, factory=factory
        )

    def register_keys(self, name, mappings):
        """Register additional host keys without taking input or writer ownership."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        return self.owner.host.register_keys(name, mappings)

    def register_clipboard(self, name, factory):
        """Select one OS clipboard connection; the broker retains request policy."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        return self.owner.clipboard.register_provider(name, factory=factory)

    def scroll_prompts(self, delta):
        """Navigate native semantic markers without interpreting shell commands."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        self.owner.vt.scroll_prompts(delta)
        self.owner.app.invalidate()

    def scroll_pages(self, pages):
        """Move the native history viewport by pages with one line of overlap."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        self.owner.scroll(pages * max(1, self.owner.size[1] - 1))

    def select_output(self, x, y):
        """Select marked native output at a viewport cell, if available."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        selected = self.owner.vt.select_output(x, y)
        self.owner.app.invalidate()
        return selected

    def select_last_output(self):
        """Select the last valid native output anchor, without a shadow history."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        selected = self.owner.vt.select_last_output()
        self.owner.app.invalidate()
        return selected

    def copy_last_output(self):
        """Request a copy after native selection; True is not OS copy completion."""
        if not self.select_last_output():
            return False
        self.owner.copy_selection()
        return True

    @property
    def mouse_required(self):
        """Only explicitly selected, currently visible surfaces request capture."""
        return (self._bar_rows > 0 and self._bar_mouse) or any(
            popup.mouse for popup in self.popups
        )

    async def search(self, text):
        """Search native history cooperatively without changing input focus.

        A later search cancels this request. Results and screen anchors remain
        native-owned; this token identifies only the caller waiting for them.
        """
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        self._cancel_search()
        serial = self._search_serial
        task = self._search_task = asyncio.current_task()
        try:
            self.owner.vt.set_search(text)
            while not self._closed and serial == self._search_serial:
                complete, count = self.owner.vt.search_step()
                if complete:
                    self.owner.app.invalidate()
                    return count
                await asyncio.sleep(0)
            raise asyncio.CancelledError
        finally:
            if self._search_task is task:
                self._search_task = None

    def _cancel_search(self):
        self._search_serial += 1
        previous, self._search_task = self._search_task, None
        if previous is not None and not previous.done():
            if previous is not asyncio.current_task():
                previous.cancel()

    def clear_search(self):
        """Cancel outstanding search work and restore the native selection."""
        self._cancel_search()
        if not self._closed:
            self.owner.vt.set_search("")
            self.owner.vt.clear_selection()
            self.owner.app.invalidate()

    def invalidate(self):
        """Request a redraw without handing a plugin the host writer."""
        if not self._closed:
            self.owner.app.invalidate()

    def select_match(self, *, previous=False):
        """Move the native viewport/selection, retaining the caller's focus."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        index = self.owner.vt.select_match(previous=previous)
        self.owner.app.invalidate()
        return index

    def bind(self, *keys, primary_only=False):
        """Register an explicit plugin shortcut, enabled only while loaded."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        if len(keys) != 1:
            raise ValueError(i18n.get(Message.VT_PLUGIN_KEY))
        enabled = (
            Condition(
                lambda: (
                    self.owner.app.layout.has_focus(self.owner.window)
                    and not self.owner.vt.alternate
                )
            )
            if primary_only
            else True
        )
        register = self.bindings.add(*keys, eager=True, filter=enabled)

        def decorate(handler):
            @wraps(handler)
            def guarded(event):
                press = event.key_sequence[-1]
                stroke = getattr(press, "structured_key", None)
                if (
                    stroke is not None
                    and self.owner.app.layout.has_focus(self.owner.window)
                    and not stroke.matches_shortcut(keys[0])
                ):
                    self.owner.control.forward_key(event)
                    return
                scope = _TaskScope(self.popups[-1] if self.popups else None)
                token = self._scope.set(scope)
                try:
                    for key in event.key_sequence:
                        self.owner.host.consume_key(key)
                    result = handler(event)
                    if inspect.isawaitable(result):
                        self.create_task(result)
                except Exception as error:
                    self._record_error(error, scope)
                finally:
                    self._scope.reset(token)

            return register(guarded)

        return decorate

    def _record_error(self, error, scope=None):
        self.errors.append(f"{type(error).__name__}: {error}")
        del self.errors[: -self.MAX_ERRORS]
        # An old task can finish after its popup closes. Its diagnostic remains
        # useful, but it no longer owns another popup's focus or result.
        if scope is not None and scope.popup in self.popups:
            self.close_popup(scope.popup)
        self.owner.app.invalidate()

    def bar_height(self, rows):
        """Reserve the same rows for PTY sizing and layout, leaving one for the shell."""
        return min(self._bar_rows, max(0, rows - 1))

    def set_bar(self, content=None, *, rows=1, mouse=False):
        """Reserve a bounded bottom region; removing it restores the full grid."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        if not 1 <= rows <= self.MAX_BAR_ROWS:
            raise ValueError(i18n.get(Message.VT_BAR_HEIGHT))
        if type(mouse) is not bool:
            raise TypeError(i18n.get(Message.VT_PLUGIN_MOUSE))
        self._bar_rows = rows if content is not None else 0
        self._bar_mouse = mouse if content is not None else False
        self._bar = (
            HSplit(
                [to_container(content)],
                height=lambda: Dimension.exact(
                    self.bar_height(self.owner.app.output.get_size().rows)
                ),
            )
            if content is not None
            else Window(height=0)
        )
        self.owner.app.invalidate()

    def show_popup(
        self, content, *, width=40, height=8, left=None, top=None, mouse=False
    ):
        """Open an opaque PTK surface and give its widgets exclusive focus."""
        if self._closed or len(self.popups) >= self.MAX_POPUPS:
            raise RuntimeError(i18n.get(Message.VT_POPUP_UNAVAILABLE))
        if width < 1 or height < 1 or any(v is not None and v < 0 for v in (left, top)):
            raise ValueError(i18n.get(Message.VT_POPUP_GEOMETRY))
        if type(mouse) is not bool:
            raise TypeError(i18n.get(Message.VT_PLUGIN_MOUSE))
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
            mouse,
        )
        self.owner.release_capture()
        self.owner.focus_terminal(False)
        self.popups.append(popup)
        scope = self._scope.get()
        if scope is not None and scope.popup not in self.popups:
            scope.popup = popup
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
        self.owner.app.retire_mouse()
        self.owner.app.invalidate()
        return True

    def create_task(self, coroutine):
        """Tie an asynchronous UI operation to the application lifetime."""
        if self._closed:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError(i18n.get(Message.VT_PLUGIN_CLOSED))
        scope = self._scope.get() or _TaskScope(
            self.popups[-1] if self.popups else None
        )

        async def guarded():
            token = self._scope.set(scope)
            try:
                return await coroutine
            except Exception as error:
                self._record_error(error, scope)
            finally:
                self._scope.reset(token)

        task = self.owner.app.create_background_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # A task cancelled before its first turn never awaits the supplied
        # coroutine. Closing it also covers that shutdown boundary.
        if inspect.iscoroutine(coroutine):
            task.add_done_callback(lambda task: coroutine.close())
        return task

    async def close(self):
        """Cancel pending results and release plugin resources on application exit."""
        if self._closed:
            return
        self._closed = True
        self._cancel_search()
        self._bar_rows = 0
        self._bar_mouse = False
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
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                self._record_error(error)
        self._cleanups.clear()
