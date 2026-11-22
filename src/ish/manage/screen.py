"""The sidebar and content pane shared by the help and settings windows."""

from __future__ import annotations

import getpass
import os
from enum import Enum, auto
from functools import partial
from time import monotonic

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings
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

from ish import __version__
from ish.lang import Message, i18n
from ish.runtime.settings import ESCAPE_DELAY

from .host import ManagementApplication
from .icons import DEFAULT_ICONS, ICON_SETS
from .palette import PanelStyle, ui_style
from .scroll import (
    CategorySplit,
    FormPane,
    HitWindow,
    PanelDivider,
    ScrollSurface,
    fit_caption,
    hover_style,
    on_hover,
)


def reset_clicks(field):
    """Start a new mouse click sequence without changing the buffer or its selection."""
    # PTK counts releases by time alone and has no public reset for its double-click timer.
    field.control._last_click_timestamp = None


def on_click(action, *, target=None):
    """Activate only when a left press and release belong to this visible action."""
    target = action if target is None else target

    def handle(event):
        if event.button != MouseButton.LEFT:
            return NotImplemented
        if event.event_type not in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
            return NotImplemented
        if get_app().layout.container.clicked(target, event):
            action()
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

    def __init__(self, content, active, focus, *, width):
        """Use the layout's actual focus as the source of both color and clicks."""
        self.active = active
        self.focus = focus
        super().__init__(
            [Window(width=1), content, Window(width=1)],
            width=width,
            height=Dimension(weight=1),
            style=lambda: "" if active() else PanelStyle.INACTIVE,
        )

    def contains(self, frame, point):
        """Hit-test the pane, including margins, against the cells in this frame."""
        boxes = frame.visible_windows_to_write_positions
        left, right = boxes.get(self.children[0]), boxes.get(self.children[-1])
        return (
            left is not None
            and right is not None
            and left.xpos <= point.x < right.xpos + right.width
            and left.ypos <= point.y < left.ypos + left.height
        )


class ActionButton(Button):
    """Run a settings action on keyboard activation or a primary mouse click."""

    PADDING = 2

    @classmethod
    def caption_width(cls, title):
        """Measure two margins around the translated caption in terminal cells."""
        return get_cwidth(title) + 2 * cls.PADDING

    def __init__(self, message, action):
        """Keep the focus and action colors independent of the shell palette."""
        title = i18n.get(message)
        super().__init__(
            title,
            handler=action,
            width=self.caption_width(title),
            left_symbol="",
            right_symbol="",
        )
        self.control.mouse_handler = on_hover(self.control, on_click(action))
        self.window.always_hide_cursor = Always()

        def button_style():
            app = get_app()
            if getattr(app, "busy", False):
                return "class:button.disabled"
            normal = (
                "class:button.focused" if app.layout.has_focus(self) else "class:button"
            )
            return hover_style(self.control, normal, "class:button.focused")

        self.window.style = button_style


class AuxiliaryButton:
    """A three-cell icon action with a translated toolbar label."""

    WIDTH = 3

    def __init__(self, glyph, label, key, action, screen):
        """Keep the same hit target and focus when the displayed icon set changes."""
        self.label = label
        self.key = key
        self.handler = action
        keys = KeyBindings()

        enabled = Condition(lambda: not get_app().busy)
        keys.add("enter", filter=enabled)(lambda _: screen.activate_button(self))
        keys.add(" ", filter=enabled)(lambda _: screen.activate_button(self))
        self.control = FormattedTextControl(
            glyph,
            focusable=True,
            show_cursor=False,
            key_bindings=keys,
        )
        self.window = HitWindow(
            self.control,
            height=1,
            width=self.WIDTH,
            align=WindowAlign.CENTER,
            always_hide_cursor=True,
            style=lambda: hover_style(
                self.control, self.style(screen.active_tool() is self), self.style(True)
            ),
        )

    @staticmethod
    def style(highlighted):
        """Give icon actions a transparent background and a transient accent."""
        return "class:auxiliary.focused" if highlighted else "class:auxiliary"

    def __pt_container__(self):
        """Expose the labelled control to PTK's focus and layout code."""
        return self.window


class SidebarFloat(Float):
    """Anchor a profile menu or a passive rail label to its rendered window."""

    def __init__(self, content, anchor, *, card=False):
        """Resolve the anchor lazily so a resized sidebar keeps the same overlay."""
        super().__init__(content=content)
        self.anchor = anchor
        self.card = card


class SidebarOverlays(FloatContainer):
    """Place sidebar floats using this frame's actual cells, including after resize."""

    def _draw_float(self, fl, screen, handlers, pos, style, erase, z_index):
        if isinstance(fl, SidebarFloat):
            anchor = fl.anchor()
            positions = screen.visible_windows_to_write_positions
            if anchor is not None and not isinstance(anchor, Window):
                anchor = next(
                    (window for window in positions if window.content is anchor), None
                )
            target = positions.get(anchor)
            if not target:
                return
            width = min(pos.width, fl.content.preferred_width(pos.width).preferred)
            if not width:
                return
            height = min(
                pos.height, fl.content.preferred_height(width, pos.height).preferred
            )
            if fl.card:
                surface = get_app().layout.container
                y = (
                    surface.rail_tip[1]
                    if surface.rail_tip
                    else screen.get_cursor_position(anchor).y
                )
                x = target.xpos + target.width + 2
                handlers = MouseHandlers()  # A passive label never takes a click.
            else:
                x = target.xpos
                above = target.ypos - pos.ypos
                below = pos.height - above - target.height
                on_top = below < height and above > below
                height = min(height, max(1, above if on_top else below))
                y = target.ypos - height if on_top else target.ypos + target.height
            fl.width, fl.height = width, height
            fl.left = max(0, min(pos.width - width, x - pos.xpos))
            fl.top = max(0, min(pos.height - height, y - pos.ypos))
        return super()._draw_float(fl, screen, handlers, pos, style, erase, z_index)


class ProfileBar(HSplit):
    """Hub-style sidebar footer with an identity strip and centered actions."""

    PRESET_WIDTH = 14
    GAP = 1

    def __init__(self, screen, page_title, preset_name, open_presets):
        """Keep the same actions and focus when the sidebar changes width."""
        self.screen = screen
        self.version = "ish " + __version__
        self.preset_name = preset_name
        self.page_title = page_title
        self.page_width = ActionButton.caption_width(page_title)
        self.heading = self._control(self._identity, screen.account)
        self.page = self._control(
            lambda: self.page_icon() if screen.compact() else page_title,
            lambda: screen.switch(screen.SWITCH),
        )
        self.preset = self._control(self._preset_caption, open_presets)
        self.controls = (self.heading, self.page, self.preset)

        version = Window(
            FormattedTextControl(self.version),
            height=1,
            width=get_cwidth(self.version),
            style="class:profile.version",
        )
        identity = VSplit(
            [
                HitWindow(self.heading, always_hide_cursor=True, height=1),
                ConditionalContainer(version, Condition(self._version_fits)),
                ConditionalContainer(
                    Window(width=1), Condition(lambda: not screen.compact())
                ),
            ],
            style=lambda: (
                AuxiliaryButton.style(False) if screen.compact() else "class:profile"
            ),
        )
        page = self._button(self.page, self.page_width)
        preset = self.preset_window = self._button(self.preset, self.PRESET_WIDTH)
        inline = VSplit([page, preset], padding=self.GAP, align=HorizontalAlign.CENTER)
        stacked = HSplit(
            [VSplit([item], align=HorizontalAlign.CENTER) for item in (page, preset)],
            padding=self.GAP,
        )
        version_below = Window(
            FormattedTextControl(
                lambda: (
                    ""
                    if screen.compact() or self._version_fits()
                    else self.version + " "
                )
            ),
            align=WindowAlign.RIGHT,
            style="class:muted",
            height=1,
        )
        actions = DynamicContainer(lambda: stacked if screen.compact() else inline)
        super().__init__(
            [
                ConditionalContainer(
                    Window(height=self.GAP), Condition(screen.compact)
                ),
                identity,
                Window(height=self.GAP),
                actions,
                version_below,
            ]
        )

    def page_icon(self):
        """Identify the destination of the screen switch in the current icon set."""
        icons = self.screen.icons()
        return icons.book if self.screen.filter_options is not None else icons.gear

    @property
    def inline_width(self):
        """Two pane insets plus both translated actions on one line."""
        return 2 + self.page_width + self.GAP + self.PRESET_WIDTH

    def _width(self):
        # FocusPane supplies one inset on each side.
        return max(1, self.screen.sidebar_width() - 2)

    def _version_fits(self):
        identity_width = (
            get_cwidth(self._account_prefix()) + get_cwidth(self.screen.username) + 1
        )
        return identity_width + self.GAP + get_cwidth(self.version) <= self._width()

    def _account_prefix(self):
        return " " + self.screen.icons().account + "  "

    def _identity(self):
        style = (
            "class:profile.focused"
            if get_app().layout.has_focus(self.heading)
            else "class:profile.name"
        )
        if self.screen.compact():
            return [(self._icon_style(self.heading), self.screen.icons().account)]
        prefix = self._account_prefix()
        width = self._width() - get_cwidth(prefix) - 1
        return [
            (
                hover_style(self.heading, style, "class:profile.focused"),
                prefix + fit_caption(self.screen.username, width),
            )
        ]

    def _preset_caption(self):
        if self.screen.compact():
            return self.screen.icons().presets
        title = self.preset_name() or i18n.get(Message.MANAGE_PRESET)
        arrow = self.screen.icons().small.down
        width = min(self._width(), self.PRESET_WIDTH) - get_cwidth(arrow) - 2
        return fit_caption(title, width) + " " + arrow

    def _control(self, caption, action):
        keys = KeyBindings()
        control = FormattedTextControl(
            caption, focusable=True, show_cursor=False, key_bindings=keys
        )

        def activate(pointer=False):
            if not self.screen.busy:
                self.screen.leave_edit()
                if self.screen.in_sidebar():
                    self.screen.focus_navigation(pointer=pointer)
                action()

        click = on_click(lambda: activate(pointer=True))

        def pointer(event):
            if event.event_type == MouseEventType.MOUSE_MOVE and self.screen.compact():
                self.screen.hover_label(control, self.label(control))
            return click(event)

        control.mouse_handler = on_hover(control, pointer)
        keys.add("enter")(lambda _: activate())
        keys.add(" ")(lambda _: activate())
        return control

    def label(self, control):
        """Return the unabridged profile action or account caption."""
        if control is self.heading:
            return self.screen.username + " · " + self.version
        if control is self.page:
            return self.page_title
        return self.preset_name() or i18n.get(Message.MANAGE_PRESET)

    def _icon_style(self, control):
        layout = self.screen.app.layout
        return hover_style(
            control,
            AuxiliaryButton.style(layout.has_focus(control)),
            AuxiliaryButton.style(True),
        )

    def _button(self, control, width):
        return HitWindow(
            control,
            width=lambda: min(width, self._width()),
            text_only=self.screen.compact,
            height=1,
            align=WindowAlign.CENTER,
            always_hide_cursor=True,
            style=lambda: (
                self._icon_style(control)
                if self.screen.compact()
                else hover_style(
                    control,
                    "class:button.focused"
                    if get_app().layout.has_focus(control)
                    else "class:button",
                    "class:button.focused",
                )
            ),
        )

    def move(self, key):
        """Use horizontal keys between actions; vertical keys also reach the account."""
        layout = self.screen.app.layout
        controls = self.controls if key in ("up", "down") else (self.page, self.preset)
        current = layout.current_control
        if current in controls:
            step = -1 if key in ("up", "left") else 1
            layout.focus(
                controls[max(0, min(len(controls) - 1, controls.index(current) + step))]
            )


class Screen:
    """Own focus, panel size and keyboard navigation for one standalone TUI."""

    SWITCH = "switch"
    PRESETS = "presets"
    NAV_ITEM_INDENT = "   "
    NAV_WIDTH = 28
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
    }

    def __init__(
        self,
        title,
        pages,
        *,
        title_icon="book",
        groups=(),
        buttons=(),
        auxiliary=(),
        filter_options=None,
        filter_query=lambda: "",
        presets=None,
        preset_name=lambda: "",
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
        app=None,
        on_switch=None,
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
        self.core_pages = len(self.pages)
        self.extra_nav = None
        try:
            self.username = getpass.getuser()
        except (KeyError, OSError):
            self.username = str(os.getuid())
        self.modal = None
        self.profile = ProfileBar(
            self,
            i18n.get(
                Message.MANAGE_HELP_TITLE
                if filter_options is not None
                else Message.MANAGE_CONFIG_SWITCH
            ),
            preset_name,
            presets or (lambda: self.switch(self.PRESETS)),
        )

        self.icons = icons
        self.filter_options = filter_options
        if filter_options is None:
            find_icon, self.find_label = "find", Message.MANAGE_FIND
        else:
            find_icon, self.find_label = "filter", Message.MANAGE_FILTER
        self.auxiliary = [
            AuxiliaryButton(
                lambda glyph=glyph: getattr(self.icons(), glyph),
                label,
                key,
                action,
                self,
            )
            for glyph, label, key, action in (
                (find_icon, i18n.get(self.find_label), "F", self.open_find),
                *auxiliary,
            )
        ]
        self.button_groups = {
            button.control: group
            for group in (self.buttons, self.auxiliary)
            for button in group
        }
        self.pointer_navigation = False
        self.on_press = on_press
        for button in (*self.buttons, *self.auxiliary):
            button.control.mouse_handler = self.button_mouse(button)
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
            HitWindow(self.nav, always_hide_cursor=True, wrap_lines=False)
        )
        self.search = None
        categories = []
        self.nav_split = self.core_pages
        if filter_options is not None:
            titles = (
                i18n.get(Message.MANAGE_ISH_SETTINGS),
                i18n.get(Message.MANAGE_INSTALLED),
            )
        else:
            self.search = TextArea(
                multiline=False, prompt=" / ", style="class:field.focused"
            )
            self.search.buffer.on_text_changed += self.find
            categories.append(
                ConditionalContainer(
                    self.search,
                    Condition(lambda: self.searching and not self.compact()),
                )
            )
            titles = tuple(self.groups.values())
            if len(titles) == 2:
                self.nav_split = max(self.groups)
        if len(titles) == 2:
            self.extra_nav = FormattedTextControl(
                lambda: self.navigation(lower=True),
                focusable=True,
                show_cursor=False,
            )
            self.extra_scroll = FormPane(
                HitWindow(self.extra_nav, always_hide_cursor=True, wrap_lines=False)
            )
            categories.append(
                CategorySplit(
                    titles, self.sidebar_scroll, self.extra_scroll, compact=self.compact
                )
            )
        else:
            categories.append(self.sidebar_scroll)
        self.bindings = KeyBindings()
        self._bind()
        resize_notice = HSplit(
            [
                Window(
                    FormattedTextControl(i18n.get(Message.MANAGE_ENLARGE)),
                    align=WindowAlign.CENTER,
                    height=1,
                    always_hide_cursor=True,
                )
            ],
            align=VerticalAlign.CENTER,
            style="class:surface class:warning",
        )
        sidebar = HSplit(
            [Window(height=1), *categories, self.profile],
            height=Dimension(weight=1),
            window_too_small=resize_notice,
        )
        heading = HSplit(
            [
                Window(height=1),
                self.page_header(),
                Window(height=1, char="─", style="class:edge"),
            ]
        )
        actions = [self.action_row()] if buttons else []
        content = HSplit(
            [heading, DynamicContainer(lambda: self.pages[self.index][1]), *actions],
            width=Dimension(weight=1),
            height=Dimension(weight=1),
            window_too_small=resize_notice,
        )

        # Leave one title cell next to the toolbar and room for a stacked action.
        tool_width = len(self.auxiliary) * (AuxiliaryButton.WIDTH + self.BUTTON_GAP)
        minimum_width = max([tool_width + 1, *(button.width for button in buttons)])

        def fits(width, height):
            body_width = width - self.sidebar_width() - 3
            if body_width < minimum_width:
                return False
            chrome = sum(
                part.preferred_height(body_width, height).min
                for part in (heading, *actions)
            )
            return height >= chrome + 3  # Window title, one body row and key hints.

        nav_pane = FocusPane(
            sidebar,
            self.in_sidebar,
            lambda: self.select_page(self.index, pointer=True),
            width=self.sidebar_width,
        )
        body_pane = FocusPane(
            content,
            lambda: not self.in_sidebar(),
            self.focus_body,
            width=Dimension(weight=1),
        )
        self.panes = nav_pane, body_pane
        base = HSplit(
            [
                self.header(title, title_icon),
                VSplit(
                    [nav_pane, PanelDivider(self.resize_sidebar), body_pane],
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
                    ActionButton(message, action)
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
        card = Window(
            FormattedTextControl(lambda: " " + self.rail_label() + " "),
            wrap_lines=True,
            style="class:nav.selected",
        )
        self.dialogs = SidebarOverlays(
            base,
            [
                SidebarFloat(card, self.rail_anchor, card=True),
            ],
        )
        if self.search is not None:
            self.dialogs.floats.append(
                Float(
                    content=ConditionalContainer(
                        self.search,
                        Condition(lambda: self.searching and self.compact()),
                    ),
                    top=2,
                    left=self.rail_width() + 1,
                    right=AuxiliaryButton.WIDTH + 2,
                    height=1,
                )
            )
        self.on_switch = on_switch
        self.layout = Layout(
            ScrollSurface(
                self.dialogs,
                self.pointer_press,
                self.dismiss_outside,
                self.focus_background,
                fits,
                resize_notice,
            ),
            focused_element=self.nav,
        )
        self.keys = ConditionalKeyBindings(
            self.bindings, Condition(lambda: self.modal is None)
        )
        self.style = DynamicStyle(lambda: ui_style(palette()))
        self.app = app or ManagementApplication(
            layout=self.layout,
            key_bindings=self.keys,
            style=self.style,
            include_default_pygments_style=False,
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
        )
        if app is not None:
            self.show()
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

    def page_nav(self):
        """Return the navigation surface containing the selected page."""
        return (
            self.extra_nav
            if self.index >= self.nav_split and self.extra_nav
            else self.nav
        )

    def show(self):
        """Change the active PTK view while the renderer keeps owning the host screen."""
        self.app.layout = self.layout
        self.app.key_bindings = self.keys
        self.app.style = self.style
        # An input batch can contain another click before the next frame is painted.
        self.app.renderer.mouse_handlers = MouseHandlers()
        self.layout.container.hovered = self.layout.container.rail_tip = None
        self.layout.container.pressed = None
        self.app.invalidate()

    def switch(self, action):
        """Open the companion view without ending the application."""
        if not self.busy and self.on_switch is not None:
            self.on_switch(action)

    def pointer_press(self, frame, point):
        """Prepare an edit that needs a matching release."""
        return self.on_press(frame, point) if self.on_press and not self.modal else None

    def dismiss_outside(self, frame, point):
        """Let the open preset menu consume a left press outside its painted bounds."""
        if not self.modal or not isinstance(self.modal[0], SidebarFloat):
            return False
        box = frame.visible_windows_to_write_positions.get(self.layout.current_window)
        if box and (
            box.xpos <= point.x < box.xpos + box.width
            and box.ypos <= point.y < box.ypos + box.height
        ):
            return False
        self.dismiss()
        return True

    def focus_background(self, frame, point):
        """Give an unclaimed left press to its pane before the next key is dispatched."""
        if self.busy or self.modal or self.filtering or self.app.is_done:
            return False
        for pane in self.panes:
            if (
                pane.contains(frame, point)
                and (not pane.active() or self.focused_buttons())
                and pane in self.app.layout.walk_through_modal_area()
            ):
                pane.focus()
                return True
        return False

    def preset_menu(self, names, current, select):
        """Show saved presets and the final creation action next to the profile."""
        entries = [*names, None]
        position = entries.index(current) if current in names else 0
        keys = KeyBindings()

        def move(index):
            nonlocal position
            position = max(0, min(len(entries) - 1, index))

        def choose(index):
            self.dismiss()
            select(entries[index])

        def mouse(index, event):
            if (
                event.event_type == MouseEventType.MOUSE_MOVE
                and event.button == MouseButton.NONE
            ):
                if position != index:
                    move(index)
                    self.app.invalidate()
                return None
            if event.event_type in (
                MouseEventType.SCROLL_UP,
                MouseEventType.SCROLL_DOWN,
            ):
                move(
                    position
                    + (1 if event.event_type == MouseEventType.SCROLL_DOWN else -1)
                )
                return None
            return on_click(lambda: choose(index), target=(menu, index))(event)

        def options():
            rows = []
            width = max(
                self.profile.PRESET_WIDTH, 3 + max(map(get_cwidth, names), default=0)
            )
            width = min(width, self.app.output.get_size().columns)
            for i, key in enumerate(entries):
                if i:
                    rows.append(("", "\n"))
                if i == position:
                    rows.append(("[SetCursorPosition]", ""))
                if key is None:
                    glyph = self.icons().new_preset
                    label = " " * ((width - get_cwidth(glyph)) // 2) + glyph
                else:
                    label = (" ● " if key == current else " ○ ") + key
                rows.append(
                    (
                        "class:nav.selected" if i == position else "",
                        label + " " * max(0, width - get_cwidth(label)),
                        partial(mouse, i),
                    )
                )
            return rows

        keys.add("down")(lambda _: move(position + 1))
        keys.add("up")(lambda _: move(position - 1))
        keys.add("end")(lambda _: move(len(entries) - 1))
        keys.add("home")(lambda _: move(0))
        keys.add("enter")(lambda _: choose(position))
        keys.add(" ")(lambda _: choose(position))
        keys.add("escape", eager=True)(lambda _: self.dismiss())
        menu = Window(
            FormattedTextControl(options, focusable=True, show_cursor=False),
            wrap_lines=True,
            style="class:setting.value",
        )
        floating = SidebarFloat(
            HSplit([menu], modal=True, key_bindings=keys),
            lambda: self.profile.preset_window,
        )
        self.show_modal(floating, menu)

    def show_modal(self, floating, focus):
        """Remember the caller and direct subsequent input to the overlay."""
        self.layout.container.pressed = None
        self.modal = floating, self.app.layout.current_window
        self.dialogs.floats.append(floating)
        self.app.layout.update_parents_relations()
        self.app.layout.focus(focus)
        self.app.invalidate()

    def dialog(self, title, body, actions):
        """Contain keys, focus and mouse input inside a single profile dialog."""
        buttons = [
            ActionButton(message, handler)
            for message, handler in [*actions, (Message.MANAGE_CLOSE_KEY, self.dismiss)]
        ]
        box = Dialog(
            title=i18n.get(title),
            body=body,
            buttons=buttons,
            modal=False,
            with_background=False,
            width=Dimension(max=58, preferred=58),
        )

        escape = KeyBindings()
        escape.add("escape", eager=True)(lambda _: self.dismiss())
        floating = Float(content=HSplit([box], key_bindings=escape, modal=True))
        self.show_modal(floating, box)

    def dismiss(self):
        """Restore focus to the control that opened the modal."""
        if self.modal:
            floating, origin = self.modal
            self.layout.container.pressed = None
            self.modal = None
            self.dialogs.floats.remove(floating)
            self.app.layout.update_parents_relations()
            self.app.layout.focus(origin)
            self.release_focus([floating.content])
            self.app.invalidate()

    def account(self):
        """Show the local identity used by this terminal session."""
        self.dialog(
            Message.MANAGE_ACCOUNT,
            Window(
                FormattedTextControl(
                    i18n.get(Message.MANAGE_LOCAL_ACCOUNT) + " · " + self.username
                ),
                height=1,
            ),
            [],
        )

    def in_sidebar(self):
        """Use the actual category or search focus to decide which pane keeps color."""
        control = get_app().layout.current_control
        return control in (self.nav, self.extra_nav, *self.profile.controls) or (
            self.search is not None and control is self.search.control
        )

    def focused_buttons(self):
        """Return the active button group from PTK's current focus."""
        return self.button_groups.get(get_app().layout.current_control, ())

    def activate_button(self, button):
        """Cancel the unfinished field before invoking a page tool."""
        if isinstance(button, AuxiliaryButton):
            self.leave_edit(EditAction.CANCEL)
        layout = self.app.layout
        if self.focused_buttons():
            self.focus_body()
            if self.focused_buttons():
                self.focus_navigation(pointer=True)
        layout.container.release_button(button.control)
        button.handler()

    def button_mouse(self, button):
        """Show hover without borrowing keyboard focus from a pane or an editor."""
        click = on_click(partial(self.activate_button, button))
        pointer = on_hover(button.control, click)

        def handle(event):
            if self.busy or self.filtering or self.modal:
                return click(event)
            return pointer(event)

        return handle

    def header(self, title, icon):
        """Display the window title and close target across both panes."""
        close = FormattedTextControl(self.HEADER_CLOSE, show_cursor=False)
        close.mouse_handler = on_hover(close, on_click(self.close))

        def heading():
            icons = self.icons()
            return getattr(icons, icon) + icons.title_gap + title

        return VSplit(
            [
                Window(width=1),
                Window(FormattedTextControl(heading)),
                HitWindow(
                    close,
                    width=AuxiliaryButton.WIDTH,
                    align=WindowAlign.CENTER,
                    style=lambda: hover_style(close, "", "class:auxiliary.focused"),
                ),
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
        """Keep the current page title and its tools on one line."""

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

            actions = VSplit(
                [
                    Window(
                        FormattedTextControl(lambda: self.status),
                        style=lambda: (
                            "class:error" if self.status_error else "class:status"
                        ),
                    ),
                    *self.buttons,
                ],
                height=1,
            )
            return HSplit([rule, actions, Window(height=padding)])

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

    def active_tool(self):
        """Use one icon for hover feedback or explicit keyboard navigation."""
        if self.busy or self.modal or self.filtering:
            return None
        layout = self.app.layout
        for control in (layout.container.hovered, layout.current_control):
            if self.button_groups.get(control) is self.auxiliary:
                return next(b for b in self.auxiliary if b.control is control)
        return None

    def tool_label(self):
        """Label the same toolbar icon that is visibly highlighted."""
        tool = self.active_tool()
        return tool.label if tool else ""

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
        if self.busy or self.modal or self.searching or self.filtering:
            return []
        settings = self.dirty is not None
        actions = [("S", Message.MANAGE_APPLY_KEY)] if settings else []
        actions.append(("F", self.find_label))
        if settings:
            actions.extend(
                [("E", Message.MANAGE_EDIT_KEY), ("R", Message.MANAGE_RELOAD_KEY)]
            )

        control = [
            self.shortcut_segment([(key, self.CTRL_KEYS[key])], message)
            for key, message in actions
        ]
        width = get_app().output.get_size().columns - 2 * self.SHORTCUT_PADDING
        if width <= 0:
            return []
        alt, alt_width = [], 0
        if self.scroll_target() is not None:
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
            click = on_click(partial(invoke, sequence), target=(self, key, alt))
            keys.append(("class:shortcut.key", glyph, click))

        # A Ctrl segment has one action. Each Alt arrow has an independent hit target.
        target = () if alt else (keys[0][2],)
        separator = self.icons().separator
        fragments = [
            ("class:shortcut.leading", separator, *target),
            ("class:shortcut.label", " ", *target),
            ("class:shortcut.label", "<", *target),
            *keys,
            ("class:shortcut.label", ">", *target),
            ("class:shortcut.label", " " + i18n.get(message) + " ", *target),
            ("class:shortcut.edge", separator, *target),
        ]
        return sum(get_cwidth(item[1]) for item in fragments), fragments

    def shortcut_group(self, items, prefix, width):
        """Fit complete key/label segments; a final ellipsis marks omitted items."""
        if not items:
            return [], 0
        fragments = (
            [("class:shortcut.alt" if prefix == self.SHORTCUT_ALT else "", prefix)]
            if prefix
            else []
        )
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
        if self.extra_nav and get_app().layout.current_control is self.extra_nav:
            return self.extra_scroll
        page = self.pages[self.index][1]
        return page if isinstance(page, FormPane) else None

    def sidebar_width(self, requested=None):
        """Leave room for the main pane when the terminal shrinks."""
        available = get_app().output.get_size().columns
        width = min(
            self.width if requested is None else requested,
            available // 2,
            max(1, available - self.MIN_BODY_WIDTH),
        )
        if width >= self.profile.inline_width:
            return width
        return self.rail_width()

    def rail_width(self):
        """Fit the widest sidebar glyph with one cell of padding on either side."""
        return 2 + max(
            get_cwidth(icon)
            for icon in (
                self.profile.page_icon(),
                self.icons().account,
                self.icons().presets,
                *self.nav_icons(),
            )
        )

    def nav_icons(self):
        """Use the icon sequence for the active management screen."""
        icons = self.icons()
        return (
            icons.settings_pages
            if self.filter_options is not None
            else icons.help_pages
        )

    def compact(self):
        """Collapse labels once the two profile actions no longer fit together."""
        return self.sidebar_width() < self.profile.inline_width

    def rail_anchor(self):
        """Find the visible window that owns the current rail caption."""
        if not self.compact() or self.modal or self.searching or not self.rail_label():
            return None
        layout = self.app.layout
        if layout.container.rail_tip:
            return layout.container.rail_tip[0]
        return layout.current_window if self.in_sidebar() else None

    def hover_label(self, control, label):
        """Associate a compact hit target with the physical pointer row."""
        surface = self.app.layout.container
        surface.rail_tip = control, surface.pointer_row, label

    def rail_label(self):
        """Describe the hovered icon or the item reached with the keyboard."""
        layout = self.app.layout
        if layout.container.rail_tip:
            return layout.container.rail_tip[2]
        control = layout.current_control
        if control in self.profile.controls:
            return self.profile.label(control)
        if control is self.page_nav() and not self.pointer_navigation:
            return self.pages[self.index][0]
        return ""

    def resize_sidebar(self, width):
        """Apply the same visible bounds to pointer and keyboard resizing."""
        width = self.sidebar_width(width)
        if self.sidebar_width() == width:
            return NotImplemented
        self.width = width

    def navigation(self, lower=False):
        """Render category groups and mark the active category even in the main pane."""
        compact = self.compact()
        fragments = []
        focused = get_app().layout.current_control is (
            self.extra_nav if lower else self.nav
        )
        if lower and len(self.pages) == self.nav_split:
            return [
                (
                    "class:muted",
                    ""
                    if compact
                    else self.NAV_ITEM_INDENT
                    + i18n.get(Message.MANAGE_EMPTY_INSTALLED),
                )
            ]
        start, stop = (
            (self.nav_split, len(self.pages)) if lower else (0, self.nav_split)
        )
        width = self.sidebar_width() - 2
        for n in range(start, stop):
            title = self.pages[n][0]
            if n != start:
                fragments.append(("", "\n\n" if compact or n in self.groups else "\n"))
            if (
                not compact
                and n in self.groups
                and (self.filter_options is not None or self.extra_nav is None)
            ):
                fragments.append(("class:muted", " " + self.groups[n] + "\n"))
            active = self.index == n
            if active:
                fragments.append(("[SetCursorPosition]", ""))
            target = (self.extra_nav if lower else self.nav, n)
            handler = on_hover(target, partial(self._click_page, n))
            emphasize = active and (
                not compact or focused and not self.pointer_navigation
            )
            style = hover_style(
                target, "class:nav.current" if emphasize else "", "class:nav.current"
            )
            if compact:
                icons = self.nav_icons()
                icon = icons[n] if n < len(icons) else self.icons().extension
                fragments.append((style, icon, handler))
            else:
                if active:
                    fragments.extend(
                        [("", " "), (style, self.icons().large.right + " ", handler)]
                    )
                else:
                    fragments.append(("", self.NAV_ITEM_INDENT))
                fragments.append(
                    (
                        style,
                        fit_caption(title, width - len(self.NAV_ITEM_INDENT)),
                        handler,
                    )
                )
        return fragments

    def _click_page(self, index, event):
        if event.event_type == MouseEventType.MOUSE_MOVE and self.compact():
            control = self.extra_nav if index >= self.nav_split else self.nav
            self.hover_label(control, self.pages[index][0])
        return on_click(
            partial(self.select_page, index, pointer=True), target=self.pages[index][1]
        )(event)

    def focus_navigation(self, *, pointer=False):
        """Keep arrow navigation available after a click without latching its highlight."""
        self.pointer_navigation = pointer
        self.app.layout.focus(self.page_nav())

    def select_page(self, index, *, pointer=False):
        """Switch pages on a single click and keep keyboard navigation in the sidebar."""
        self.leave_edit()
        self.searching = False
        self.index = index
        self.focus_navigation(pointer=pointer)

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

    def open_find(self):
        """Open the current window's search from its toolbar or shortcut."""
        if self.busy:
            return
        if self.filter_options is not None:
            self.open_filter()
            return
        if not self.searching:
            self.return_index = self.index
            self.searching = True
            self.search.text = ""
            self.app.layout.update_parents_relations()
        self.app.layout.focus(self.search)

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
        nav = Condition(
            lambda: self.app.layout.current_control in (self.nav, self.extra_nav)
        )
        profile = Condition(lambda: self.app.layout.has_focus(self.profile))
        searching = Condition(lambda: self.searching)
        filtering = Condition(lambda: self.filtering)
        body = ~nav & ~searching & ~filtering & ~profile

        @bind("left", filter=profile)
        @bind("right", filter=profile)
        @bind("up", filter=profile)
        @bind("down", filter=profile)
        def profile_move(event):
            self.profile.move(event.key_sequence[0].key)

        @bind("tab", filter=profile)
        @bind("s-tab", filter=profile)
        def profile_leave(event):
            self.select_page(
                0 if event.key_sequence[0].key == "tab" else max(self.groups)
            )

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
            was_pointer = self.pointer_navigation
            self.pointer_navigation = False
            step = -1 if event.key_sequence[0].key == "up" else 1
            first = self.group_start()
            last = (
                min((i for i in self.groups if i > self.index), default=len(self.pages))
                - 1
            )
            index = max(first, min(last, self.index + step))
            if index == self.index:
                return None if was_pointer else NotImplemented
            self.index = index

        @bind("c-left", filter=nav)
        @bind("c-right", filter=nav)
        def resize(event):
            self.pointer_navigation = False
            step = -1 if event.key_sequence[0].key == "c-left" else 1
            if step > 0 and self.compact():
                return self.resize_sidebar(self.profile.inline_width)
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

        tools_available = ~filtering & Condition(lambda: not self.busy)
        for button in self.auxiliary:
            bind(self.CTRL_KEYS[button.key], filter=tools_available, eager=True)(
                lambda event, button=button: self.activate_button(button)
            )

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
            pos = groups.index(current) + step
            if not 0 <= pos < len(groups):
                self.app.layout.focus(self.profile.page)
            else:
                self.select_page(groups[pos])

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
            self.focus_navigation()

        @bind(
            "tab",
            filter=body & ~Condition(lambda: palette_focus() and self.editor().picking),
        )
        @bind(
            "s-tab",
            filter=body & ~Condition(lambda: palette_focus() and self.editor().picking),
        )
        def zone(event):
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
                self.focus_navigation()
            elif self.editing is not None or (
                palette_focus() and self.editor().picking
            ):
                self.leave_edit(EditAction.CANCEL)
            elif self.focused_buttons():
                self.focus_body()
            elif not self.in_sidebar():
                self.focus_navigation()
            else:
                self.close()

        if self.dirty is None:
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
