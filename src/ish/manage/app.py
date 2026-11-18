"""Render explicit management windows without starting a shell or native engine."""

from __future__ import annotations

import asyncio
import copy
import os
import shlex
import shutil
from pathlib import Path

from prompt_toolkit.application import in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    DynamicContainer,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
    to_container,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Button, CheckboxList, Label, RadioList, TextArea

from ish.config import config
from ish.lang import Message, i18n
from ish.lang.i18n import valid_language
from ish.log import LOG_LEVELS
from ish.plugin.manager import PluginManager
from ish.runtime.settings import ESCAPE_DELAY, RcError
from ish.terminal.features import FEATURES
from ish.terminal.settings import CLIPBOARD_POLICIES, COLOR_DEPTHS, MOUSE_POLICIES

from .content import help_pages, help_text
from .host import ManagementApplication
from .store import SettingsStore


def text(value):
    """Keep metadata and file errors as visible text, never terminal controls."""
    return "".join(
        character if character.isprintable() or character in "\n\t" else "�"
        for character in str(value)
    )


def label(message, **values):
    """Translate a label without interpreting plugin or user content as markup."""
    return Label(text(i18n.get(message, **values)))


class Screen:
    """Own navigation, PTK focus and the lifetime of one management screen."""

    NAV_WIDTH = 26
    STYLE = {
        "nav.selected": "reverse",
        "heading": "bold",
        "status": "italic",
        "button.focused": "reverse",
    }

    def __init__(self, title, pages, hint, *, buttons=(), input=None, output=None):
        """Compose category navigation and scrollable content on host default colors."""
        self.pages, self.index, self.status = pages, 0, ""
        self.nav = FormattedTextControl(self.navigation, focusable=True)
        self.bindings = KeyBindings()
        navigation = Condition(lambda: self.app.layout.current_control is self.nav)

        @self.bindings.add("up", filter=navigation)
        def previous(event):
            self.index = (self.index - 1) % len(self.pages)
            event.app.layout.update_parents_relations()

        @self.bindings.add("down", filter=navigation)
        def following(event):
            self.index = (self.index + 1) % len(self.pages)
            event.app.layout.update_parents_relations()

        @self.bindings.add("right", filter=navigation)
        @self.bindings.add("enter", filter=navigation)
        def enter(event):
            event.app.layout.update_parents_relations()
            event.app.layout.focus(self.pages[self.index][1])

        self.bindings.add("tab", eager=True)(lambda event: self.move_focus(1))
        self.bindings.add("s-tab", eager=True)(lambda event: self.move_focus(-1))

        # PTK queues ESC + a key together for Alt bindings. Only a standalone
        # ESC may bypass its longer key-sequence timeout after byte decoding.
        @self.bindings.add(
            "escape", eager=Condition(lambda: not self.app.key_processor.input_queue)
        )
        @self.bindings.add("c-c")
        @self.bindings.add("c-d")
        def close(event):
            event.app.exit(result=False)

        self.app = ManagementApplication(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl([("class:heading", title)]), height=1
                        ),
                        VSplit(
                            [
                                Window(self.nav, width=self.NAV_WIDTH, wrap_lines=True),
                                DynamicContainer(lambda: self.pages[self.index][1]),
                            ]
                        ),
                        VSplit(list(buttons), padding=2, height=1)
                        if buttons
                        else Window(height=0),
                        Window(
                            FormattedTextControl(lambda: text(self.status)),
                            height=2,
                            wrap_lines=True,
                            style="class:status",
                        ),
                        Window(FormattedTextControl(hint), height=2, wrap_lines=True),
                    ]
                ),
                focused_element=self.nav,
            ),
            key_bindings=self.bindings,
            style=Style.from_dict(self.STYLE),
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )
        self.app.ttimeoutlen = ESCAPE_DELAY

    def navigation(self):
        """Show category selection independently of focus in its form."""
        fragments = []
        for index, (title, _) in enumerate(self.pages):
            fragments.extend(
                [
                    (
                        "class:nav.selected" if index == self.index else "",
                        f"{'›' if index == self.index else ' '} {title}",
                    ),
                    ("", "\n"),
                ]
            )
        return fragments

    def move_focus(self, direction):
        """Follow the active layout even when multiple keys precede the next render."""
        layout = self.app.layout
        layout.update_parents_relations()
        windows = list(layout.get_focusable_windows())
        index = (
            windows.index(layout.current_window)
            if layout.current_window in windows
            else 0
        )
        layout.focus(windows[(index + direction) % len(windows)])

    def run(self):
        """Let PTK restore its terminal context on normal close, EOF or interruption."""
        try:
            return self.app.run()
        except (EOFError, KeyboardInterrupt):
            return False


class SettingsWindow:
    """Edit a draft; only the store owns durable configuration changes."""

    EDITOR_VARIABLES = ("VISUAL", "EDITOR")
    DEFAULT_EDITOR = "vi"

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
    }

    def __init__(self, store, *, input=None, output=None):
        """Build editable fields from the one validated settings object."""
        self.store = store
        self.editing = False
        self.inputs, self.choices, self.feature_choices = {}, {}, {}
        self.invalid_plugins = {}
        pages = self.pages()
        self.initial = copy.deepcopy(self.values())
        self.screen = Screen(
            i18n.get(Message.MANAGE_CONFIG_TITLE),
            pages,
            i18n.get(Message.MANAGE_CONFIG_KEYS),
            buttons=[
                Button(i18n.get(Message.MANAGE_SAVE), handler=self.save),
                Button(i18n.get(Message.MANAGE_EDIT), handler=self.edit),
                Button(i18n.get(Message.MANAGE_RELOAD), handler=self.reload),
                Button(
                    i18n.get(Message.MANAGE_CANCEL),
                    handler=lambda: self.screen.app.exit(result=False),
                ),
            ],
            input=input,
            output=output,
        )
        self.screen.status = i18n.get(Message.MANAGE_FILE, path=store.path)
        self.screen.bindings.add("c-s", eager=True)(lambda event: self.save())
        self.screen.bindings.add("c-e", eager=True)(lambda event: self.edit())
        self.screen.bindings.add("c-r", eager=True)(lambda event: self.reload())
        if store.error:
            self.screen.status = i18n.get(Message.MANAGE_READ_ERROR, error=store.error)

    def field(self, name, message, value):
        """Keep each text field in the draft with its translated label."""
        control = TextArea(text="" if value is None else str(value), multiline=False)
        self.inputs[name] = control
        return HSplit([label(message), control])

    def choice(self, name, message, values, selected):
        """Expose only allowed policy values; labels never change their meaning."""
        control = RadioList(
            [
                (
                    value,
                    i18n.get(self.POLICIES[value])
                    if value in self.POLICIES
                    else str(value),
                )
                for value in values
            ],
            default=selected,
        )
        self.choices[name] = control
        return HSplit([label(message), control])

    def pages(self):
        """Create category forms while reusing field validation and feature policies."""
        settings = self.store.settings
        languages = {
            "en": i18n.get(Message.MANAGE_LANG_EN),
            "ko": i18n.get(Message.MANAGE_LANG_KO),
        }
        for path in config.LANG_DIR.glob("*.json"):
            if valid_language(path.stem):
                languages.setdefault(path.stem, path.stem)
        if settings.lang:
            languages.setdefault(settings.lang, settings.lang)
        self.language = RadioList(
            [(None, i18n.get(Message.MANAGE_LANG_AUTO)), *languages.items()],
            default=settings.lang,
        )
        general = HSplit(
            [
                label(Message.MANAGE_NEXT_SESSION),
                label(Message.MANAGE_LANG),
                self.language,
                self.field("shell", Message.MANAGE_SHELL, settings.shell),
                self.field("cwd", Message.MANAGE_CWD, settings.cwd),
                self.field(
                    "escape_delay", Message.MANAGE_ESCAPE, settings.escape_delay
                ),
                self.field("log_file", Message.MANAGE_LOG_FILE, settings.log_file),
                self.choice(
                    "log_level",
                    Message.MANAGE_LOG_LEVEL,
                    LOG_LEVELS,
                    settings.log_level,
                ),
            ],
            padding=1,
        )
        plugin_page = HSplit(
            [
                label(Message.MANAGE_PLUGIN_NOTE),
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
                Button(i18n.get(Message.MANAGE_REFRESH), handler=self.refresh),
                DynamicContainer(lambda: self.plugin_content),
            ],
            padding=1,
        )
        self.selection = list(settings.plugins)
        self.refresh_plugins()
        feature_parts = [label(Message.MANAGE_FEATURE_NOTE)]
        for name, spec in FEATURES.items():
            selector = RadioList(
                [(value, i18n.get(self.POLICIES[value])) for value in spec.choices],
                default=settings.features[name],
            )
            self.feature_choices[name] = selector
            feature_parts.extend(
                [
                    label(self.FEATURE_LABELS[name]),
                    Label(name),
                    label(Message.MANAGE_QUERY, query=spec.query)
                    if spec.query
                    else label(Message.MANAGE_UNQUERIED),
                    selector,
                ]
            )
        input_page = HSplit(
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
            padding=1,
        )
        return [
            (i18n.get(title), ScrollablePane(to_container(Box(body, padding=1))))
            for title, body in (
                (Message.MANAGE_GENERAL, general),
                (Message.MANAGE_PLUGINS, plugin_page),
                (Message.MANAGE_FEATURES, HSplit(feature_parts, padding=1)),
                (Message.MANAGE_INPUT, input_page),
            )
        ]

    def refresh_plugins(self):
        """Discover metadata through PluginManager without executing selected code."""
        if getattr(self, "plugin_choices", None) is not None:
            self.selection = list(self.plugin_choices.current_values)
        directory = Path(self.inputs["plugin_dir"].text).expanduser()
        if not directory.is_absolute():
            directory = self.store.path.parent / directory
        manager = PluginManager(script_dir=directory)
        try:
            candidates = manager.discover()
            self.plugin_error = None
        except (OSError, ValueError) as error:
            candidates = ()
            self.plugin_error = error
        rows, known, invalid = [], set(), {}
        for candidate in candidates:
            known.add(candidate.name)
            if candidate.error:
                invalid[candidate.name] = candidate.error
                title = i18n.get(
                    Message.MANAGE_PLUGIN_UNAVAILABLE,
                    name=candidate.name,
                    error=candidate.error,
                )
            else:
                info = candidate.info
                title = f"{candidate.name} {info.version}\n  {info.description}"
            rows.append((candidate.name, text(title)))
        for name in self.selection:
            if name not in known:
                invalid[name] = i18n.get(Message.MANAGE_PLUGIN_MISSING)
                rows.append(
                    (
                        name,
                        text(
                            i18n.get(
                                Message.MANAGE_PLUGIN_UNAVAILABLE,
                                name=name,
                                error=invalid[name],
                            )
                        ),
                    )
                )
        self.invalid_plugins = invalid
        self.plugin_choices = (
            CheckboxList(rows, default_values=self.selection) if rows else None
        )
        self.plugin_content = HSplit(
            [
                label(Message.MANAGE_ERROR, error=self.plugin_error)
                if self.plugin_error
                else Window(height=0),
                self.plugin_choices or label(Message.MANAGE_NO_PLUGINS),
            ]
        )

    def refresh(self):
        """Refresh only the catalog, retaining current selections and other draft fields."""
        try:
            self.refresh_plugins()
            self.screen.app.layout.update_parents_relations()
            self.screen.app.layout.focus(self.screen.nav)
        except (OSError, ValueError) as error:
            self.failure(error)

    def values(self):
        """Read draft controls with field types; the settings owner validates meaning."""
        values = {name: control.text for name, control in self.inputs.items()}
        for name in ("plugin_dir", "plugin_lib_dir", "log_file"):
            values[name] = values[name] or None
        values["escape_delay"] = float(values["escape_delay"])
        for name in ("scrollback_lines", "scrollback_bytes"):
            values[name] = int(values[name])
        values.update(
            {name: control.current_value for name, control in self.choices.items()}
        )
        values.update(
            lang=self.language.current_value,
            plugins=list(self.plugin_choices.current_values)
            if self.plugin_choices
            else [],
            features={
                name: control.current_value
                for name, control in self.feature_choices.items()
            },
        )
        return values

    def changes(self):
        """Save only edited fields, leaving unrelated Python configuration intact."""
        return {
            name: value
            for name, value in self.values().items()
            if value != self.initial[name]
        }

    def failure(self, error):
        """Keep recoverable errors in the window after PTK has acquired the screen."""
        self.screen.status = i18n.get(Message.MANAGE_ERROR, error=error)
        self.screen.app.invalidate()

    def save(self):
        """Validate and atomically save explicit edits, then close the window."""
        if self.editing:
            return
        try:
            changes = self.changes()
            for name in changes.get("plugins", []):
                if name in self.invalid_plugins and name not in self.initial["plugins"]:
                    raise ValueError(
                        i18n.get(
                            Message.MANAGE_PLUGIN_UNAVAILABLE,
                            name=name,
                            error=self.invalid_plugins[name],
                        )
                    )
            self.store.save(changes)
        except (OSError, ValueError, TypeError) as error:
            self.failure(error)
            return
        self.screen.app.exit(result=True)

    def reload(self):
        """Discard the draft and load the file once, retaining readable error state."""
        self.store.reload()
        self.inputs, self.choices, self.feature_choices = {}, {}, {}
        if hasattr(self, "plugin_choices"):
            del self.plugin_choices
        self.screen.pages = self.pages()
        self.initial = copy.deepcopy(self.values())
        self.screen.app.layout.update_parents_relations()
        self.screen.app.layout.focus(self.screen.nav)
        self.screen.status = (
            i18n.get(Message.MANAGE_READ_ERROR, error=self.store.error)
            if self.store.error
            else i18n.get(Message.MANAGE_RELOADED)
        )
        self.screen.app.invalidate()

    def edit(self):
        """Open the user's editor outside PTK; do not pass arguments through a shell."""
        if self.editing:
            return
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
            return

        self.editing = True

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
                self.reload()
                if status:
                    self.screen.status = i18n.get(
                        Message.MANAGE_EDITOR_STATUS, status=status
                    )
            except (OSError, ValueError) as error:
                self.failure(error)
            finally:
                self.editing = False
                self.screen.app.invalidate()

        self.screen.app.create_background_task(open_file())

    def run(self):
        """Return whether explicit save completed; cancellation never writes."""
        return self.screen.run()


def help_window(parser):
    """Select a category TUI only for a real terminal; keep redirected help readable."""
    if not os.isatty(0) or not os.isatty(1) or os.environ.get("TERM") == "dumb":
        print(help_text(parser), end="")
        return
    pages = [
        (title, TextArea(text=body, read_only=True, scrollbar=True, wrap_lines=True))
        for title, body in help_pages(parser)
    ]
    Screen(
        i18n.get(Message.MANAGE_HELP_TITLE), pages, i18n.get(Message.MANAGE_HELP_KEYS)
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
