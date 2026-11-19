"""Render explicit management windows without starting a shell or native engine."""

from __future__ import annotations

import asyncio
import copy
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal

from prompt_toolkit.application import get_app, in_terminal
from prompt_toolkit.cache import SimpleCache
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Dimension,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Label, TextArea

from ish.config import config
from ish.lang import Message, i18n
from ish.lang.i18n import valid_language
from ish.log import LOG_LEVELS
from ish.plugin.manager import PluginCategory
from ish.runtime.settings import MAX_ESCAPE_DELAY, MIN_ESCAPE_DELAY, RcError
from ish.terminal.features import FEATURES
from ish.terminal.settings import (
    CLIPBOARD_POLICIES,
    COLOR_DEPTHS,
    LIMIT_MAXIMUM,
    MOUSE_POLICIES,
)

from .content import help_pages, help_text
from .icons import DEFAULT_ICONS, ICON_SETS
from .packages import CatalogReader, PackageList
from .palette_editor import PaletteEditor
from .screen import EditAction, Screen, on_click, reset_clicks
from .scroll import FieldWindow, FormPane
from .store import SettingsStore


def text(value):
    """Keep metadata and file errors as visible text, never terminal controls."""
    value = str(value)
    if value.isprintable():
        return value
    return "".join(
        character if character.isprintable() or character in "\n\t" else "�"
        for character in value
    )


def label(message, *, focusable=False, **values):
    """Build a status window without interpreting plugin or user content as markup."""
    return Window(
        FormattedTextControl(
            text(i18n.get(message, **values)), focusable=focusable, show_cursor=False
        ),
        dont_extend_height=True,
        wrap_lines=True,
    )


class ActionButton(Button):
    """Run a settings action on keyboard activation or a primary mouse click."""

    def __init__(self, message, action):
        """Keep the focus and action colors independent of the shell palette."""
        title = i18n.get(message)
        super().__init__(
            title,
            handler=action,
            width=get_cwidth(title) + 4,
            left_symbol="",
            right_symbol="",
        )
        self.window.always_hide_cursor = Always()

        def button_style():
            app = get_app()
            if getattr(app, "busy", False):
                return "class:button.disabled"
            return (
                "class:button.focused" if app.layout.has_focus(self) else "class:button"
            )

        self.window.style = button_style


@dataclass(frozen=True, slots=True)
class NumberSpec:
    """Parse a numeric draft and step it in the unit shown by the form."""

    parse: type[int] | type[float]
    step: int | Decimal
    minimum: int | float
    maximum: int | float

    def bind(self, field):
        """Step a field's draft only while its editor has input focus."""
        keys = KeyBindings()
        buffer = field.buffer

        @keys.add("up", filter=~buffer.read_only, eager=True)
        @keys.add("down", filter=~buffer.read_only, eager=True)
        def adjust(event):
            try:
                value = self.parse(buffer.text)
            except ValueError:
                return NotImplemented
            if not self.minimum <= value <= self.maximum:
                return NotImplemented

            current = Decimal(str(value))
            step = self.step if event.key_sequence[-1].key == "up" else -self.step
            result = min(
                Decimal(str(self.maximum)),
                max(Decimal(str(self.minimum)), current + step),
            )
            if result == current:
                return NotImplemented
            updated = format(result.normalize(), "f")
            cursor = buffer.cursor_position
            position = (
                len(updated)
                if cursor == len(buffer.text)
                else min(cursor, len(updated))
            )
            buffer.document = Document(updated, cursor_position=position)

        field.control.key_bindings = keys


class TextField(TextArea):
    """Keep one editable value and its last accepted text during an edit."""

    def __init__(self, *, validate, **kwargs):
        """Use the owning settings validator when the user accepts this text."""
        super().__init__(**kwargs)
        self.validate = validate

    def begin(self):
        """Keep PTK double-click selection within one edit of this field."""
        self.original = self.text
        self.buffer.exit_selection()
        reset_clicks(self)

    def accept(self):
        """Leave valid input in the draft, preserving its editable spelling."""
        if self.original != self.text:
            self.validate(self.text)

    def cancel(self):
        """Restore the value at the beginning of this edit only."""
        if self.text != self.original:
            self.text = self.original


class Choice:
    """Keep the accepted field value separate from the highlighted menu item."""

    def __init__(self, values, selected, screen):
        """Show the saved choice until the user opens this field."""
        self.values = list(values)
        self.current_value = selected
        self.screen = screen
        self.position = 0
        keys = KeyBindings()
        self.control = FormattedTextControl(
            lambda: dict(self.values)[self.current_value],
            focusable=True,
            show_cursor=False,
            key_bindings=keys,
        )
        self.window = Window(self.control, height=1, wrap_lines=False)
        self.control.mouse_handler = on_click(self.toggle)

        @keys.add("up", filter=Condition(self.editing))
        @keys.add("down", filter=Condition(self.editing))
        def move(event):
            step = -1 if event.key_sequence[0].key == "up" else 1
            return self.move(step)

        @keys.add(" ", filter=Condition(self.editing))
        def apply(event):
            self.accept()
            self.screen().leave_edit()

    def editing(self):
        """Return whether this selector currently owns the open list."""
        screen = self.screen()
        return screen is not None and screen.editing is self.control

    def begin(self):
        """Place the highlight on the current value when opening the choices."""
        self.position = next(
            i for i, item in enumerate(self.values) if item[0] == self.current_value
        )

    def toggle(self):
        """Open the options, or collapse them while keeping the current draft value."""
        if self.editing():
            self.screen().leave_edit()
        else:
            self.screen().start_edit(self.control)

    def accept(self):
        """Copy the highlighted value into the settings draft."""
        self.current_value = self.values[self.position][0]

    def move(self, step):
        """Highlight a visible candidate without changing the draft."""
        position = max(0, min(len(self.values) - 1, self.position + step))
        if position == self.position:
            return NotImplemented
        self.position = position

    def options(self):
        """Mark the accepted item and tell PTK which candidate to keep visible."""
        rows = []
        for i, (value, title) in enumerate(self.values):
            if rows:
                rows.append(("", "\n"))
            if i == self.position:
                rows.append(("[SetCursorPosition]", ""))

            rows.append(
                (
                    "class:nav.selected" if i == self.position else "",
                    (" ● " if value == self.current_value else " ○ ") + title,
                )
            )
        return rows


class ChoiceMenu(FloatContainer):
    """Paint the active selector above its scrolling page without sizing the form."""

    def __init__(self, page, active):
        """Share one floating list between all the fields in the settings window."""
        self.active = active
        control = FormattedTextControl(
            lambda: self.active().options(), show_cursor=False
        )
        control.mouse_handler = self.choose
        menu = VSplit(
            [
                Window(width=1, char="│", style="class:setting.edge.focused"),
                Window(control, char=" ", wrap_lines=False),
                Window(width=1, char="│", style="class:setting.edge.focused"),
            ],
            style="class:setting.value",
        )
        super().__init__(page, [Float(content=menu, ycursor=True)])

    def choose(self, event):
        """Route the entire candidate row to the item under the mouse."""
        choice = self.active()
        if (
            not isinstance(choice, Choice)
            or self.floats[0].attach_to_window is not choice.window
        ):
            return NotImplemented
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            return choice.move(
                1 if event.event_type == MouseEventType.SCROLL_DOWN else -1
            )
        if event.button != MouseButton.LEFT:
            return NotImplemented
        if event.event_type == MouseEventType.MOUSE_UP:
            choice.position = event.position.y
            choice.accept()
            choice.screen().leave_edit()
        return None

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
    ):
        """Attach the float to the field's physical position after the page scrolls."""
        choice = self.active()
        menu = self.floats[0]
        menu.attach_to_window = None
        if not isinstance(choice, Choice):
            return self.content.write_to_screen(
                screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
            )
        targets = MouseHandlers()
        self.content.write_to_screen(
            screen, targets, write_position, parent_style, erase_bg, z_index
        )
        positions = screen.visible_windows_to_write_positions
        left = positions.get(choice.edges[0])
        right = positions.get(choice.edges[1])
        pos = write_position
        owner = choice.screen()
        rows = owner.pages[owner.index][1].viewport_rows
        if not (left and right and left.ypos in rows):
            # Do not leave an invisible candidate ready to accept after manual scrolling.
            choice.screen().leave_edit()

        def outside(event):
            x, y = event.position
            dismiss = (
                event.event_type == MouseEventType.MOUSE_DOWN
                and event.button == MouseButton.LEFT
                and choice.editing()
                and not (
                    left and right and y == left.ypos and left.xpos <= x <= right.xpos
                )
            )
            if dismiss:
                choice.screen().leave_edit()
            result = targets.mouse_handlers[y][x](event)
            return None if dismiss else result

        mouse_handlers.set_mouse_handler_for_range(
            pos.xpos, pos.xpos + pos.width, pos.ypos, pos.ypos + pos.height, outside
        )
        if choice.editing():
            menu.attach_to_window = choice.window
            menu.left = left.xpos - pos.xpos
            menu.width = right.xpos + right.width - left.xpos
            menu.height = len(choice.values)
            # PTK fits the list below or above the anchor and clips long menus.
            viewport = WritePosition(pos.xpos, rows.start, pos.width, len(rows))
            self._draw_float(
                menu, screen, mouse_handlers, viewport, parent_style, erase_bg, z_index
            )


class ValueBox(VSplit):
    """A settings field whose edges, value width and trailing fill have fixed policies."""

    CACHE_SIZE = 4

    def __init__(self, columns):
        """Reuse allocations while input content or focus changes at the same width."""
        super().__init__(columns, style="class:setting.value")
        self._widths = SimpleCache(maxsize=self.CACHE_SIZE)

    def _divide_widths(self, width):
        # The value column has explicit min/preferred/max widths. Text, focus and
        # popup selection does not change its width. Reuse PTK's exact allocation.
        divide = super()._divide_widths
        return self._widths.get(width, lambda: divide(width))


class OptionRow(HSplit):
    """Reuse PTK's measured height for the same field presentation and geometry."""

    CACHE_SIZE = 8

    def __init__(self, children, widget, editing):
        """Keep a bounded cache with the row and its original editable control."""
        super().__init__(children)
        self.widget = widget
        self.editing = editing
        self._heights = SimpleCache(maxsize=self.CACHE_SIZE)

    def preferred_height(self, width, max_available_height):
        """Measure again when the geometry or the title's edit glyph changes."""
        measure = super().preferred_height
        return self._heights.get(
            (width, max_available_height, self.editing()),
            lambda: measure(width, max_available_height),
        )

    def write_to_screen(self, screen, handlers, position, style, erase, z_index):
        """Keep the real control in the layout even when its pixels can be skipped."""
        draw = super().write_to_screen
        pane = FormPane.current()
        args = (screen, handlers, position, style, erase, z_index)
        if (
            pane is None
            or z_index
            or self.widget.control is get_app().layout.current_control
        ):
            # PTK follows the geometric position and cursor of a focused control.
            draw(*args)
            return

        def paint(rows):
            if (
                position.ypos < rows.stop
                and position.ypos + position.height > rows.start
            ):
                draw(*args)

        pane.paint_viewport(paint)


class SettingsWindow:
    """Edit a draft; only the store owns durable configuration changes."""

    EDITOR_VARIABLES = ("VISUAL", "EDITOR")
    CATALOGS = {
        PluginCategory.PLUGIN: (
            Message.MANAGE_PLUGIN_PAGE,
            Message.MANAGE_PLUGIN_SELECTION,
            Message.MANAGE_PLUGIN_NOTE,
            Message.MANAGE_NO_PLUGINS,
        ),
        PluginCategory.MODULE: (
            Message.MANAGE_MODULE_PAGE,
            Message.MANAGE_MODULE_SELECTION,
            Message.MANAGE_MODULE_NOTE,
            Message.MANAGE_NO_MODULES,
        ),
    }
    PAGE_TITLES = (
        Message.MANAGE_GENERAL,
        Message.MANAGE_PALETTE,
        Message.MANAGE_INPUT,
        Message.MANAGE_FEATURES,
        Message.MANAGE_PLUGIN_PAGE,
        Message.MANAGE_MODULE_PAGE,
    )
    DEFAULT_EDITOR = "vi"
    EXTENSIONS_START = 4
    VALUE_WIDTH = 48
    MIN_VALUE_WIDTH = 6
    OPTION_GAP = 2
    NUMBERS = {
        "escape_delay": NumberSpec(
            float, Decimal("0.001"), MIN_ESCAPE_DELAY, MAX_ESCAPE_DELAY
        ),
        **dict.fromkeys(
            ("scrollback_lines", "scrollback_bytes"),
            NumberSpec(int, 1, 0, LIMIT_MAXIMUM),
        ),
    }

    FEATURE_LABELS = {
        "hyperlinks": Message.MANAGE_FEATURE_HYPERLINKS,
        "extended_styles": Message.MANAGE_FEATURE_EXTENDED_STYLES,
        "overline": Message.MANAGE_FEATURE_OVERLINE,
        "cursor_color": Message.MANAGE_FEATURE_CURSOR_COLOR,
        "iterm_graphics": Message.MANAGE_FEATURE_ITERM_GRAPHICS,
        "kitty_graphics": Message.MANAGE_FEATURE_KITTY_GRAPHICS,
        "sixel_graphics": Message.MANAGE_FEATURE_SIXEL_GRAPHICS,
        "desktop_notifications": Message.MANAGE_FEATURE_DESKTOP_NOTIFICATIONS,
        "synchronized_output": Message.MANAGE_FEATURE_SYNCHRONIZED_OUTPUT,
        "enhanced_keyboard": Message.MANAGE_FEATURE_ENHANCED_KEYBOARD,
        "pixel_mouse": Message.MANAGE_FEATURE_PIXEL_MOUSE,
        "clipboard_protocol": Message.MANAGE_FEATURE_CLIPBOARD_PROTOCOL,
        "scheme_notifications": Message.MANAGE_FEATURE_SCHEME_NOTIFICATIONS,
    }
    POLICIES = {
        "on": Message.MANAGE_ON,
        "off": Message.MANAGE_OFF,
        "auto": Message.MANAGE_AUTO,
        "write": Message.MANAGE_CLIPBOARD_WRITE,
        "read-write": Message.MANAGE_CLIPBOARD_BOTH,
        "inherit": Message.MANAGE_INHERIT,
        **{mode: glyphs.label for mode, glyphs in ICON_SETS.items()},
    }

    def __init__(self, store, *, input=None, output=None):
        """Build editable fields from the one validated settings object."""
        self.store = store
        self.option_queries = {}
        self._catalog = None
        self.reader = CatalogReader(self._catalog_ready)
        self.inputs, self.choices, self.feature_choices = {}, {}, {}
        self.fields = {}
        pages = self.pages()
        self.initial = copy.deepcopy(self.values())
        self.screen = Screen(
            i18n.get(Message.MANAGE_CONFIG_TITLE),
            pages,
            groups=[
                (0, i18n.get(Message.MANAGE_PREFERENCES)),
                (2, i18n.get(Message.MANAGE_TERMINAL_GROUP)),
                (self.EXTENSIONS_START, i18n.get(Message.MANAGE_EXTENSIONS_GROUP)),
            ],
            buttons=[
                ActionButton(Message.MANAGE_CANCEL, self.cancel),
                ActionButton(Message.MANAGE_APPLY_KEY, self.save),
            ],
            auxiliary=[
                (
                    "filter",
                    i18n.get(Message.MANAGE_FILTER),
                    lambda: self.screen.open_filter(),
                ),
                ("refresh", i18n.get(Message.MANAGE_RELOAD), self.reload),
                (
                    "open_file",
                    f"{i18n.get(Message.MANAGE_EDIT_KEY)} ({self.store.path.name})",
                    self.edit,
                ),
            ],
            filter_options=self.filter_options,
            filter_query=self.current_query,
            overlay=lambda base: ChoiceMenu(
                base, lambda: self.fields.get(self.screen.editing)
            ),
            on_press=self.finish_outside_click,
            field_finished=self.field_finished,
            page_selected=self.visit_page,
            loading=lambda: self.reader.request is not None,
            dirty=self.dirty,
            palette=lambda: self.palette.preview(),
            icons=self.icons,
            editor=lambda: self.palette,
            fields=self.fields,
            input=input,
            output=output,
        )
        available = Condition(lambda: not self.screen.filtering)
        self.screen.bindings.add("c-s", filter=available, eager=True)(
            lambda event: self.save()
        )
        self.screen.bindings.add("c-e", filter=available, eager=True)(
            lambda event: self.edit()
        )
        self.screen.bindings.add("c-r", filter=available, eager=True)(
            lambda event: self.reload()
        )
        if store.error:
            self.screen.set_status(
                i18n.get(Message.MANAGE_READ_ERROR, error=store.error), error=True
            )

    def row(self, name, title, widget, content, *, detail=None):
        """Group a setting's title, explanation and editable value as one search result."""
        self.fields[widget.control] = widget
        description = i18n.get(Message[f"MANAGE_DESC_{name.upper()}"])
        if detail:
            description = f"{description}  {detail}"
        click = on_click(
            widget.toggle
            if isinstance(widget, Choice)
            else lambda: self.screen.start_edit(widget.control)
        )
        editing = (
            widget.editing if isinstance(widget, Choice) else ~widget.buffer.read_only
        )

        def focus_style(part):
            suffix = ".focused" if get_app().layout.has_focus(widget.control) else ""
            return f"class:setting.{part}{suffix}"

        def heading():
            prefix = f"{self.icons().edit} " if editing() else ""
            return [(focus_style("title"), prefix + title)]

        def side(symbol, handler):
            side_control = FormattedTextControl(symbol)
            side_control.mouse_handler = handler
            return Window(side_control, width=1, char=" ")

        def edge():
            return Window(char="│", width=1, style=lambda: focus_style("edge"))

        widget.window.char = " "
        controls = [edge(), side(" ", click), content]
        if isinstance(widget, Choice):
            controls.append(
                side(
                    lambda: (
                        self.icons().small.up if editing() else self.icons().small.down
                    ),
                    click,
                )
            )
        elif name in self.NUMBERS:
            controls.append(side(lambda: self.icons().number, click))
        controls.extend([side(" ", click), edge()])
        if isinstance(widget, Choice):
            widget.edges = controls[0], controls[-1]
        else:
            widget.input_span = controls[1], controls[-2]
        fixed_width = len(controls) - 1
        widget.window.width = Dimension(
            min=self.MIN_VALUE_WIDTH - fixed_width,
            preferred=self.VALUE_WIDTH - fixed_width,
            max=self.VALUE_WIDTH - fixed_width,
        )
        controls.append(Window(style="class:surface"))
        row = OptionRow(
            [
                Window(
                    FormattedTextControl(heading),
                    dont_extend_height=True,
                    wrap_lines=True,
                ),
                Label(description, style="class:setting.description"),
                ValueBox(controls),
            ],
            widget,
            editing,
        )

        def matches(query):
            if isinstance(widget, Choice):
                value = widget.current_value
                display = dict(widget.values)[value]
            else:
                value = display = widget.text
            return any(
                query in str(item).casefold()
                for item in (name, title, description, value, display)
                if item is not None
            )

        self.option_matches[row] = matches
        return row

    def form(self, title, parts):
        """Keep the original rows alive so filtering cannot discard their draft."""
        body = HSplit(parts, padding=self.OPTION_GAP)
        empty = Window(
            FormattedTextControl(
                lambda: i18n.get(Message.MANAGE_FILTER_EMPTY),
                focusable=True,
                show_cursor=False,
            ),
            height=1,
        )
        self.option_pages[title] = body, tuple(body.children), empty

    @staticmethod
    def editor_group(title, description, editor):
        """Keep a compound editor together with its own title and instructions."""
        return HSplit(
            [
                Label(i18n.get(title), style="class:setting.title"),
                Label(i18n.get(description), style="class:setting.description"),
                editor,
            ]
        )

    def current_query(self):
        """Return the active page's applied filter for the dialog and status line."""
        return self.option_queries.get(self.PAGE_TITLES[self.screen.index], "")

    def filter_options(self, query):
        """Apply the dialog's query to just the page on which it was opened."""
        title = self.PAGE_TITLES[self.screen.index]
        if query:
            self.option_queries[title] = query
        else:
            self.option_queries.pop(title, None)
        if title == Message.MANAGE_PALETTE:
            self.palette.apply_filter(query)
        for category, (page, *_) in self.CATALOGS.items():
            if title == page and self.plugin_rows is not None:
                self.show_plugins((category,))
                return
        self.filter_form(title)

    def filter_form(self, title):
        """Remove unmatched rows, their explanatory lines and their layout padding."""
        body, rows, empty = self.option_pages[title]
        query = self.option_queries.get(title, "").casefold()
        if query:
            body.children = [
                row for row in rows if self.option_matches[row](query)
            ] or [empty]
        else:
            body.children = list(rows)

    def field(self, name, message, value, placeholder=None):
        """Keep each text field in the draft with its translated label."""

        def editing():
            return (
                hasattr(self, "screen")
                and self.screen.editing is control.control
                and not self.screen.busy
            )

        control = TextField(
            validate=lambda raw: self.store.validated(
                {name: self.input_value(name, raw)}
            ),
            text="" if value is None else str(value),
            multiline=False,
            read_only=Condition(lambda: not editing()),
            height=1,
            wrap_lines=False,
        )
        control.window = FieldWindow(control.control)
        if name in self.NUMBERS:
            self.NUMBERS[name].bind(control)
        mouse_handler = control.control.mouse_handler

        def edit_at(event):
            if event.button != MouseButton.LEFT or self.screen.busy:
                return NotImplemented
            if event.event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
                self.screen.start_edit(control.control)
            return mouse_handler(event)

        control.control.mouse_handler = edit_at
        if placeholder:
            control.control.input_processors.append(
                ConditionalProcessor(
                    BeforeInput([("class:muted", i18n.get(placeholder))]),
                    Condition(lambda: not editing() and not control.text),
                )
            )
        self.inputs[name] = control
        return self.row(name, i18n.get(message), control, control)

    def outside_input(self, frame, point):
        """Identify an open text edit when the pointer is outside its visible box."""
        field = self.screen.active_input()
        if field is None:
            return None
        first, last = getattr(field, "input_span", (field.window, field.window))
        positions = frame.visible_windows_to_write_positions
        left, right = positions.get(first), positions.get(last)
        viewport = self.screen.pages[self.screen.index][1].viewport_rows
        if (
            left
            and right
            and point.y == left.ypos
            and point.y in viewport
            and left.xpos <= point.x < right.xpos + right.width
        ):
            return None
        return field

    def finish_outside_click(self, frame, point):
        """Finish the original edit after the clicked widget handles its release."""
        field = self.outside_input(frame, point)
        if field is None:
            return None

        def release(frame, released_at):
            if released_at == point and self.outside_input(frame, released_at) is field:
                self.screen.leave_edit()
                return True
            return False

        return release

    def choice(self, name, message, values, selected):
        """Expose only allowed policy values; labels never change their meaning."""
        control = Choice(
            [
                (
                    value,
                    i18n.get(self.POLICIES[value])
                    if value in self.POLICIES
                    else str(value),
                )
                for value in values
            ],
            selected,
            lambda: getattr(self, "screen", None),
        )
        self.choices[name] = control
        return self.row(name, i18n.get(message), control, control.window)

    def icons(self):
        """Preview the highlighted icon style while editing that choice."""
        choice = self.choices["ui_icons"]
        mode = (
            choice.values[choice.position][0]
            if choice.editing()
            else choice.current_value
        )
        return ICON_SETS[mode]

    def catalog_page(self, category, *fields):
        """Use the shared package selection in this category's independently filtered view."""
        listing = DynamicContainer(lambda: self.catalog_content[category])
        self.option_matches[listing] = lambda _: (
            self.reader.request is not None
            or self.plugin_error is not None
            or self.catalog_choices.get(category) is not None
        )
        self.form(self.CATALOGS[category][0], [*fields, listing])

    def visit_page(self, index):
        """Read extension metadata on its first visit, outside PTK's render path."""
        if (
            index >= self.EXTENSIONS_START
            and self.plugin_rows is None
            and self.reader.request is None
        ):
            self.refresh_plugins()

    def pages(self):
        """Create category forms while reusing field validation and feature policies."""
        settings = self.store.settings
        self.option_pages, self.option_matches = {}, {}
        languages = {
            "en": i18n.get(Message.MANAGE_LANG_EN),
            "ko": i18n.get(Message.MANAGE_LANG_KO),
        }
        for path in config.LANG_DIR.glob("*.json"):
            if valid_language(path.stem):
                languages.setdefault(path.stem, path.stem)
        if settings.lang:
            languages.setdefault(settings.lang, settings.lang)
        self.language = Choice(
            [(None, i18n.get(Message.MANAGE_LANG_AUTO)), *languages.items()],
            settings.lang,
            lambda: getattr(self, "screen", None),
        )
        self.form(
            Message.MANAGE_GENERAL,
            [
                self.row(
                    "lang",
                    i18n.get(Message.MANAGE_LANG),
                    self.language,
                    self.language.window,
                ),
                self.field("shell", Message.MANAGE_SHELL, settings.shell),
                self.field("cwd", Message.MANAGE_CWD, settings.cwd),
                self.field(
                    "escape_delay", Message.MANAGE_ESCAPE, settings.escape_delay
                ),
                self.choice(
                    "ui_icons", Message.MANAGE_UI_ICONS, ICON_SETS, settings.ui_icons
                ),
                self.field(
                    "log_file",
                    Message.MANAGE_LOG_FILE,
                    settings.log_file,
                    Message.MANAGE_LOG_DISABLED,
                ),
                self.choice(
                    "log_level",
                    Message.MANAGE_LOG_LEVEL,
                    LOG_LEVELS,
                    settings.log_level,
                ),
            ],
        )
        self.catalog_page(
            PluginCategory.PLUGIN,
            self.field(
                "plugin_dir",
                Message.MANAGE_PLUGIN_DIR,
                settings.plugin_dir or config.PLUGIN_SCRIPT_DIR,
            ),
            self.field(
                "plugin_lib_dir",
                Message.MANAGE_LIBRARY_DIR,
                settings.plugin_lib_dir or config.PLUGIN_LIB_DIR,
            ),
        )
        self.catalog_page(PluginCategory.MODULE)
        self.selection = list(settings.plugins)
        self.plugin_rows = self.plugin_error = None
        self.invalid_plugins = {}
        self.catalog_choices = dict.fromkeys(self.CATALOGS)
        self.catalog_content = {
            category: Window(height=0) for category in self.CATALOGS
        }
        feature_parts = []
        for name, spec in FEATURES.items():
            selector = Choice(
                [(value, i18n.get(self.POLICIES[value])) for value in spec.choices],
                settings.features[name],
                lambda: getattr(self, "screen", None),
            )
            self.feature_choices[name] = selector
            feature_parts.append(
                self.row(
                    name,
                    i18n.get(self.FEATURE_LABELS[name]),
                    selector,
                    selector.window,
                    detail=i18n.get(Message.MANAGE_QUERY, query=spec.query)
                    if spec.query
                    else None,
                )
            )
        self.form(Message.MANAGE_FEATURES, feature_parts)
        self.form(
            Message.MANAGE_INPUT,
            [
                self.choice(
                    "mouse", Message.MANAGE_MOUSE, MOUSE_POLICIES, settings.mouse
                ),
                self.choice(
                    "clipboard",
                    Message.MANAGE_CLIPBOARD,
                    CLIPBOARD_POLICIES,
                    settings.clipboard,
                ),
                self.choice(
                    "color_depth",
                    Message.MANAGE_COLOR_DEPTH,
                    COLOR_DEPTHS,
                    settings.color_depth,
                ),
                self.field(
                    "scrollback_lines",
                    Message.MANAGE_SCROLL_LINES,
                    settings.scrollback_lines,
                ),
                self.field(
                    "scrollback_bytes",
                    Message.MANAGE_SCROLL_BYTES,
                    settings.scrollback_bytes,
                ),
            ],
        )
        self.palette = PaletteEditor(
            settings.ui_palette,
            focus=self.focus_palette,
            confirm=lambda: self.screen.leave_edit(EditAction.CONFIRM),
            icons=self.icons,
            width=lambda: (
                self.screen.content_width() - FormPane.BAR_WIDTH - FormPane.BAR_GAP
            ),
        )
        colors = self.editor_group(
            self.palette.TITLE,
            Message.MANAGE_PALETTE_NOTE,
            self.palette.container,
        )
        self.option_matches[colors] = lambda _: bool(self.palette.visible_roles)
        self.form(Message.MANAGE_PALETTE, [colors])
        if Message.MANAGE_PALETTE in self.option_queries:
            self.palette.apply_filter(self.option_queries[Message.MANAGE_PALETTE])
        for title in self.option_queries:
            self.filter_form(title)
        pages = [
            (
                i18n.get(title),
                FormPane(
                    HSplit([Window(height=1), self.option_pages[title][0], Window()]),
                    icons=self.icons,
                    # The palette is one tall control. Follow its selected role or shade.
                    keep_focused_window_visible=title != Message.MANAGE_PALETTE,
                ),
            )
            for title in self.PAGE_TITLES
        ]
        # Catalog rows have fixed heights and only visible lines are painted.
        # PTK's default virtual-height cap would hide packages after row 10,000.
        for _, pane in pages[self.EXTENSIONS_START :]:
            pane.max_available_height = sys.maxsize
        return pages

    def refresh_plugins(self, *, reuse=False):
        """Discover metadata through PluginManager without executing selected code."""
        self._catalog_source = self.inputs["plugin_dir"].text
        source = (
            self._catalog_source,
            self.store.path.parent,
            config.PLUGIN_SCRIPT_DIR,
        )
        cache = self._catalog if reuse else None
        if self.screen.app.is_running:
            self.reader.start(source, cache, self.screen.app)
            self.plugin_rows = self.plugin_error = None
            self.show_plugins()
            self.screen.update_progress()
            self.screen.app.invalidate()
        else:
            try:
                directory, candidates = self.reader.read(source, cache)
            except (OSError, ValueError) as error:
                self._catalog_ready(None, (), error)
            else:
                self._catalog_ready(directory, candidates, None)

    def _catalog_ready(self, directory, candidates, error):
        """Install complete metadata for the latest directory on the PTK thread."""
        self.plugin_error = error
        self._catalog = (directory, candidates) if error is None else None
        rows = {category: [] for category in self.CATALOGS}
        known, invalid = set(), {}
        for candidate in candidates:
            known.add(candidate.name)
            category = PluginCategory.PLUGIN
            if candidate.error:
                invalid[candidate.name] = candidate.error
                title = i18n.get(
                    Message.MANAGE_PLUGIN_UNAVAILABLE,
                    name=candidate.name,
                    error=candidate.error,
                )
            else:
                info = candidate.info
                category = info.category
                title = f"{candidate.name} {info.version}  {info.description}"
            row = candidate.name, " ".join(text(title).split())
            for page in (
                self.CATALOGS if category == PluginCategory.BOTH else (category,)
            ):
                rows[page].append(row)
        for name in self.selection:
            if name not in known:
                invalid[name] = i18n.get(Message.MANAGE_PLUGIN_MISSING)
                title = i18n.get(
                    Message.MANAGE_PLUGIN_UNAVAILABLE, name=name, error=invalid[name]
                )
                rows[PluginCategory.PLUGIN].append(
                    (name, " ".join(text(title).split()))
                )
        self.invalid_plugins = invalid
        # CheckboxList maps the clicked logical line to a plugin index.
        self.plugin_rows = rows
        self.show_plugins()
        screen = self.screen
        layout = screen.app.layout
        layout.update_parents_relations()
        if layout.current_window not in layout.find_all_windows():
            screen.focus_body()
        screen.update_progress()
        screen.app.invalidate()

    def show_plugins(self, categories=None):
        """Filter cached metadata while keeping the full, ordered plugin selection."""
        categories = self.CATALOGS if categories is None else categories
        replaced = [self.catalog_content[category] for category in categories]
        for category in categories:
            title, heading, note, empty = self.CATALOGS[category]
            previous = self.catalog_choices.get(category)
            if previous is not None:
                self.fields.pop(previous.control, None)
            checkbox = (
                self.package_choices(
                    self.plugin_rows[category],
                    self.option_queries.get(title, "").casefold(),
                )
                if self.plugin_rows is not None
                else None
            )
            self.catalog_choices[category] = checkbox
            if self.reader.request is not None:
                content = Window(
                    FormattedTextControl(
                        i18n.get(Message.MANAGE_LOADING),
                        focusable=True,
                        show_cursor=False,
                    ),
                    height=1,
                )
            elif checkbox is not None:
                content = self.editor_group(heading, note, checkbox)
            else:
                content = label(empty, focusable=category == PluginCategory.MODULE)
            if self.plugin_error is not None:
                error = label(
                    Message.MANAGE_ERROR,
                    focusable=category == PluginCategory.MODULE and checkbox is None,
                    error=self.plugin_error,
                )
                content = HSplit([error, content]) if checkbox is not None else error
            self.catalog_content[category] = content
            self.filter_form(title)
        self.screen.release_focus(replaced)

    def package_choices(self, candidates, query):
        """Bind each visible list to one ordered sequence, including rows on both pages."""
        rows = candidates
        if query:
            rows = [
                (name, title) for name, title in candidates if query in title.casefold()
            ]
        if not rows:
            return None
        checkbox = PackageList(rows, self.selection)
        self.fields[checkbox.control] = checkbox
        mouse_handler = checkbox.control.mouse_handler

        def toggle(event):
            if self.screen.busy or event.button != MouseButton.LEFT:
                return NotImplemented
            if event.event_type == MouseEventType.MOUSE_UP:
                self.screen.leave_edit()
                # Finishing a directory edit can replace the list under this click.
                if checkbox is not self.fields.get(checkbox.control):
                    return None
                self.screen.app.layout.focus(checkbox.control)
            return mouse_handler(event)

        checkbox.control.mouse_handler = toggle
        return checkbox

    def focus_palette(self):
        """Move mouse focus to the color picker before applying a selection."""
        self.screen.leave_edit()
        self.screen.app.layout.focus(self.palette.control)

    def _file_work(self, operation):
        """Show the busy state for the file operation and return focus to the UI loop."""
        screen = self.screen
        if screen.busy:
            return
        screen.busy = True

        def perform():
            try:
                operation()
            except (OSError, ValueError, TypeError) as error:
                self.failure(error)
            finally:
                screen.busy = False
                screen.app.invalidate()

        if not screen.app.is_running:
            perform()
            return

        def rendered(app):
            app.after_render -= rendered
            # User rc code can require the main thread, e.g. signal handlers.
            asyncio.get_running_loop().call_soon(perform)

        screen.app.after_render += rendered
        screen.app.invalidate()

    def field_finished(self, control):
        """Show packages from the edited directory while keeping the rest of the draft."""
        source = self.inputs["plugin_dir"]
        if control is source.control and source.text != self._catalog_source:
            self.refresh_plugins(reuse=True)
            layout = self.screen.app.layout
            layout.update_parents_relations()
            if (
                self.current_query()
                and layout.current_window not in layout.find_all_windows()
            ):
                self.screen.focus_body()

    def input_value(self, name, raw):
        """Decode form text; the settings validator owns invalid-value diagnostics."""
        if name in self.NUMBERS:
            try:
                return self.NUMBERS[name].parse(raw)
            except ValueError:
                return raw
        if name in ("plugin_dir", "plugin_lib_dir", "log_file") and not raw:
            return None
        return raw

    def values(self):
        """Read draft controls with field types; the settings owner validates meaning."""
        values = {
            name: self.input_value(name, field.text)
            for name, field in self.inputs.items()
        }
        values.update(
            {name: control.current_value for name, control in self.choices.items()}
        )
        values.update(
            ui_palette=self.palette.values(),
            lang=self.language.current_value,
            plugins=list(self.selection),
            features={
                name: control.current_value
                for name, control in self.feature_choices.items()
            },
        )
        return values

    def changes(self, values=None):
        """Save only edited fields, leaving unrelated Python configuration intact."""
        return {
            name: value
            for name, value in (self.values() if values is None else values).items()
            if value != self.initial[name]
        }

    def dirty(self):
        """Include invalid draft input in the indicator so it cannot appear saved."""
        try:
            return bool(self.changes())
        except (ValueError, TypeError):
            return True

    def failure(self, error):
        """Keep recoverable errors in the window after PTK has acquired the screen."""
        self.screen.set_status(i18n.get(Message.MANAGE_ERROR, error=error), error=True)

    def save(self):
        """Write accepted values and establish a new baseline for further edits."""
        if self.screen.busy:
            return
        if not self.screen.leave_edit(EditAction.SAVE):
            return
        try:
            values = self.values()
            changes = self.changes(values)
            if not changes:
                self.store.save(changes)
                self.screen.set_status("")
                return
            new_plugins = set(changes.get("plugins", ())).difference(
                self.initial["plugins"]
            )
            if new_plugins and self.reader.request is not None:
                self.screen.set_status(
                    i18n.get(Message.MANAGE_SAVE_AFTER_CATALOG), error=True
                )
                return
            for name in changes.get("plugins", []):
                if name in new_plugins and name in self.invalid_plugins:
                    raise ValueError(
                        i18n.get(
                            Message.MANAGE_PLUGIN_UNAVAILABLE,
                            name=name,
                            error=self.invalid_plugins[name],
                        )
                    )
        except (OSError, ValueError, TypeError) as error:
            self.failure(error)
            return

        def write():
            self.store.save(changes)
            self.initial = values
            self.screen.set_status(i18n.get(Message.MANAGE_SAVED))

        self._file_work(write)

    def cancel(self):
        """Restore the last good settings without writing or executing the rc."""
        if self.screen.busy:
            return
        changed = self.dirty()
        self._restore_form(reuse_catalog=True)
        self._status(Message.MANAGE_DISCARDED if changed else None)

    def reload(self):
        """Discard the draft and load the file once, retaining readable error state."""
        self._file_work(self._reload)

    def _reload(self):
        self.store.reload()
        self._restore_form()
        self.screen.app.layout.focus(self.screen.nav)
        self._status(Message.MANAGE_RELOADED)

    def _restore_form(self, *, reuse_catalog=False):
        layout = self.screen.app.layout
        in_form = layout.has_focus(self.screen.pages[self.screen.index][1])
        self.screen.leave_edit(EditAction.CANCEL)
        self.inputs, self.choices, self.feature_choices = {}, {}, {}
        self.fields.clear()
        catalog_opened = self.plugin_rows is not None or self.reader.request is not None
        self.reader.cancel()
        self.screen.pages = self.pages()
        if catalog_opened or not reuse_catalog:
            self.refresh_plugins(reuse=reuse_catalog)
        self.initial = copy.deepcopy(self.values())
        layout.update_parents_relations()
        if in_form:
            self.screen.focus_body()
        self.screen.release_focus()

    def _status(self, message):
        if self.store.error:
            self.screen.set_status(
                i18n.get(Message.MANAGE_READ_ERROR, error=self.store.error), error=True
            )
        else:
            self.screen.set_status(i18n.get(message) if message is not None else "")

    def edit(self):
        """Open the user's editor outside PTK; do not pass arguments through a shell."""
        if self.screen.busy:
            return
        rollback = "" if self.screen.leave_edit() else self.screen.status

        def show_result():
            if rollback and self.screen.status != rollback:
                self.screen.set_status(f"{rollback} {self.screen.status}", error=True)

        try:
            if self.changes():
                raise ValueError(i18n.get(Message.MANAGE_UNSAVED))
            selected = next(
                (
                    os.environ[name]
                    for name in self.EDITOR_VARIABLES
                    if os.environ.get(name)
                ),
                self.DEFAULT_EDITOR,
            )
            arguments = shlex.split(selected)
            if not arguments or shutil.which(arguments[0]) is None:
                raise ValueError(i18n.get(Message.MANAGE_EDITOR_MISSING))
        except (OSError, ValueError) as error:
            self.failure(error)
            show_result()
            return

        self.screen.busy = True
        self.screen.app.invalidate()

        async def open_file():
            try:
                self.store.path.parent.mkdir(parents=True, exist_ok=True)
                async with in_terminal():
                    process = await asyncio.create_subprocess_exec(
                        *arguments, str(self.store.path)
                    )
                    try:
                        status = await process.wait()
                    except BaseException:
                        if process.returncode is None:
                            process.terminate()
                        await process.wait()
                        raise
                self._reload()
                if status:
                    self.screen.set_status(
                        i18n.get(Message.MANAGE_EDITOR_STATUS, status=status),
                        error=True,
                    )
            except (OSError, ValueError) as error:
                self.failure(error)
            finally:
                self.screen.busy = False
                show_result()
                self.screen.app.invalidate()

        self.screen.app.create_background_task(open_file())

    def run(self):
        """Return whether this visit actually replaced the file, regardless of exit key."""
        writes = self.store.writes
        try:
            self.screen.run()
        finally:
            self.reader.cancel()
        return self.store.writes != writes


def help_window(parser, *, no_rc=False):
    """Select a category TUI only for a real terminal; keep redirected help readable."""
    if not os.isatty(0) or not os.isatty(1) or os.environ.get("TERM") == "dumb":
        print(help_text(parser), end="")
        return
    appearance = {} if no_rc else SettingsStore.read_appearance(config.RC_FILE)

    def icons():
        return ICON_SETS[appearance.get("ui_icons", DEFAULT_ICONS)]

    pages = [
        (
            title,
            FormPane(
                HSplit(
                    [
                        Window(height=1),
                        Window(
                            FormattedTextControl(
                                body, focusable=True, show_cursor=False
                            ),
                            wrap_lines=True,
                            always_hide_cursor=True,
                        ),
                    ]
                ),
                icons=icons,
            ),
        )
        for title, body in help_pages(parser)
    ]
    Screen(
        i18n.get(Message.MANAGE_HELP_TITLE),
        pages,
        groups=[
            (0, i18n.get(Message.MANAGE_GUIDE)),
            (6, i18n.get(Message.MANAGE_REFERENCE)),
        ],
        buttons=[
            ActionButton(Message.MANAGE_CLOSE_KEY, lambda: get_app().exit(result=False))
        ],
        palette=lambda: appearance.get("ui_palette", {}),
        icons=icons,
    ).run()


def settings_window(startup, *, no_rc=False, language=None):
    """Reuse the one settings evaluator and never launch plugins, a shell or engine."""
    store = SettingsStore(
        config.RC_FILE, settings_type=startup.settings, loader=startup.load, no_rc=no_rc
    )
    selected = store.settings.lang
    if isinstance(store.error, RcError) and valid_language(store.error.settings.lang):
        selected = store.error.settings.lang
    i18n.load(language if language is not None else selected)
    return SettingsWindow(store).run()
