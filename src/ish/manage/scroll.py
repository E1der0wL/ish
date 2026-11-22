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
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth

from .palette import PanelStyle


def on_hover(target, handler):
    """Report a painted hit target without acting on it or changing PTK focus."""

    def handle(event):
        if (
            event.event_type == MouseEventType.MOUSE_MOVE
            and event.button == MouseButton.NONE
        ):
            app = get_app()
            if not app.busy:
                app.layout.container.hovered = target
        return handler(event)

    return handle


def hover_style(target, normal, highlighted=None):
    """Keep just the widget under the pointer in its focused colors."""
    if getattr(get_app().layout.container, "hovered", None) == target:
        return f"{highlighted or normal} {PanelStyle.HOVER}"
    return normal


def fit_caption(text, width):
    """Fit a single caption in terminal cells, reserving the last cell for ellipsis."""
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    used = 0
    for index, char in enumerate(text):
        used += get_cwidth(char)
        if used >= width:
            return text[:index] + "…"
    return text


def preview_value(control, value, expanded):
    """Paint a cell-limited preview without altering the control's editable value."""
    original = control.create_content

    def content(width, height):
        raw = value()
        if expanded() or not raw:
            return original(width, height)
        line = [("", fit_caption(raw, width))]
        return UIContent(get_line=lambda _: line, line_count=1, show_cursor=False)

    control.create_content = content


class HitWindow(Window):
    """Limit pointer input to the text cells actually painted on each visible row."""

    def __init__(self, *args, text_only=lambda: True, **kwargs):
        """Keep a filled button's full hit box when it renders with a visible background."""
        self.text_only = text_only
        super().__init__(*args, **kwargs)

    def write_to_screen(self, screen, handlers, position, style, erase, z_index):
        """Copy only painted columns from PTK’s row-specific mouse map."""
        if not self.text_only():
            return super().write_to_screen(
                screen, handlers, position, style, erase, z_index
            )
        targets = MouseHandlers()
        super().write_to_screen(screen, targets, position, style, erase, z_index)
        info = self.render_info
        if not info:
            return
        align = self.align() if callable(self.align) else self.align
        for offset, row in enumerate(info.displayed_lines):
            y = position.ypos + offset
            cells = min(
                position.width, fragment_list_width(info.ui_content.get_line(row))
            )
            if cells and offset < position.height:
                left = position.xpos
                if align == WindowAlign.CENTER:
                    left += (position.width - cells) // 2
                elif align == WindowAlign.RIGHT:
                    left += position.width - cells
                handlers.set_mouse_handler_for_range(
                    left,
                    left + cells,
                    y,
                    y + 1,
                    targets.mouse_handlers[y][left],
                )


class FieldWindow(Window):
    """Pan one long input line without modifying its buffer, selection or cursor."""

    def __init__(self, control, editing):
        """Keep normal PTK cursor following until Alt navigation moves the viewport."""
        self._anchor = None
        self._column = 0
        super().__init__(
            control,
            height=lambda: None if editing() and self._anchor is None else 1,
            dont_extend_height=True,
            wrap_lines=Condition(lambda: editing() and self._anchor is None),
            style="class:text-area",
            always_hide_cursor=Condition(lambda: self._anchor is not None),
        )

    def reset_pan(self):
        """Open a fresh edit with the full, wrapped buffer visible."""
        self._anchor = None
        self.horizontal_scroll = 0

    def preview_expanded(self):
        """Explicit Alt panning exposes the original line until the cursor moves."""
        return self._anchor == self.content.buffer.document

    def pan(self, step):
        """Move the visible text by one cell, accumulating even before a redraw."""
        info = self.render_info
        if info is None:
            return False
        limit = max(0, get_cwidth(self.content.buffer.text) + 1 - info.window_width)
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

    def __init__(
        self, content, on_press, dismiss_outside, focus_background, fits, resize_notice
    ):
        """Keep hover local to this management window."""
        super().__init__([content], window_too_small=resize_notice)
        self.fits = fits
        self._fits = True
        self.on_press = on_press
        self.dismiss_outside = dismiss_outside
        self._consume_release = False
        self.focus_background = focus_background
        self._finish_click = None
        self.pressed = None
        self.hovered = None
        self.dragged = None
        self._size = None
        self._drag_cancelled = False
        self._release_hover = None
        self._clicked_button = None
        self.rail_tip = None
        self.pointer_row = 0

    def clicked(self, target, event):
        """Pair a button release with its original target across redraws."""
        if event.button == MouseButton.LEFT and not get_app().busy:
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self.pressed = target
            elif event.event_type == MouseEventType.MOUSE_UP:
                return self.pressed == target
        return False

    def release_button(self, control):
        """Delay hover re-entry until the pointer has actually left the clicked button."""
        self.hovered = None
        self._clicked_button = (control, get_app().layout.current_window)

    def cancel_deferred_click(self):
        """An explicit field action already handled the pending outside click."""
        self._finish_click = None

    def write_to_screen(self, screen, mouse_handlers, write_position, *args, **kwargs):
        """Use the actual event target, including overlaid dialogs and other panels."""
        targets = MouseHandlers()
        size = (write_position.width, write_position.height)
        if self._size != size:
            self._fits = self.fits(*size)
            if not self._fits:
                self.pressed = None
                self._finish_click = None
            if self.dragged is not None:
                self._drag_cancelled = True
        self._size = size
        if not self._fits:
            # Only change the drawing. PTK retains the editor, selection and cursor.
            self.window_too_small.write_to_screen(
                screen, mouse_handlers, write_position, *args, **kwargs
            )
            return
        super().write_to_screen(screen, targets, write_position, *args, **kwargs)

        def route(event):
            self._release_hover = None
            old_tip, self.rail_tip = self.rail_tip, None
            self.pointer_row = event.position.y
            previous = self.hovered
            self.hovered = None
            x, y = event.position
            target = targets.mouse_handlers[y][x]
            kind = event.event_type
            new_gesture = kind == MouseEventType.MOUSE_DOWN or (
                kind == MouseEventType.MOUSE_MOVE and event.button == MouseButton.NONE
            )
            if new_gesture:
                self.pressed = None
            if kind == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
                self._consume_release = self.dismiss_outside(screen, event.position)
            if self._consume_release and event.button == MouseButton.LEFT:
                # The release still belongs to the dismissed menu after a repaint or move.
                if kind == MouseEventType.MOUSE_UP:
                    self._consume_release = False
                return None
            if new_gesture:
                # A new press or a buttonless motion also ends a lost release.
                self.dragged = None
                self._drag_cancelled = False
                self._finish_click = (
                    self.on_press(screen, event.position)
                    if self.on_press and event.button == MouseButton.LEFT
                    else None
                )
            elif self.dragged is not None and (
                kind == MouseEventType.MOUSE_MOVE or self._drag_cancelled
            ):
                # A captured divider or thumb drag keeps the open editor.
                self._finish_click = None
            if self.dragged is None:
                result = target(event)
                if (
                    kind == MouseEventType.MOUSE_DOWN
                    and event.button == MouseButton.LEFT
                    and result is NotImplemented
                    and self.focus_background(screen, event.position)
                ):
                    result = None
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
            if kind == MouseEventType.MOUSE_UP:
                self.pressed = None
            if kind == MouseEventType.MOUSE_UP and self._finish_click is not None:
                finish, self._finish_click = self._finish_click, None
                if event.button == MouseButton.LEFT and finish(screen, event.position):
                    result = None
            if kind == MouseEventType.MOUSE_MOVE and self._clicked_button is not None:
                control, origin = self._clicked_button
                if (
                    self.hovered is control
                    and get_app().layout.current_window is origin
                ):
                    self.hovered = None
                else:
                    self._clicked_button = None
            if previous != self.hovered or old_tip != self.rail_tip:
                get_app().invalidate()
            return result

        pos = write_position
        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos, pos.xpos + pos.width, pos.ypos, pos.ypos + pos.height, route
        )
        if self._release_hover is not None:
            route(self._release_hover)


class PanelDivider(HSplit):
    """Drag the one-cell panel boundary without taking keyboard focus."""

    HEADING_ROWS = 2

    def __init__(self, resize=None, *, title=None, compact=lambda: False):
        """Use the same capture for a vertical edge or a category heading and rule."""
        self.resize = resize
        self.title = title
        self._rule_y = self._grab = 0
        if title is None:
            super().__init__([Window(char="│", style=self._style)], width=1)
        else:
            super().__init__(
                [
                    Window(
                        FormattedTextControl(lambda: "" if compact() else " " + title),
                        height=1,
                        style="class:group",
                    ),
                    Window(height=1, char="─", style=self._style),
                ],
                height=self.HEADING_ROWS,
            )

    def _style(self):
        surface = get_app().layout.container
        return (
            "class:edge.hover"
            if surface.hovered is self or surface.dragged is self
            else "class:edge"
        )

    def write_to_screen(self, screen, mouse_handlers, write_position, *args, **kwargs):
        """Capture from the painted handle, even when dragging outside its pane."""
        self._rule_y = write_position.ypos + write_position.height - 1
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
            if self.resize is not None:
                self._grab = (
                    self._rule_y - event.position.y if self.title is not None else 0
                )
                surface.dragged = self
            return None
        if (
            event.event_type == MouseEventType.MOUSE_UP
            and event.button == MouseButton.LEFT
        ):
            return None
        return NotImplemented

    def drag(self, event):
        """Align the panel edge to the drag position until release."""
        if event.event_type in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
            target = (
                event.position.x
                if self.title is None
                else event.position.y + self._grab
            )
            return self.resize(target)
        return NotImplemented


class CategorySplit(HSplit):
    """Allocate two scrolling lists around a draggable heading inside the sidebar."""

    GAP = 1
    MIN_LIST_ROWS = 1

    def __init__(self, titles, upper, lower, *, compact=lambda: False):
        """Start at the natural list heights and retain the ratio after a drag."""
        self.upper, self.lower = upper, lower
        self.upper_box = HSplit([upper])
        self.gap = Window()
        self.fraction = None
        self.available = self.upper_rows = self.rule_y = 0
        super().__init__(
            [
                PanelDivider(title=titles[0], compact=compact),
                self.upper_box,
                self.gap,
                PanelDivider(self.resize, title=titles[1], compact=compact),
                lower,
            ]
        )

    def preferred_height(self, width, max_available_height):
        """Let the parent shrink independently of last frame's list heights."""
        minimum = 2 * (PanelDivider.HEADING_ROWS + self.MIN_LIST_ROWS)
        return Dimension(
            min=min(minimum, max_available_height), preferred=max_available_height
        )

    def _clamp(self, rows):
        minimum = min(self.MIN_LIST_ROWS, self.available // 2)
        return max(minimum, min(rows, self.available - minimum))

    def resize(self, rule_y):
        """Remember the latest request, including several moves before a redraw."""
        rows = self._clamp(self.upper_rows + rule_y - self.rule_y)
        previous = (
            self.upper_rows
            if self.fraction is None
            else round(self.available * self.fraction)
        )
        if rows == previous or not self.available:
            return NotImplemented
        self.fraction = rows / self.available

    def write_to_screen(self, screen, handlers, position, *args, **kwargs):
        """Reserve both headings and allocate remaining rows to the two lists."""
        space = max(0, position.height - 2 * PanelDivider.HEADING_ROWS)
        gap = min(self.GAP, max(0, space - 2 * self.MIN_LIST_ROWS))
        self.gap.height = Dimension.exact(gap)
        self.available = space - gap
        if self.fraction is None:
            first = self.upper.preferred_height(
                position.width, self.available
            ).preferred
            second = self.lower.preferred_height(
                position.width, self.available
            ).preferred
            rows = min(first, max(self.available // 2, self.available - second))
        else:
            rows = round(self.available * self.fraction)
        self.upper_rows = self._clamp(rows)
        self.upper_box.height = Dimension.exact(self.upper_rows)
        self.rule_y = (
            position.ypos + 2 * PanelDivider.HEADING_ROWS - 1 + self.upper_rows + gap
        )
        super().write_to_screen(screen, handlers, position, *args, **kwargs)


class FormPane(ScrollablePane):
    """Let keyboard navigation, field focus and pointer input scroll the same form."""

    UP_ARROW = "△"
    DOWN_ARROW = "▽"
    WHEEL_ROWS = 3
    PAGE_OVERLAP = 2
    BAR_WIDTH = 1
    BAR_GAP = 1
    _CURRENT = ContextVar("ish_form_pane", default=None)
    HEIGHT_CACHE_SIZE = 4

    def __init__(self, content, *, keep_focused_window_visible=True):
        """Reserve a scrollbar column on overflow and keep field focus in the form."""
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

    def focus_item(self, position):
        """Keep the focused setting's title, description and editor together."""
        self._focused_item = position

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

    def restore_scroll(self, offset):
        """Restore a translated page's reading position, clamped on its first render."""
        self.vertical_scroll = offset
        self._manual, self._anchor = True, None

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
        if self._focused_item is not None:
            position = self._focused_item
        super()._make_window_visible(visible_height, virtual_height, position, cursor)

    def _draw_scrollbar(self, write_position, content_height, screen):
        track = self._track
        top = track.top(self.vertical_scroll)
        surface = get_app().layout.container
        highlighted = surface.hovered is self or surface.dragged is self
        thumb_style = (
            " class:scrollbar.highlight" if highlighted else " class:scrollbar.button"
        )
        arrow_style = " class:edge"
        if highlighted:
            thumb_style += " " + PanelStyle.HOVER
            arrow_style = " class:accent " + PanelStyle.HOVER
        thumb = Char(" ", self._parent_style + thumb_style)
        rail = Char(" ", self._parent_style)
        for row in range(track.rows):
            y = track.y + row
            step = track.step(y)
            if step:
                cell = Char(
                    self.UP_ARROW if step < 0 else self.DOWN_ARROW,
                    self._parent_style + arrow_style,
                )
            else:
                cell = thumb if top <= row < top + track.thumb else rail
            screen.data_buffer[y][track.x] = cell

    def _begin(self, event, step):
        app = get_app()
        track = self._track
        # Scrollbar focus belongs to its page, never to an unrelated action button.
        if not app.layout.has_focus(self):
            app.layout.focus(self)
        self._manual, self._anchor = True, None
        if step:
            self._scroll(self.vertical_scroll + step)
            return
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
        self._focused_item = None
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
                if event.button == MouseButton.LEFT and not app.busy:
                    step = track.step(y)
                    if step:
                        if surface.clicked((self, step), event):
                            self._begin(event, step)
                        return None
                    if event.event_type == MouseEventType.MOUSE_DOWN:
                        self._begin(event, 0)
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
