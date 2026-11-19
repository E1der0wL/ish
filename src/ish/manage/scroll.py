"""Scrollable management pages with one viewport for keys, wheel and thumb drags."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit.application import get_app
from prompt_toolkit.cache import SimpleCache
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Dimension, HSplit, ScrollablePane, Window
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType


class FieldWindow(Window):
    """Pan one long input line without modifying its buffer, selection or cursor."""

    def __init__(self, control):
        """Keep normal PTK cursor following until Alt navigation moves the viewport."""
        self._anchor = None
        self._column = 0
        super().__init__(
            control,
            height=1,
            wrap_lines=False,
            style="class:text-area",
            always_hide_cursor=Condition(lambda: self._anchor is not None),
        )

    def pan(self, step):
        """Move the visible text by one cell, accumulating even before a redraw."""
        info = self.render_info
        if info is None:
            return False
        content = info.ui_content
        limit = max(0, fragment_list_width(content.get_line(0)) - info.window_width)
        if not limit:
            return False
        position = self.horizontal_scroll if self._anchor is None else self._column
        previous = self._anchor
        self._column = max(0, min(limit, position + step))
        self._anchor = self.content.buffer.document
        return self._column != position or self._anchor != previous

    def _scroll_without_linewrapping(self, ui_content, width, height):
        super()._scroll_without_linewrapping(ui_content, width, height)
        if self._anchor is not None:
            if (
                get_app().layout.current_window is self
                and self._anchor == self.content.buffer.document
            ):
                limit = max(0, fragment_list_width(ui_content.get_line(0)) - width)
                self._column = self.horizontal_scroll = min(self._column, limit)
            else:
                self._anchor = None


@dataclass(frozen=True)
class _Track:
    """Use the same row rounding when painting, grabbing and moving the thumb."""

    MIN_ARROW_ROWS = 4

    x: int
    y: int
    rows: int
    total: int

    @property
    def edge(self):
        # Two end arrows still leave a cell for the thumb and one for travel.
        return int(self.rows >= self.MIN_ARROW_ROWS)

    @property
    def limit(self):
        return max(0, self.total - self.rows)

    @property
    def thumb(self):
        length = self.rows - 2 * self.edge
        return (
            max(1, min(length - 1, round(length * self.rows / self.total)))
            if self.total
            else 0
        )

    @property
    def travel(self):
        return self.rows - 2 * self.edge - self.thumb

    def top(self, offset):
        return self.edge + (
            round(self.travel * offset / self.limit) if self.limit else 0
        )

    def step(self, row):
        """Identify an end arrow in the same coordinates used to draw it."""
        if self.edge and row == self.y:
            return -1
        if self.edge and row == self.y + self.rows - 1:
            return 1
        return 0

    def offset(self, row, grab):
        travel = self.travel
        if travel == 0:
            return 0 if row < self.y else self.limit
        top = max(0, min(travel, row - self.y - self.edge - grab))
        return round(top * self.limit / travel)


class ScrollSurface(HSplit):
    """Route hover and drag gestures across the entire management window."""

    def __init__(self, content, hover_changed, on_press=None):
        """Keep hover local to this management window."""
        super().__init__([content])
        self.hover_changed = hover_changed
        self.on_press = on_press
        self._finish_click = None
        self.hovered = None
        self.dragged = None
        self._size = None
        self._drag_cancelled = False
        self._release_hover = None

    def cancel_deferred_click(self):
        """An explicit field action already handled the pending outside click."""
        self._finish_click = None

    def write_to_screen(self, screen, mouse_handlers, write_position, *args, **kwargs):
        """Use the actual event target, including overlaid dialogs and other panels."""
        targets = MouseHandlers()
        size = (write_position.width, write_position.height)
        if self._size != size:
            self._finish_click = None
            if self.dragged is not None:
                self._drag_cancelled = True
        self._size = size
        super().write_to_screen(screen, targets, write_position, *args, **kwargs)

        def route(event):
            self._release_hover = None
            previous = self.hovered
            self.hovered = None
            x, y = event.position
            target = targets.mouse_handlers[y][x]
            kind = event.event_type
            if kind == MouseEventType.MOUSE_DOWN or (
                kind == MouseEventType.MOUSE_MOVE and event.button == MouseButton.NONE
            ):
                # A new press or a buttonless motion also ends a lost release.
                self.dragged = None
                self._drag_cancelled = False
                self._finish_click = (
                    self.on_press(screen, event.position)
                    if self.on_press and event.button == MouseButton.LEFT
                    else None
                )
            elif kind == MouseEventType.MOUSE_MOVE:
                self._finish_click = None
            if self.dragged is None:
                result = target(event)
            else:
                self.hovered = self.dragged
                result = NotImplemented
                if not self._drag_cancelled and not get_app().busy:
                    result = self.dragged.drag(event)
                if kind == MouseEventType.MOUSE_UP:
                    # When drag and release share one input batch, the old hit map
                    # still has the divider at its initial column. Hit-test the
                    # stationary pointer once the new columns have been painted.
                    self.dragged = None
                    self._release_hover = MouseEvent(
                        event.position,
                        MouseEventType.MOUSE_MOVE,
                        MouseButton.NONE,
                        event.modifiers,
                    )
                    result = None
            if kind == MouseEventType.MOUSE_UP and self._finish_click is not None:
                finish, self._finish_click = self._finish_click, None
                if event.button == MouseButton.LEFT and finish(screen, event.position):
                    result = None
            if previous is not self.hovered:
                self.hover_changed(previous, self.hovered)
                get_app().invalidate()
            return result

        pos = write_position
        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos, pos.xpos + pos.width, pos.ypos, pos.ypos + pos.height, route
        )
        if self._release_hover is not None:
            route(self._release_hover)


class PanelDivider(Window):
    """Drag the one-cell panel boundary without taking keyboard focus."""

    def __init__(self, resize):
        """Apply the new width through the same policy as keyboard resizing."""
        self.resize = resize
        super().__init__(width=1, char="│", style=self._style)

    def _style(self):
        surface = get_app().layout.container
        return (
            "class:accent"
            if surface.hovered is self or surface.dragged is self
            else "class:edge"
        )

    def write_to_screen(self, screen, mouse_handlers, write_position, *args, **kwargs):
        """Use screen coordinates for movement outside the one-column handle."""
        super().write_to_screen(screen, mouse_handlers, write_position, *args, **kwargs)
        pos = write_position
        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos,
            pos.xpos + pos.width,
            pos.ypos,
            pos.ypos + pos.height,
            self._handle,
        )

    def _handle(self, event):
        app = get_app()
        if app.busy or self not in app.layout.walk_through_modal_area():
            return NotImplemented
        surface = app.layout.container
        surface.hovered = self
        if (
            event.event_type == MouseEventType.MOUSE_DOWN
            and event.button == MouseButton.LEFT
        ):
            surface.dragged = self
            return None
        return NotImplemented

    def drag(self, event):
        """Align the panel edge to the drag position until release."""
        if event.event_type in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
            return self.resize(event.position.x)
        return NotImplemented


class FormPane(ScrollablePane):
    """Let keyboard navigation, field focus and pointer input scroll the same form."""

    WHEEL_ROWS = 3
    PAGE_OVERLAP = 2
    BAR_WIDTH = 1
    BAR_GAP = 1
    _CURRENT = ContextVar("ish_form_pane", default=None)
    HEIGHT_CACHE_SIZE = 4

    def __init__(self, content, *, icons, keep_focused_window_visible=True):
        """Reserve a scrollbar column on overflow and keep field focus in the form."""
        self.icons = icons
        self._show = False
        super().__init__(
            content,
            show_scrollbar=Condition(lambda: self._show),
            display_arrows=False,
            keep_focused_window_visible=keep_focused_window_visible,
        )
        self._manual = False
        self._anchor = None
        self._track = None
        self._frame = None
        self._grab = None
        self._deferred = []
        self._heights = SimpleCache(maxsize=self.HEIGHT_CACHE_SIZE)

    @classmethod
    def current(cls):
        """Return the pane whose virtual screen is currently being painted."""
        return cls._CURRENT.get()

    @property
    def visible_rows(self):
        """Rows copied from the virtual screen into this frame's viewport."""
        return range(self.vertical_scroll, self.vertical_scroll + self._track.rows)

    @property
    def viewport_rows(self):
        """Host rows available to a popup anchored inside the scrolling page."""
        return range(self._track.y, self._track.y + self._track.rows)

    def _height_at(self, width):
        return self._heights.get(
            (get_app().render_counter, width),
            lambda: (
                self.content.preferred_height(
                    width, self.max_available_height
                ).preferred
            ),
        )

    def preferred_height(self, width, max_available_height):
        """Measure a given content width once while PTK lays out the frame."""
        reserve = self.BAR_WIDTH + self.BAR_GAP if self._show else 0
        return Dimension(preferred=self._height_at(max(0, width - reserve)))

    def paint_viewport(self, draw):
        """Schedule visible content after PTK has settled the viewport for this frame."""
        self._deferred.append(draw)

    def _copy_over_screen(self, screen, virtual, position, virtual_width):
        # The pinned ScrollablePane settles focus before this copy. Paint exactly
        # the range it will copy, even on a first-frame jump to a distant field.
        rows = self.visible_rows
        for draw in self._deferred:
            draw(rows)
        # The gap may hold the trailing cell of a wide glyph at the content edge.
        copy_width = virtual_width + (self.BAR_GAP if self._show else 0)
        super()._copy_over_screen(screen, virtual, position, copy_width)

    def scroll(self, key):
        """Apply Alt navigation to this viewport without selecting a field or item."""
        if not self._track:
            return False
        if key in (Keys.Left, Keys.Right):
            window = get_app().layout.current_window
            return isinstance(window, FieldWindow) and window.pan(
                1 if key == Keys.Right else -1
            )
        page = max(1, self._track.rows - self.PAGE_OVERLAP)
        if key == Keys.Home:
            return self._scroll(0)
        if key == Keys.End:
            return self._scroll(self._track.limit)
        step = {
            Keys.Up: -1,
            Keys.Down: 1,
            Keys.PageUp: -page,
            Keys.PageDown: page,
        }.get(key)
        return step is not None and self._scroll(self.vertical_scroll + step)

    def _scroll(self, offset):
        previous, manual = self.vertical_scroll, self._manual
        self.vertical_scroll = max(0, min(self._track.limit, offset))
        self._manual = True
        return self.vertical_scroll != previous or not manual

    def _make_window_visible(self, visible_height, virtual_height, position, cursor):
        window = get_app().layout.current_window
        if isinstance(window, FieldWindow) and window._anchor is not None:
            # PTK's invisible cursor can lie outside this virtually rendered form.
            cursor = None
        anchor = (window, cursor)
        if self._manual and (self._anchor is None or anchor == self._anchor):
            self._anchor = anchor
            return
        self._manual, self._anchor = False, anchor
        super()._make_window_visible(visible_height, virtual_height, position, cursor)

    def _draw_scrollbar(self, write_position, content_height, screen):
        track = self._track
        top = track.top(self.vertical_scroll)
        surface = get_app().layout.container
        highlighted = surface.hovered is self or surface.dragged is self
        thumb_style = (
            " class:scrollbar.highlight" if highlighted else " class:scrollbar.button"
        )
        thumb = Char(" ", self._parent_style + thumb_style)
        rail = Char(" ", self._parent_style)
        arrows = self.icons().large
        for row in range(track.rows):
            y = track.y + row
            step = track.step(y)
            if step:
                cell = Char(
                    arrows.up if step < 0 else arrows.down,
                    self._parent_style + " class:scrollbar.arrow",
                )
            else:
                cell = thumb if top <= row < top + track.thumb else rail
            screen.data_buffer[y][track.x] = cell

    def _begin(self, event):
        app = get_app()
        track = self._track
        # Scrollbar focus belongs to its page, never to an unrelated action button.
        if not app.layout.has_focus(self):
            app.layout.focus(self)
        self._manual, self._anchor = True, None
        step = track.step(event.position.y)
        if step:
            self._scroll(self.vertical_scroll + step)
            self._grab = None  # Consume the release without starting a thumb drag.
        else:
            grab = event.position.y - track.y - track.top(self.vertical_scroll)
            if not 0 <= grab < track.thumb:
                grab = track.thumb // 2
                self._scroll(track.offset(event.position.y, grab))
            self._grab = grab, event.position.y
        app.layout.container.dragged = self

    def drag(self, event):
        """Keep the thumb attached while its original track is visible."""
        if self._frame != get_app().render_counter or not self._show:
            self._grab = None
        if self._grab is not None and event.event_type in (
            MouseEventType.MOUSE_UP,
            MouseEventType.MOUSE_MOVE,
        ):
            grab, last = self._grab
            if event.position.y != last:
                self._scroll(self._track.offset(event.position.y, grab))
                self._grab = grab, event.position.y
        return None

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
    ):
        """Draw the page and attach input using the geometry from this render."""
        pos = write_position

        app = get_app()
        self._parent_style = parent_style
        self._frame = app.render_counter
        self._show = False
        total = self._height_at(pos.width)
        self._show = (
            pos.width > self.BAR_WIDTH + self.BAR_GAP
            and pos.height > 0
            and total > pos.height
        )
        if self._show:
            total = self._height_at(pos.width - self.BAR_WIDTH - self.BAR_GAP)
        total = min(total, self.max_available_height)
        track = _Track(
            pos.xpos + pos.width - self.BAR_WIDTH,
            pos.ypos,
            pos.height,
            max(total, pos.height),
        )
        if self._track != track:
            self._grab = None
        self._track = track
        self.vertical_scroll = min(self.vertical_scroll, track.limit)
        token = self._CURRENT.set(self)
        try:
            # The fixed scrollbar gap needs no flexible PTK column or mouse map.
            if self._show:
                pos = WritePosition(
                    pos.xpos, pos.ypos, pos.width - self.BAR_GAP, pos.height
                )
            super().write_to_screen(
                screen, mouse_handlers, pos, parent_style, erase_bg, z_index
            )
        finally:
            self._CURRENT.reset(token)
            self._deferred.clear()

    def _copy_over_mouse_handlers(self, mouse_handlers, virtual, pos, virtual_width):
        # A single handler translates into the exact visible virtual rows, including
        # when rows above them have no mouse entries because they weren't painted.
        app, track, offset = get_app(), self._track, self.vertical_scroll

        def handle(event):
            x, y = event.position
            on_bar = self._show and x == track.x
            wheel = event.event_type in (
                MouseEventType.SCROLL_UP,
                MouseEventType.SCROLL_DOWN,
            )
            if (on_bar or wheel) and self not in app.layout.walk_through_modal_area():
                return NotImplemented
            if on_bar:
                surface = app.layout.container
                if isinstance(surface, ScrollSurface):
                    surface.hovered = self
            if wheel:
                step = (
                    -self.WHEEL_ROWS
                    if event.event_type == MouseEventType.SCROLL_UP
                    else self.WHEEL_ROWS
                )
                return (
                    None
                    if self._scroll(self.vertical_scroll + step)
                    else NotImplemented
                )
            if on_bar:
                if (
                    event.event_type == MouseEventType.MOUSE_DOWN
                    and event.button == MouseButton.LEFT
                    and not app.busy
                ):
                    self._begin(event)
                    return None
                # ScrollSurface invalidates when the pointer enters or leaves the bar.
                return NotImplemented
            point = Point(x=x - pos.xpos, y=y - pos.ypos + offset)
            return virtual.mouse_handlers[point.y][point.x](
                MouseEvent(point, event.event_type, event.button, event.modifiers)
            )

        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos, track.x + self.BAR_WIDTH, pos.ypos, pos.ypos + pos.height, handle
        )
