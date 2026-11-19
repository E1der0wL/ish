"""The sidebar and content pane shared by the help and settings windows."""

from __future__ import annotations

from enum import Enum, auto
from functools import partial
from time import monotonic

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
    to_container,
)
from prompt_toolkit.layout.containers import HorizontalAlign, VerticalAlign, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, CheckboxList, Dialog, TextArea

from ish.lang import Message, i18n
from ish.runtime.settings import ESCAPE_DELAY

from .host import ManagementApplication
from .icons import DEFAULT_ICONS, ICON_SETS
from .palette import PanelStyle, ui_style
from .scroll import FormPane, PanelDivider, ScrollSurface


def reset_clicks(field):
    """Start a new mouse click sequence without changing the buffer or its selection."""
    # PTK counts releases by time alone and has no public reset for its double-click timer.
    field.control._last_click_timestamp = None


def on_click(action):
    """Run a management action on left release, leaving wheel events to PTK."""

    def handle(event):
        if event.button != MouseButton.LEFT:
            return NotImplemented
        if event.event_type == MouseEventType.MOUSE_UP:
            if not getattr(get_app(), "busy", False):
                action()
        elif event.event_type != MouseEventType.MOUSE_DOWN:
            return NotImplemented
        return None

    return handle


class EditAction(Enum):
    """The user's intent when finishing the current field."""

    BLUR = auto()
    CONFIRM = auto()
    SAVE = auto()
    CANCEL = auto()


class FocusPane(VSplit):
    """Keep the inactive pane readable and let a background click activate it."""

    def __init__(self, content, active, focus, *, width, button_focus=None):
        """Use the layout's actual focus as the source of both color and clicks."""
        self.active = active
        self.focus = focus
        self.button_focus = button_focus
        super().__init__(
            [Window(width=1), content, Window(width=1)],
            width=width,
            height=Dimension(weight=1),
            style=lambda: "" if active() else PanelStyle.INACTIVE,
        )

    def write_to_screen(self, screen, mouse_handlers, write_position, *args, **kwargs):
        """Give widgets the first chance to handle a click in the inactive pane."""
        if self.active() and not (self.button_focus and self.button_focus()):
            return super().write_to_screen(
                screen, mouse_handlers, write_position, *args, **kwargs
            )
        targets = MouseHandlers()
        super().write_to_screen(screen, targets, write_position, *args, **kwargs)

        def handle(event):
            app = get_app()
            before = app.layout.current_control
            x, y = event.position
            result = targets.mouse_handlers[y][x](event)
            if (
                event.button == MouseButton.LEFT
                and event.event_type == MouseEventType.MOUSE_UP
                and app.layout.current_control is before
                and result is NotImplemented
                and not app.busy
                and self in app.layout.walk_through_modal_area()
                and not app.is_done
            ):
                self.focus()
                return None
            return result

        pos = write_position
        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos, pos.xpos + pos.width, pos.ypos, pos.ypos + pos.height, handle
        )


class AuxiliaryButton:
    """A three-cell icon action with a translated toolbar label."""

    WIDTH = 3

    def __init__(self, glyph, label, action):
        """Keep the same hit target and focus when the displayed icon set changes."""
        self.label = label
        self.handler = action
        keys = KeyBindings()

        enabled = Condition(lambda: not get_app().busy)
        keys.add("enter", filter=enabled)(lambda _: action())
        keys.add(" ", filter=enabled)(lambda _: action())
        self.control = FormattedTextControl(
            glyph,
            focusable=True,
            show_cursor=False,
            key_bindings=keys,
        )
        self.window = Window(
            self.control,
            height=1,
            width=self.WIDTH,
            align=WindowAlign.CENTER,
            always_hide_cursor=True,
            style=lambda: (
                "class:auxiliary.focused"
                if get_app().layout.has_focus(self.control) and not get_app().busy
                else "class:auxiliary"
            ),
        )

    def __pt_container__(self):
        """Expose the labelled control to PTK's focus and layout code."""
        return self.window


class Screen:
    """Own focus, panel size and keyboard navigation for one standalone TUI."""

    NAV_WIDTH = 26
    MIN_NAV_WIDTH = 12
    MIN_BODY_WIDTH = 34
    BUTTON_GAP = 1
    HEADER_CLOSE = "×"
    HELP_PADDING_MIN_ROWS = 22
    PROGRESS_INTERVAL = 0.12
    PROGRESS_WIDTH = 24
    PROGRESS_PULSE = 5
    SHORTCUT_PADDING = 1
    SHORTCUT_ELLIPSIS = " …"
    SHORTCUT_CTRL = "Ctrl + "
    SHORTCUT_ALT = " Alt + "
    SCROLL_ARROWS = {"←": Keys.Left, "↓": Keys.Down, "↑": Keys.Up, "→": Keys.Right}
    SCROLL_KEYS = (
        *SCROLL_ARROWS.values(),
        Keys.PageUp,
        Keys.PageDown,
        Keys.Home,
        Keys.End,
    )
    CTRL_KEYS = {
        "F": Keys.ControlF,
        "E": Keys.ControlE,
        "R": Keys.ControlR,
        "S": Keys.ControlS,
        "C": Keys.ControlC,
    }

    def __init__(
        self,
        title,
        pages,
        *,
        groups=(),
        buttons=(),
        auxiliary=(),
        filter_options=None,
        filter_query=lambda: "",
        field_finished=None,
        page_selected=None,
        overlay=None,
        on_press=None,
        loading=lambda: False,
        palette=lambda: {},
        icons=lambda: ICON_SETS[DEFAULT_ICONS],
        editor=lambda: None,
        dirty=None,
        fields=None,
        input=None,
        output=None,
    ):
        """Build the two panes and place keyboard focus on their category list."""
        self.pages = [(title, to_container(page)) for title, page in pages]
        self._index, self.status = 0, ""
        self.page_selected = page_selected
        self.status_error = False
        self._edit_notice = False
        self.dirty = dirty
        self.groups = dict(groups)
        self.buttons = buttons
        self.icons = icons
        self.auxiliary = [
            AuxiliaryButton(
                lambda key=key: getattr(self.icons(), key),
                label,
                action,
            )
            for key, label, action in auxiliary
        ]
        self.button_groups = {
            button.control: group
            for group in (self.buttons, self.auxiliary)
            for button in group
        }
        self.hover_origin = None
        for button in (*self.buttons, *self.auxiliary):
            button.control.mouse_handler = self.button_mouse(button)
        self.filter_options = filter_options
        self.field_finished = field_finished
        self.filter_query = filter_query
        self.filtering = False
        self.filter_origin = None
        self.fields = fields if fields is not None else {}
        self.editor = editor
        self.width = self.NAV_WIDTH
        self.searching = False
        self.return_index = 0
        self.editing = None
        self._progress_timer = None
        self.loading = loading
        self._progress_start = 0.0
        self.nav = FormattedTextControl(
            self.navigation, focusable=True, show_cursor=False
        )
        self.sidebar_scroll = FormPane(
            Window(self.nav, always_hide_cursor=True), icons=self.icons
        )
        self.search = None
        if filter_options is None:
            self.search = TextArea(
                multiline=False, prompt=" / ", style="class:field.focused"
            )
            self.search.buffer.on_text_changed += self.find
        self.bindings = KeyBindings()
        self._bind()

        def line(fragments, style="", **kwargs):
            return Window(
                FormattedTextControl(fragments), style=style, height=1, **kwargs
            )

        sidebar = HSplit(
            [
                Window(height=1),
                *(
                    [
                        line(title, "class:title"),
                        Window(height=1, char="─", style="class:edge"),
                    ]
                    if dirty is None
                    else []
                ),
                *(
                    [
                        ConditionalContainer(
                            self.search, Condition(lambda: self.searching)
                        )
                    ]
                    if self.search is not None
                    else []
                ),
                self.sidebar_scroll,
                Window(height=1),
            ],
            height=Dimension(weight=1),
        )
        content = HSplit(
            [
                Window(height=1),
                *(
                    [
                        self.page_header(),
                        Window(height=1, char="─", style="class:edge"),
                    ]
                    if dirty is not None
                    else []
                ),
                DynamicContainer(lambda: self.pages[self.index][1]),
                *([self.action_row()] if buttons else []),
            ],
            width=Dimension(weight=1),
            height=Dimension(weight=1),
        )
        base = HSplit(
            [
                *([self.header(title)] if dirty is not None else []),
                VSplit(
                    [
                        FocusPane(
                            sidebar,
                            self.in_sidebar,
                            lambda: self.select_page(self.index),
                            width=self.sidebar_width,
                        ),
                        PanelDivider(self.resize_sidebar),
                        FocusPane(
                            content,
                            lambda: not self.in_sidebar(),
                            self.focus_body,
                            width=Dimension(weight=1),
                            button_focus=self.focused_buttons,
                        ),
                    ],
                    height=Dimension(weight=1),
                ),
                Window(
                    FormattedTextControl(self.shortcuts),
                    style="class:shortcut.bar",
                    height=1,
                ),
            ],
            style="class:surface",
        )
        if filter_options is not None:
            self.filter_input = TextArea(multiline=False, style="class:field.focused")
            dialog = Dialog(
                title=i18n.get(Message.MANAGE_FILTER),
                body=self.filter_input,
                buttons=[
                    Button(
                        i18n.get(message),
                        handler=action,
                        left_symbol="",
                        right_symbol="",
                    )
                    for message, action in (
                        (Message.MANAGE_FILTER_APPLY, self.apply_filter),
                        (Message.MANAGE_CANCEL, self.cancel_filter),
                    )
                ],
                width=Dimension(preferred=46, max=46),
                with_background=False,
            )
            filter_dialog = ConditionalContainer(
                dialog, Condition(lambda: self.filtering)
            )
            base = FloatContainer(base, [Float(content=filter_dialog)])
        if overlay is not None:
            base = overlay(base)
        self.app = ManagementApplication(
            layout=Layout(
                ScrollSurface(base, self.button_hover, on_press),
                focused_element=self.nav,
            ),
            key_bindings=self.bindings,
            style=DynamicStyle(lambda: ui_style(palette())),
            include_default_pygments_style=False,
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
        )
        self.app.ttimeoutlen = ESCAPE_DELAY
        self.busy = False

    @property
    def busy(self):
        """Whether a management operation currently owns input."""
        return self.app.busy

    @busy.setter
    def busy(self, value):
        self.app.busy = value
        self.update_progress()

    def update_progress(self):
        """Animate one timer for both blocking file work and background discovery."""
        pending = self.busy or self.loading()
        if not pending and self._progress_timer is not None:
            self._progress_timer.cancel()
            self._progress_timer = None
        if pending and self._progress_timer is None:
            self._progress_start = monotonic()
            self._refresh_progress()

    def _refresh_progress(self):
        if (
            (self.busy or self.loading())
            and self.app.is_running
            and self.app.loop is not None
        ):
            self.app.invalidate()
            self._progress_timer = self.app.loop.call_later(
                self.PROGRESS_INTERVAL, self._refresh_progress
            )

    def in_sidebar(self):
        """Use the actual category or search focus to decide which pane keeps color."""
        control = get_app().layout.current_control
        return control is self.nav or (
            self.search is not None and control is self.search.control
        )

    def focused_buttons(self):
        """Return the active button group from PTK's current focus."""
        return self.button_groups.get(get_app().layout.current_control, ())

    def button_mouse(self, button):
        """Report eligible hover targets and execute a left click once."""

        def activate():
            self.app.layout.focus(button)
            button.handler()

        click = on_click(activate)

        def handle(event):
            if (
                event.event_type != MouseEventType.MOUSE_MOVE
                or event.button != MouseButton.NONE
            ):
                if (
                    event.button == MouseButton.LEFT
                    and event.event_type
                    in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP)
                    and not self.busy
                ):
                    self.app.layout.container.hovered = button.control
                return click(event)
            editor = self.editor()
            if (
                self.busy
                or self.filtering
                or self.in_sidebar()
                or self.editing is not None
                or (editor and editor.picking)
            ):
                return NotImplemented
            self.app.layout.container.hovered = button.control
            return NotImplemented

        return handle

    def button_hover(self, previous, target):
        """Borrow focus across adjacent buttons and return it when the pointer leaves."""
        layout = self.app.layout
        origin = self.hover_origin if layout.current_control is previous else None
        self.hover_origin = None
        if target in self.button_groups:
            if layout.current_control is not target:
                self.hover_origin = origin or layout.current_window
                layout.focus(target)
        elif origin is not None:
            if origin in walk(layout.container, skip_hidden=True):
                layout.focus(origin)
            else:
                self.focus_body()

    def header(self, title):
        """Display the window title and a close target across the whole settings view."""
        close = FormattedTextControl(self.HEADER_CLOSE, show_cursor=False)
        close.mouse_handler = on_click(self.close)

        def heading():
            icons = self.icons()
            return icons.gear + icons.gear_gap + title

        return VSplit(
            [
                Window(width=1),
                Window(FormattedTextControl(heading)),
                Window(close, width=AuxiliaryButton.WIDTH, align=WindowAlign.CENTER),
                Window(width=1),
            ],
            height=1,
            style="class:header",
        )

    def close(self):
        """Leave the management window without writing the current draft."""
        if not self.busy:
            self.app.exit(result=False)

    def group_start(self):
        """Find the sidebar group containing the selected page."""
        return max((start for start in self.groups if start <= self.index), default=0)

    def page_header(self):
        """Keep the page title and all three tool targets on one line."""

        def title():
            return self.pages[self.index][0]

        def title_width():
            tool_width = len(self.auxiliary) * (AuxiliaryButton.WIDTH + self.BUTTON_GAP)
            space = self.content_width() - tool_width - self.BUTTON_GAP
            return min(get_cwidth(title()), max(0, space))

        return VSplit(
            [
                Window(
                    FormattedTextControl(title),
                    width=title_width,
                    style="class:section.title",
                ),
                Window(
                    FormattedTextControl(self.tool_label),
                    align=WindowAlign.RIGHT,
                    style="class:muted",
                ),
                *self.auxiliary,
            ],
            height=1,
            padding=self.BUTTON_GAP,
        )

    def action_row(self):
        """Place status and actions side by side, directly below the divider."""
        rule = Window(height=1, char="─", style="class:edge")
        if self.dirty is None:

            def padding():
                return int(
                    get_app().output.get_size().rows >= self.HELP_PADDING_MIN_ROWS
                )

            close = VSplit(self.buttons, height=1, align=HorizontalAlign.RIGHT)
            return HSplit([rule, Window(height=padding), close, Window(height=padding)])

        inline_width = sum(
            button.width for button in self.buttons
        ) + self.BUTTON_GAP * (len(self.buttons) - 1)
        stacked_width = max(button.width for button in self.buttons)
        inline = VSplit(self.buttons, height=1, padding=self.BUTTON_GAP)
        stacked = HSplit(
            [
                VSplit([button], height=1, align=HorizontalAlign.RIGHT)
                for button in self.buttons
            ]
        )

        def button_width():
            return (
                inline_width
                if self.content_width() >= inline_width + 2 * self.BUTTON_GAP + 1
                else stacked_width
            )

        def status():
            width = self.content_width() - button_width() - 2 * self.BUTTON_GAP
            return self.status_line(max(0, width))

        actions = HSplit(
            [
                DynamicContainer(
                    lambda: inline if button_width() == inline_width else stacked
                )
            ],
            width=button_width,
            align=VerticalAlign.BOTTOM,
        )
        row = VSplit(
            [
                Window(width=self.BUTTON_GAP, height=1),
                Window(
                    FormattedTextControl(status),
                    # A pasted filter can be arbitrarily long. Keep its summary
                    # on one line while allowing actionable errors to be read in full.
                    wrap_lines=Condition(
                        lambda: self.status_error or not self.filter_query()
                    ),
                    dont_extend_height=True,
                ),
                Window(width=self.BUTTON_GAP, height=1),
                actions,
            ],
        )
        return HSplit([rule, row, Window(height=1)])

    def content_width(self):
        """Measure the main pane between the divider and its two margins."""
        return max(1, get_app().output.get_size().columns - self.sidebar_width() - 3)

    def tool_label(self):
        """Identify the focused icon alongside its toolbar controls."""
        control = self.app.layout.current_control
        return next((b.label for b in self.auxiliary if b.control is control), "")

    def status_line(self, width):
        """Leave the strip empty until there is progress, a result or a warning."""
        if self.busy or (self.loading() and not self.status_error):
            label = "◌  " + i18n.get(Message.MANAGE_LOADING)
            if width < get_cwidth(label):
                return [("class:status.busy", "◌" if width else "")]
            width = min(self.PROGRESS_WIDTH, max(0, width - get_cwidth(label) - 2))
            if not width:
                return [("class:status.busy", label)]
            pulse = min(width, self.PROGRESS_PULSE)
            travel = width - pulse
            tick = int((monotonic() - self._progress_start) / self.PROGRESS_INTERVAL)
            left = travel - abs(tick % (2 * travel) - travel) if travel else 0
            return [
                ("class:status.busy", label + "  "),
                ("class:edge", "━" * left),
                ("class:status.busy", "━" * pulse),
                ("class:edge", "━" * (travel - left)),
            ]
        if self.status_error:
            return [("class:error", "!  " + self.status)]
        status = []
        if self.dirty():
            status.append(
                ("class:status.dirty", "●  " + i18n.get(Message.MANAGE_DIRTY))
            )
        elif self.status:
            status.append(("class:status", "●  " + self.status))
        query = self.filter_query()
        if query:
            status.append(
                (
                    "class:muted",
                    ("  ·  " if status else "")
                    + i18n.get(Message.MANAGE_FILTER_ACTIVE, query=query),
                )
            )
        return status

    def set_status(self, message, error=False):
        """Publish a result independently of the draft's current save state."""
        self.status, self.status_error = message, error
        self._edit_notice = False
        self.app.invalidate()

    def shortcuts(self):
        """Use the two ends of the footer for active Ctrl and Alt combinations."""
        if self.busy:
            return []
        settings = self.dirty is not None
        entering_query = self.searching or self.filtering
        actions = []
        if not entering_query:
            if settings:
                actions.append(("S", Message.MANAGE_APPLY_KEY))
            find = (
                Message.MANAGE_FILTER
                if self.filter_options is not None
                else Message.MANAGE_FIND
            )
            actions.append(("F", find))
        if settings and not entering_query:
            actions.extend(
                [("E", Message.MANAGE_EDIT_KEY), ("R", Message.MANAGE_RELOAD_KEY)]
            )
        if not settings:
            actions.append(("C", Message.MANAGE_CLOSE_KEY))
        if not actions:
            return []

        control = [
            self.shortcut_segment([(key, self.CTRL_KEYS[key])], message)
            for key, message in actions
        ]
        width = get_app().output.get_size().columns - 2 * self.SHORTCUT_PADDING
        if width <= 0:
            return []
        alt, alt_width = [], 0
        if not entering_query and self.scroll_target() is not None:
            scroll = self.shortcut_segment(
                self.SCROLL_ARROWS.items(), Message.MANAGE_SCROLL, alt=True
            )
            first_ctrl = len(self.SHORTCUT_CTRL) + control[0][0]
            alt, alt_width = self.shortcut_group(
                [scroll], self.SHORTCUT_ALT, max(0, width - first_ctrl)
            )
        left, used = self.shortcut_group(control, self.SHORTCUT_CTRL, width - alt_width)
        margin = ("", " " * self.SHORTCUT_PADDING)
        return [margin, *left, ("", " " * (width - used - alt_width)), *alt, margin]

    def shortcut_segment(self, bindings, message, *, alt=False):
        """Return the measured cap and route its clicks to the displayed binding."""

        def invoke(sequence):
            if not alt:
                # The queued action settles the edit, e.g. Save must keep invalid input.
                self.app.layout.container.cancel_deferred_click()
            self.app.key_processor.feed_multiple(sequence, first=True)

        keys = []
        for glyph, key in bindings:
            sequence = (
                [KeyPress(Keys.Escape), KeyPress(key)] if alt else [KeyPress(key)]
            )
            click = on_click(partial(invoke, sequence))
            keys.append(("class:shortcut.key", glyph, click))

        # A Ctrl segment has one action. Each Alt arrow has an independent hit target.
        target = () if alt else (keys[0][2],)
        separator = self.icons().separator
        fragments = [
            ("class:shortcut.leading", separator, *target),
            ("class:shortcut.label", " ", *target),
            *keys,
            ("class:shortcut.label", " " + i18n.get(message) + " ", *target),
            ("class:shortcut.edge", separator, *target),
        ]
        return sum(get_cwidth(item[1]) for item in fragments), fragments

    def shortcut_group(self, items, prefix, width):
        """Fit complete key/label segments; a final ellipsis marks omitted items."""
        if not items:
            return [], 0
        fragments = [("", prefix)] if prefix else []
        used = get_cwidth(prefix)
        ellipsis = get_cwidth(self.SHORTCUT_ELLIPSIS)
        for index, (cells, segment) in enumerate(items):
            reserve = ellipsis if index + 1 < len(items) else 0
            if used + cells + reserve > width:
                if used + ellipsis <= width:
                    fragments.append(("", self.SHORTCUT_ELLIPSIS))
                    used += ellipsis
                return (fragments, used) if used <= width else ([], 0)
            fragments.extend(segment)
            used += cells
        return fragments, used

    def scroll_target(self):
        """Select the focused sidebar or the current page, including its action row."""
        if self.searching or self.filtering:
            return None
        if get_app().layout.current_control is self.nav:
            return self.sidebar_scroll
        page = self.pages[self.index][1]
        return page if isinstance(page, FormPane) else None

    def sidebar_width(self, requested=None):
        """Leave room for the main pane when the terminal shrinks."""
        available = get_app().output.get_size().columns
        return max(
            6,
            min(
                max(self.MIN_NAV_WIDTH, self.width if requested is None else requested),
                available // 2,
                max(self.MIN_NAV_WIDTH, available - self.MIN_BODY_WIDTH),
            ),
        )

    def resize_sidebar(self, width):
        """Apply the same visible bounds to pointer and keyboard resizing."""
        width = self.sidebar_width(width)
        if self.sidebar_width() == width:
            return NotImplemented
        self.width = width

    def navigation(self):
        """Render category groups and mark the active category even in the main pane."""
        fragments = []
        focused = get_app().layout.current_control is self.nav
        for n, (title, _) in enumerate(self.pages):
            if n in self.groups:
                if n:
                    fragments.append(("", "\n"))
                fragments.append(("class:muted", self.groups[n] + "\n"))
            active = self.index == n
            if active:
                fragments.append(("[SetCursorPosition]", ""))
            style = (
                ("class:nav.selected" if focused else "class:nav.current")
                if active
                else ""
            )
            marker = self.icons().large.right if active else " "
            row = marker + " " + title
            row += " " * max(0, self.sidebar_width() - 2 - get_cwidth(row))
            fragments.append((style, row, partial(self._click_page, n)))
            fragments.append(("", "\n"))
        return fragments

    def _click_page(self, index, event):
        if index == self.index and self.app.layout.current_control is self.nav:
            return NotImplemented
        return on_click(partial(self.select_page, index))(event)

    def select_page(self, index):
        """Switch pages on a single click and keep keyboard navigation in the sidebar."""
        self.leave_edit()
        self.searching = False
        self.index = index
        self.app.layout.focus(self.nav)

    @property
    def index(self):
        """The page selected by the sidebar, search or pointer."""
        return self._index

    @index.setter
    def index(self, value):
        if value != self._index:
            self._index = value
            if self.page_selected is not None:
                self.page_selected(value)

    def active_input(self):
        """Return the text box holding the user's current input, including HEX."""
        field = self.fields.get(self.editing)
        if isinstance(field, TextArea):
            return field
        picker = self.editor()
        return picker.active_input if picker else None

    def leave_edit(self, action=EditAction.BLUR):
        """Report validation success; rejected Enter and Save keep the editor open."""
        control = self.editing
        picker = self.editor()
        widget = picker if picker and picker.picking else self.fields.get(control)
        if widget is None:
            return True
        field = self.active_input()
        self.app.layout.container.cancel_deferred_click()
        try:
            if action is EditAction.CANCEL or (
                field is None and action is not EditAction.CONFIRM
            ):
                if hasattr(widget, "cancel"):
                    widget.cancel()
            else:
                widget.accept()
        except (OSError, RuntimeError, ValueError) as error:
            if action is EditAction.BLUR:
                widget.cancel()
                self.editing = None
                message = Message.MANAGE_EDIT_RESTORED
            else:
                self.hover_origin = None
                self.app.layout.container.hovered = None
                reset_clicks(field)
                self.app.layout.focus(field)
                message = Message.MANAGE_ERROR
            self.set_status(i18n.get(message, error=error), error=True)
            self._edit_notice = True
            return False
        self.editing = None
        if self._edit_notice:
            self.set_status("")
        if (
            control is not None
            and action is not EditAction.CANCEL
            and self.field_finished
        ):
            self.field_finished(control)
        return True

    def start_edit(self, control):
        """Give keyboard or pointer editing the same starting value and focus."""
        if self.busy:
            return
        if self.editing is not control:
            self.leave_edit()
            self.searching = False
            self.app.layout.focus(control)
            self.editing = control
            widget = self.fields[control]
            if hasattr(widget, "begin"):
                widget.begin()

    def find(self, _):
        """Preview the first help category matching the query."""
        query = self.search.text.strip().casefold()
        self.index = self.return_index
        if not query:
            return
        for n, (title, _) in enumerate(self.pages):
            if query in title.casefold():
                self.index = n
                return

    def open_filter(self):
        """Edit a temporary query and keep the applied option filter until confirmed."""
        if self.busy or self.filter_options is None or self.filtering:
            return
        self.filter_origin = self.app.layout.current_window
        self.leave_edit()
        self.filter_input.text = self.filter_query()
        self.filter_input.buffer.cursor_position = len(self.filter_input.text)
        self.filtering = True
        self.app.layout.update_parents_relations()
        self.app.layout.focus(self.filter_input)

    def apply_filter(self):
        """Update the visible options without committing the settings draft."""
        self.filter_options(self.filter_input.text.strip())
        self.cancel_filter()

    def cancel_filter(self):
        """Close the filter editor, retaining focus if its option is still visible."""
        self.filtering = False
        layout = self.app.layout
        layout.update_parents_relations()
        if self.filter_origin in walk(layout.container, skip_hidden=True):
            layout.focus(self.filter_origin)
        else:
            self.focus_body()
        self.filter_origin = None

    def _bind(self):
        bind = self.bindings.add
        nav = Condition(lambda: self.app.layout.current_control is self.nav)
        searching = Condition(lambda: self.searching)
        filtering = Condition(lambda: self.filtering)
        body = ~nav & ~searching & ~filtering

        def scroll(event):
            target = self.scroll_target()
            if (
                self.busy
                or target is None
                or not target.scroll(event.key_sequence[-1].key)
            ):
                return NotImplemented

        for key in self.SCROLL_KEYS:
            bind("escape", key, eager=True)(scroll)

        # PTK's default document key bindings move the text cursor to scroll.
        # Ordinary navigation belongs to the sidebar, forms and action buttons.
        document = body & Condition(
            lambda: (
                self.dirty is None
                and isinstance(self.pages[self.index][1], FormPane)
                and self.app.layout.has_focus(self.pages[self.index][1])
            )
        )
        for key in self.SCROLL_KEYS:
            if key != Keys.Left:  # Left goes back to the sidebar from the help body.
                bind(key, filter=document, eager=True)(lambda _: None)

        @bind("up", filter=nav)
        @bind("down", filter=nav)
        def category(event):
            step = -1 if event.key_sequence[0].key == "up" else 1
            first = self.group_start()
            last = (
                min((i for i in self.groups if i > self.index), default=len(self.pages))
                - 1
            )
            index = max(first, min(last, self.index + step))
            if index == self.index:
                return NotImplemented
            self.index = index

        @bind("c-left", filter=nav)
        @bind("c-right", filter=nav)
        def resize(event):
            step = -1 if event.key_sequence[0].key == "c-left" else 1
            return self.resize_sidebar(self.sidebar_width() + step)

        @bind("enter", filter=nav)
        @bind(" ", filter=nav)
        @bind("right", filter=nav)
        def enter(event):
            self.focus_body()

        @bind(
            "enter",
            filter=filtering
            & Condition(lambda: self.app.layout.has_focus(self.filter_input)),
            eager=True,
        )
        def apply_query(event):
            self.apply_filter()

        @bind("c-f", filter=~filtering)
        def search(event):
            if self.filter_options is not None:
                self.open_filter()
                return
            self.leave_edit()
            self.return_index = self.index
            self.searching = True
            self.search.text = ""
            event.app.layout.update_parents_relations()
            event.app.layout.focus(self.search)

        @bind("enter", filter=searching, eager=True)
        def accept_search(event):
            self.searching = False
            self.focus_body()

        @bind("tab", filter=nav)
        @bind("s-tab", filter=nav)
        def next_group(event):
            groups = list(self.groups)
            if len(groups) < 2:
                self.focus_body()
                return
            current = self.group_start()
            step = -1 if event.key_sequence[0].key == "s-tab" else 1
            self.index = groups[(groups.index(current) + step) % len(groups)]

        def palette_focus():
            widget = self.editor()
            return widget is not None and self.app.layout.has_focus(widget.container)

        def active_input():
            return self.editing is not None or (
                palette_focus() and self.editor().picking
            )

        idle = body & ~Condition(active_input)
        native_list = Condition(
            lambda: isinstance(
                self.fields.get(self.app.layout.current_control), CheckboxList
            )
        )

        @bind("left", filter=idle)
        def sidebar(event):
            event.app.layout.focus(self.nav)

        @bind(
            "tab",
            filter=body & ~Condition(lambda: palette_focus() and self.editor().picking),
        )
        @bind(
            "s-tab",
            filter=body & ~Condition(lambda: palette_focus() and self.editor().picking),
        )
        def zone(event):
            self.hover_origin = None
            self.leave_edit()
            layout = event.app.layout
            groups = [
                None,
                *(group for group in (self.buttons, self.auxiliary) if group),
            ]
            current = next(
                (
                    i
                    for i, group in enumerate(groups)
                    if group and any(layout.has_focus(b) for b in group)
                ),
                0,
            )
            step = -1 if event.key_sequence[0].key == "s-tab" else 1
            target = groups[(current + step) % len(groups)]
            if target is None:
                self.focus_body()
            else:
                layout.focus(target[0 if step > 0 else -1])

        @bind(
            "up",
            filter=idle
            & ~Condition(palette_focus)
            & ~native_list
            & Condition(lambda: bool(self.fields)),
        )
        @bind(
            "down",
            filter=idle
            & ~Condition(palette_focus)
            & ~native_list
            & Condition(lambda: bool(self.fields)),
        )
        def field(event):
            if not self.move_field(-1 if event.key_sequence[0].key == "up" else 1):
                return NotImplemented

        actions = Condition(lambda: bool(self.focused_buttons()))

        @bind("left", filter=actions)
        @bind("right", filter=actions)
        def action(event):
            self.hover_origin = None
            layout = event.app.layout
            buttons = self.focused_buttons()
            pos = next(i for i, b in enumerate(buttons) if layout.has_focus(b))
            step = -1 if event.key_sequence[0].key == "left" else 1
            target = pos + step
            if not 0 <= target < len(buttons):
                return NotImplemented
            layout.focus(buttons[target])

        field_focus = Condition(lambda: self.app.layout.current_control in self.fields)

        @bind("enter", filter=idle & field_focus & ~native_list, eager=True)
        @bind(" ", filter=idle & field_focus & ~native_list, eager=True)
        def edit(event):
            self.start_edit(event.app.layout.current_control)

        @bind("enter", filter=Condition(lambda: self.editing is not None), eager=True)
        def accept(event):
            self.leave_edit(EditAction.CONFIRM)

        @bind("escape", eager=Condition(lambda: not self.app.key_processor.input_queue))
        def back(event):
            if self.busy:
                return
            if self.filtering:
                self.cancel_filter()
            elif self.searching:
                self.searching, self.index = False, self.return_index
                event.app.layout.focus(self.nav)
            elif self.editing is not None or (
                palette_focus() and self.editor().picking
            ):
                self.leave_edit(EditAction.CANCEL)
            elif self.focused_buttons():
                self.focus_body()
            elif event.app.layout.current_control is not self.nav:
                event.app.layout.focus(self.nav)
            else:
                self.close()

        if self.dirty is None:
            bind("c-c", eager=True)(lambda _: self.close())
            bind("c-d", eager=True)(lambda _: self.close())

    def release_focus(self, replaced=None):
        """Forget retired controls while keeping focus history for live windows."""
        layout = self.app.layout
        # PTK retains each visited window to restore a container's last focus.
        # It has no public removal API for windows replaced by a dynamic form.
        if replaced is None:  # Every page was rebuilt; shared screen controls survive.
            retired = set(layout._stack).difference(layout.find_all_windows())
        else:
            retired = {
                window
                for container in replaced
                for window in walk(container)
                if isinstance(window, Window)
            }
        if layout.current_window in retired:
            self.focus_body()
        if retired:
            layout._stack[:] = [
                window for window in layout._stack if window not in retired
            ]

    def focus_body(self):
        """Give focus to the selected page after PTK has attached its controls."""
        self.searching = False
        layout = self.app.layout
        layout.update_parents_relations()
        page = self.pages[self.index][1]
        if any(
            isinstance(w, Window) and w.content.is_focusable()
            for w in walk(page, skip_hidden=True)
        ):
            layout.focus(page)
        elif self.auxiliary:
            layout.focus(self.auxiliary[0])

    def move_field(self, direction):
        """Move through visible page controls without crossing into the action row."""
        layout = self.app.layout
        page = self.pages[self.index][1]
        items = [
            w
            for w in walk(page, skip_hidden=True)
            if isinstance(w, Window) and w.content.is_focusable()
        ]
        if layout.current_window in items:
            target = items.index(layout.current_window) + direction
            if 0 <= target < len(items):
                layout.focus(items[target])
                return True
        return False

    def run(self):
        """Return the explicit save result, treating interrupted input as cancellation."""
        try:
            return self.app.run()
        except (EOFError, KeyboardInterrupt):
            return False
