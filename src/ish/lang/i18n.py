"""Merge built-in messages with JSON translations and synchronize language changes."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from enum import StrEnum, auto
from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Any, Dict, Optional

from ish.config import config
from ish.log import DEFAULT_LOGGER_NAME

__all__ = ["I18N", "Message"]


_LANGUAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def valid_language(value):
    """Validate the language name shared by configuration and translation lookup."""
    return isinstance(value, str) and _LANGUAGE_NAME.fullmatch(value) is not None


class Message(StrEnum):
    """Name a built-in translation by its public string key and its English fallback."""

    def __new__(cls, key: str, english: str):
        """Use the lowercase member name on disk and keep the English text alongside it."""
        member = str.__new__(cls, key)
        member._value_ = key
        member._english = english
        return member

    @property
    def english(self) -> str:
        """Return the shipped English template for this key."""
        return self._english

    MANAGE_CONFIG_OPTION = auto(), "Open the settings window without starting a shell."
    MANAGE_OPTION_CONFLICT = (
        auto(),
        "--config can be combined only with --home, --lang and --no-rc.",
    )
    MANAGE_HELP_TITLE = auto(), "ish — Help"
    MANAGE_CONFIG_TITLE = auto(), "ish — Settings"
    MANAGE_OVERVIEW = auto(), "Getting started"
    MANAGE_KEYS = auto(), "Keys and navigation"
    MANAGE_SETTINGS = auto(), "Configuration"
    MANAGE_PLUGINS = auto(), "Plugins and supplements"
    MANAGE_FEATURES = auto(), "Host features"
    MANAGE_INPUT = auto(), "Input and display"
    MANAGE_DIAGNOSTICS = auto(), "Diagnostics and logs"
    MANAGE_LIMITS = auto(), "Support and limits"
    MANAGE_OPTIONS = auto(), "Command-line options"
    MANAGE_GENERAL = auto(), "Language and startup"
    MANAGE_HELP_KEYS = auto(), "↑↓ Category   Tab Focus   PgUp/PgDn Scroll   Esc Close"
    MANAGE_CONFIG_KEYS = (
        auto(),
        "Tab Focus   Space Select   Ctrl+S Save   Ctrl+E Edit file   Esc Cancel",
    )
    MANAGE_SAVE = auto(), "Save and close"
    MANAGE_CANCEL = auto(), "Cancel"
    MANAGE_EDIT = auto(), "Edit .ishrc.py"
    MANAGE_RELOAD = auto(), "Reload / discard draft"
    MANAGE_CONFIG_OBJECT = (
        auto(),
        "Assign settings on config; do not replace or delete the config object.",
    )
    MANAGE_REFRESH = auto(), "Refresh list"
    MANAGE_FILE = auto(), "Settings file: {path}"
    MANAGE_ERROR = auto(), "Cannot complete this action: {error}"
    MANAGE_READ_ERROR = (
        auto(),
        "The settings file could not be loaded. Edit the file to fix it; saving is disabled.\n{error}",
    )
    MANAGE_NEXT_SESSION = (
        auto(),
        "This window changes saved choices for the next session. No plugins or native engine are started here.",
    )
    MANAGE_UNSAVED = (
        auto(),
        "There are unsaved changes. Save or cancel before opening or reloading the file.",
    )
    MANAGE_RELOADED = auto(), "Settings reloaded from disk."
    MANAGE_EDITOR_MISSING = (
        auto(),
        "No editor found. Set VISUAL or EDITOR, or install vi.",
    )
    MANAGE_EDITOR_STATUS = auto(), "The editor exited with status {status}."
    MANAGE_LANG = auto(), "Language"
    MANAGE_LANG_AUTO = auto(), "Automatic — LANG, then English"
    MANAGE_LANG_EN = auto(), "English"
    MANAGE_LANG_KO = auto(), "Korean"
    MANAGE_SHELL = auto(), "Shell executable"
    MANAGE_CWD = auto(), "Starting directory"
    MANAGE_LOG_FILE = auto(), "Log file (empty: disabled)"
    MANAGE_LOG_LEVEL = auto(), "Log level"
    MANAGE_ESCAPE = auto(), "ESC / Alt sequence wait (seconds)"
    MANAGE_PLUGIN_DIR = auto(), "Plugin directory"
    MANAGE_LIBRARY_DIR = auto(), "Plugin dependency directory"
    MANAGE_PLUGIN_NOTE = (
        auto(),
        "UI plugins and protocol supplements use the same directory and selection list. Only selected modules load at the next startup. Browsing this list does not run plugin code or install dependencies.",
    )
    MANAGE_NO_PLUGINS = (
        auto(),
        "No plugins found. Choose a directory containing plugin packages and refresh the list.",
    )
    MANAGE_PLUGIN_UNAVAILABLE = auto(), "{name} — unavailable: {error}"
    MANAGE_PLUGIN_MISSING = auto(), "Selected plugin is missing from this directory."
    MANAGE_FEATURE_NOTE = (
        auto(),
        "These are user choices, not detected support. Actual host support has not been queried in this window. Automatic is offered only where ish implements a support query; geometry and restoration requirements still apply to On.",
    )
    MANAGE_ON = auto(), "On"
    MANAGE_OFF = auto(), "Off"
    MANAGE_AUTO = auto(), "Automatic"
    MANAGE_QUERY = auto(), "Support query: {query}"
    MANAGE_UNQUERIED = auto(), "No support query; choose On or Off."
    MANAGE_MOUSE = auto(), "Mouse policy"
    MANAGE_CLIPBOARD = auto(), "Program clipboard access"
    MANAGE_CLIPBOARD_WRITE = auto(), "Write only"
    MANAGE_CLIPBOARD_BOTH = auto(), "Read and write"
    MANAGE_COLOR_DEPTH = auto(), "Color depth"
    MANAGE_INHERIT = auto(), "Inherit PTK / environment"
    MANAGE_SCROLL_LINES = auto(), "Scrollback line budget"
    MANAGE_SCROLL_BYTES = auto(), "Scrollback byte budget (0: disabled)"
    MANAGE_FEATURE_HYPERLINKS = auto(), "Clickable hyperlinks (OSC 8)"
    MANAGE_FEATURE_EXTENDED_STYLES = auto(), "Extended underline styles and colors"
    MANAGE_FEATURE_OVERLINE = auto(), "Overline"
    MANAGE_FEATURE_CURSOR_COLOR = auto(), "Cursor color"
    MANAGE_FEATURE_ITERM_GRAPHICS = auto(), "iTerm image output"
    MANAGE_FEATURE_KITTY_GRAPHICS = auto(), "Kitty image output"
    MANAGE_FEATURE_SIXEL_GRAPHICS = auto(), "SIXEL image output"
    MANAGE_FEATURE_DESKTOP_NOTIFICATIONS = auto(), "Desktop notifications"
    MANAGE_FEATURE_SYNCHRONIZED_OUTPUT = auto(), "Synchronized screen updates"
    MANAGE_FEATURE_ENHANCED_KEYBOARD = auto(), "Extended keyboard protocol"
    MANAGE_FEATURE_PIXEL_MOUSE = auto(), "Pixel mouse coordinates"
    MANAGE_FEATURE_CLIPBOARD_PROTOCOL = auto(), "Extended clipboard protocol"
    MANAGE_FEATURE_SCHEME_NOTIFICATIONS = auto(), "Light / dark theme notifications"
    MANAGE_STORE_CHANGED = (
        auto(),
        "The settings file changed outside this window. Reload it before saving.",
    )
    MANAGE_STORE_BLOCK = (
        auto(),
        "The managed settings block is malformed or is followed by executable code. Edit .ishrc.py before saving.",
    )
    MANAGE_STORE_INVALID = auto(), "Fix the settings file before saving: {error}"
    MANAGE_STORE_FIELD = auto(), "This setting cannot be edited here: {name}"
    MANAGE_STORE_VALUE = (
        auto(),
        "This value cannot be saved as a Python literal: {name}",
    )
    MANAGE_STORE_LINK = (
        auto(),
        "The settings path or its link target changed. Reload before saving.",
    )
    MANAGE_HELP_OVERVIEW = (
        auto(),
        "Run ish to start your native shell in the libghostty + PTK terminal. The default adds no bar, popup, shortcut or shell hook.\n\nExamples:\n  ish\n  ish --shell bash\n  ish --shell zsh --cwd ~/work\n  ish --shell tcsh --no-plugins\n  ish --config\n\nThe shell edits command lines, continuation and heredocs. Programs keep their normal PTY stdin. Bash, zsh and tcsh are the tested shells on Linux/WSL.",
    )
    MANAGE_HELP_KEYS_BODY = (
        auto(),
        "Shell and program keys normally go to their PTY. Enter, Ctrl+C and Ctrl+D keep native shell meaning. When a key starts with a lone ESC byte, the default wait is {escape_delay} seconds for a possible suffix. Increase escape_delay if Alt or arrow keys split on a slow connection. A complete enhanced Escape key report skips this wait.\n\nShift+PageUp / Shift+PageDown scroll internal history. Mouse off leaves dragging to the host. Automatic captures only when a program or explicitly selected UI requests it.\n\nA selected popup plugin can reserve a key globally, including inside Vim. Closing a popup restores input focus; it does not undo input already sent to a program.\n\nIn these management windows: Tab changes focus, arrows select categories or choices, Space toggles choices, PageUp/PageDown scroll, Esc closes. In settings, Ctrl+S saves and Ctrl+E opens .ishrc.py.",
    )
    MANAGE_HELP_SETTINGS_BODY = (
        auto(),
        "Use ish --config to edit language, selected plugins, host features and other session choices. Save and close writes only edited values; Cancel does not write. Changes apply at the next startup.\n\nThe one settings file is ~/ish/.ishrc.py, or DIR/.ishrc.py with --home DIR. It is Python code evaluated once when starting a session or loading the settings window. Help, version and diagnose never evaluate it.\n\nExample:\n  config.lang = 'ko'\n  config.plugins = []\n  config.mouse = 'auto'\n  config.features = {'hyperlinks': 'on'}\n\nExplicit CLI choices override this file. Language order is explicit setting (CLI first), LANG, then English. --no-rc skips the file. The settings window preserves your Python code and appends one managed block, backs up the prior file and rejects conflicting edits.\n\nEdit .ishrc.py uses VISUAL, EDITOR, or vi without invoking a shell. Save or cancel draft changes before editing the file.",
    )
    MANAGE_HELP_PLUGINS_BODY = (
        auto(),
        "UI plugins and protocol supplements share PluginManager, plugin_dir and config.plugins. No second loader or directory is needed.\n\nDefault sources: ~/ish/plugin/script\nDefault dependencies: ~/ish/plugin/lib\n\nEach plugin is a directory with NAME.py or __init__.py and literal PLUGIN_META metadata. Put a trusted package there, refresh the settings list, and select it. Turning it off removes it from the saved selection; source files stay on disk.\n\nA plugin may add bars, popups, search, keys, output protocols or a clipboard provider through the existing API. Configuring a protocol does not enable a UI plugin. Python changes apply at the next session without rebuilding the native engine.\n\n--plugin NAME selects this session's list; --no-plugins disables all. Plugin code is trusted Python and can install its declared dependencies when actually loaded, never while merely browsing settings.",
    )
    MANAGE_HELP_FEATURES_BODY = (
        auto(),
        "Every feature offers On and Off. Automatic exists only for features with an implemented support query. User policy, query response and actual use are separate facts.\n\nExample:\n  ish --feature hyperlinks=on --feature enhanced_keyboard=auto\n  config.features = {'kitty_graphics': 'off'}\n\nOn does not prove host support and cannot bypass image geometry or cursor restoration prerequisites. Unknown support does not turn an automatic feature on. Host names are not a general allow list.\n\nThe settings page lists every feature and its query, where available. It does not send queries or claim a connected host supports them.",
    )
    MANAGE_HELP_INPUT_BODY = (
        auto(),
        "Mouse: off preserves host drag selection; auto captures program/UI requests; on also enables local selection. The host can select only currently displayed cells, not hidden native history or content behind a popup.\n\nClipboard defaults to write-only access for programs. Use read-write only when reads are wanted. This is separate from the host terminal's usual copy/paste. The optional clipboard_system supplement can connect Windows/WSL or an available X11 selection. A remote session needs an actual clipboard transport.\n\nColor depth defaults to inherit from PTK/environment. Use --color-depth 24 for explicit RGB. Scrollback line/byte budgets are approximate native page budgets; a byte budget of 0 disables history.",
    )
    MANAGE_HELP_DIAGNOSTICS_BODY = (
        auto(),
        "ish --version\nish --diagnose\nish --log-file ./ish.log --log-level INFO\n\nHelp, version and diagnostics do not evaluate .ishrc.py, load plugins, start a shell or load the native engine. Diagnostics report defaults and CLI choices, not evaluated saved settings or measured host support.\n\nFile logs are optional. DEBUG, INFO, WARNING, ERROR and CRITICAL are available. Logs contain diagnostics, not native input/output or clipboard content. Console diagnostics are emitted outside the active terminal screen.",
    )
    MANAGE_HELP_LIMITS_BODY = (
        auto(),
        "Current validation targets Linux/WSL with Bash, zsh and tcsh. This is not a claim that every host, operating system or escape sequence is supported.\n\nSIXEL, Kitty and iTerm images use native storage and the selected host output protocol. SIXEL text writes clear the overlapped image cells; origin/horizontal-margin input remains unsupported. Partial alpha over text requires a host output protocol that supports it. Transparent defaults use the host's background. Some ambiguous legacy keys cannot be distinguished from terminal replies and are not guessed as input.\n\nDesktop notifications are off by default. Optional command navigation uses existing OSC 133 markers and does nothing without them; ish installs no shell hooks.\n\nNormal exit restores the host state ish observed and changed. The final application title is intentionally retained. SIGKILL or loss of the host cannot guarantee restoration.\n\nThe shell owns command editing; ish composes the screen. Host features cannot all be passed through unchanged while also providing screen composition.",
    )

    CLI_ARGUMENTS_TITLE = auto(), "positional arguments"
    CLI_OPTIONS_TITLE = auto(), "options"
    CLI_HELP_OPTION_HELP = auto(), "Show this help message and exit."
    CLI_LANG_OPTION_HELP = (
        auto(),
        "Set the interface language. Otherwise use LANG, with English fallback.",
    )
    CLI_VERSION_OPTION_HELP = auto(), "Show the ish version and exit."
    CLI_HOME_OPTION_HELP = (
        auto(),
        "Use DIR for ish settings, plugins, logs, and cache (default: ~/ish). Does not change HOME.",
    )
    CLI_HOME_ERROR = auto(), "Cannot use ish home {path!r}: {error}"
    CLI_DIAGNOSE_OPTION_HELP = (
        auto(),
        "Print environment diagnostics and exit without loading user code or starting a shell.",
    )
    CLI_DIAGNOSE_VERSION = auto(), "ish version"
    CLI_DIAGNOSE_RUNTIME = auto(), "Runtime"
    CLI_DIAGNOSE_SOURCE = auto(), "Python source"
    CLI_DIAGNOSE_BUNDLED = auto(), "Bundled CPython"
    CLI_DIAGNOSE_PYTHON = auto(), "Python version"
    CLI_DIAGNOSE_EXECUTABLE = auto(), "Executable"
    CLI_DIAGNOSE_PLATFORM = auto(), "Platform"
    CLI_DIAGNOSE_LIBC = auto(), "Host libc"
    CLI_DIAGNOSE_SHELL = auto(), "Requested shell"
    CLI_DIAGNOSE_SHELL_PATH = auto(), "Shell path"
    CLI_DIAGNOSE_SHELL_REALPATH = auto(), "Resolved shell path"
    CLI_DIAGNOSE_MISSING = auto(), "Not found"
    CLI_DIAGNOSE_UNAVAILABLE = auto(), "Unavailable"
    CLI_DIAGNOSE_HOME = auto(), "ish home"
    CLI_DIAGNOSE_RC = auto(), "Startup file"
    CLI_DIAGNOSE_RC_POLICY = auto(), "Startup file loading"
    CLI_DIAGNOSE_PLUGINS = auto(), "Plugin directory"
    CLI_DIAGNOSE_DISABLED = auto(), "Disabled"
    CLI_DIAGNOSE_LANGUAGE = auto(), "Selected language"
    CLI_DIAGNOSE_LANGUAGE_DIR = auto(), "Language directory"
    CLI_DIAGNOSE_CACHE = auto(), "Session cache"
    CLI_DIAGNOSE_CACHE_ACCESS = auto(), "Cache directory access"
    CLI_DIAGNOSE_CACHE_WRITABLE = (
        auto(),
        "Write/search access to {path}; creation and execution were not tested",
    )
    CLI_DIAGNOSE_CACHE_DENIED = auto(), "No directory write/search access to {path}"
    CLI_DIAGNOSE_CACHE_NOEXEC = auto(), "Cache filesystem noexec"
    CLI_DIAGNOSE_YES = auto(), "Yes"
    CLI_DIAGNOSE_NO = auto(), "No"
    CLI_DIAGNOSE_TERMINAL = auto(), "TERM"
    CLI_DIAGNOSE_STDIN_TTY = auto(), "Standard input is a TTY"
    CLI_DIAGNOSE_STDOUT_TTY = auto(), "Standard output is a TTY"
    CLI_DIAGNOSE_TERMINAL_SIZE = auto(), "Terminal size"
    CLI_DIAGNOSE_TERMINAL_DIMENSIONS = auto(), "{columns} columns x {rows} rows"
    PM_LOAD_ERROR = (
        auto(),
        "An Error has been occurred while resolving plugin {plugin_file}:\r\n{error}",
    )
    PM_LOAD_FAIL = auto(), "Failed to load plugin '{plugin_file}'"
    PM_EXPECT_STRING = auto(), "expected a string"
    PM_EXPECT_STRINGS = auto(), "expected a list or tuple of strings"
    PM_META_MAPPING = auto(), "{field} must be a mapping"
    PM_META_LITERAL = auto(), "{field} must be literal"
    PM_NAME_INVALID = auto(), "A plugin name must identify one visible source directory"
    PM_VERSION_INVALID = (
        auto(),
        "Invalid version requirement {requirement!r} for {version!r}: {error}",
    )
    PM_REQUIREMENT_INVALID = (
        auto(),
        "Invalid Python requirement {requirement!r}: {error}",
    )
    PM_INSTALLED_INVALID = (
        auto(),
        "Installed dependency does not provide the requested import and version",
    )
    PM_ENTRY_CONFLICT = (
        auto(),
        "Plugin entry {name!r} conflicts with an existing module",
    )
    PM_NAMESPACE_CONFLICT = (
        auto(),
        "Plugin namespace {name!r} is owned by another package",
    )
    PM_SELECTION_INVALID = auto(), "Selected plugins must be a sequence of names"
    PM_INSTALL_BUSY = (
        auto(),
        "Another dependency installation is using {path}; retry after it finishes",
    )
    PM_PACKAGE_BUSY = (
        auto(),
        "Package {name!r} is in use; restart ish before replacing it",
    )
    PM_MODULE_BUSY = (
        auto(),
        "Module {name!r} is in use; restart ish before replacing it",
    )
    PM_PIP_EMPTY = auto(), "pip did not produce an installed distribution"
    PM_DEPENDENCY_BUSY = (
        auto(),
        "Dependency {name!r} is in use; restart ish before changing its version",
    )
    PM_FILE_OWNERSHIP = (
        auto(),
        "Dependency installation would overwrite files owned by another package",
    )
    PM_PATH_OUTSIDE = auto(), "Dependency path leaves the plugin directory: {path}"
    PM_PATH_DIRECTORY = (
        auto(),
        "Dependency file conflicts with an existing directory: {path}",
    )
    PM_DEBUG_PATHS = auto(), "sys.path count: {count}\n{paths}"
    PM_DEBUG_DIRECTORY_READ = auto(), "Error reading directory"
    PM_DEBUG_LIBRARY = (
        auto(),
        "Plugin Lib Dir exists: {exists}\nPlugin Lib Dir path: {path}\nPlugin Lib Dir contents (top {limit}): {contents}",
    )
    PM_DEBUG_PYTHON = (
        auto(),
        "Python Executable: {executable}\nPython Version: {version}",
    )
    PM_DEBUG_TITLE = auto(), "Debug Info"
    PM_DEBUG_SYS_PATH = auto(), "Sys Path"
    PM_DEBUG_LIB_DIR = auto(), "Plugin Lib Dir"
    PM_DEBUG_TRACEBACK = auto(), "Traceback"
    PIP_INSTALL_FAIL = auto(), "Failed to install library '{library}'"

    # Translation validation and diagnostics.
    I18N_LANGUAGE_ERROR = (
        auto(),
        "Language must be a single language code, such as en or ko",
    )
    I18N_MESSAGES_ERROR = auto(), "Language messages must map strings to strings"
    I18N_FIELDS_ERROR = auto(), "Translation placeholders do not match {name}"
    I18N_FORMAT_ERROR = auto(), "Invalid translation template for {name}: {error}"
    I18N_LOAD_ERROR = auto(), "Cannot load language file {path}: {error}"

    # Shared logging and native transport diagnostics.
    LOG_DESTINATION = auto(), "Log destination must be a regular file"
    LOG_OUTPUT_ERROR = auto(), "Log output failed: {error}"
    LOG_LEVEL_ERROR = auto(), "Unknown log level: {value}"
    WRITER_CLOSED = auto(), "Writer is closed"
    WRITE_LIMIT = auto(), "Pending terminal writes exceed the byte limit"
    WRITE_EMPTY = auto(), "write returned zero bytes"
    SESSION_STARTED = auto(), "The terminal session has already started"
    INPUT_LIMIT = auto(), "Pending startup input exceeds the byte limit"

    # Native-stream startup and UI messages.
    STARTUP_SETTING_LANG = auto(), "lang must be a nonempty language code or None"
    STARTUP_SETTING_SHELL = auto(), "shell must be a nonempty executable name or path"
    STARTUP_SETTING_LIST = auto(), "{name} must be a list of strings"
    STARTUP_SETTING_PLUGINS = auto(), "plugins must contain nonempty names"
    STARTUP_SETTING_PATH = auto(), "{name} must be a path"
    STARTUP_SETTING_LEVEL = auto(), "log_level must be one of {levels}"
    STARTUP_SETTING_ESCAPE = (
        auto(),
        "escape_delay must be between {minimum} and {maximum} seconds",
    )
    STARTUP_RC_FILE_ERROR = auto(), "configuration must be a regular Python file"
    STARTUP_RC_ERROR = auto(), "Cannot load configuration {path}: {error}"
    STARTUP_ARG_ERROR = auto(), "argument {argument}: {error}"
    STARTUP_ARG_UNKNOWN = auto(), "unrecognized arguments: {arguments}"
    STARTUP_ARG_EXPECTED = auto(), "expected one argument"
    STARTUP_ARG_CONFLICT = auto(), "not allowed with argument {argument}"
    STARTUP_ARG_CHOICE = auto(), "invalid choice: {value} (choose from {choices})"
    STARTUP_ARG_VALUE = auto(), "invalid {kind} value: {value}"
    STARTUP_ARG_EXPLICIT = auto(), "ignored explicit argument {value}"
    STARTUP_META_DIR = auto(), "DIR"
    STARTUP_META_SECONDS = auto(), "SECONDS"
    STARTUP_META_SHELL = auto(), "SHELL"
    STARTUP_META_NAME = auto(), "NAME"
    STARTUP_META_PATH = auto(), "PATH"
    STARTUP_META_ARG = auto(), "ARG"
    STARTUP_META_LANG = auto(), "LANG"
    STARTUP_NO_RC_HELP = auto(), "Skip ish's .ishrc.py for this session."
    STARTUP_NO_PLUGINS_HELP = auto(), "Disable all selected plugins for this session."
    STARTUP_ESCAPE_DELAY_HELP = (
        auto(),
        "Wait SECONDS for an ambiguous ESC prefix to complete (default {escape_delay}).",
    )
    STARTUP_RC_NOT_EXECUTED = auto(), "Not executed (read-only query)"
    STARTUP_DIAGNOSE_ESCAPE_DELAY = auto(), "Escape sequence delay (seconds)"
    STARTUP_USAGE = auto(), "usage: "
    STARTUP_PARSE_ERROR = auto(), "error: {error}"
    STARTUP_SHELL_HELP = (
        auto(),
        "Run SHELL; the account's login shell is selected by default.",
    )
    STARTUP_CWD_HELP = auto(), "Start the shell in DIR (default: current directory)."
    STARTUP_PLUGIN_HELP = (
        auto(),
        "Select Python plugins instead of the rc list; repeat to select more.",
    )
    STARTUP_PLUGIN_DIR_HELP = (
        auto(),
        "Read selected Python plugins from DIR instead of the bundled directory.",
    )
    STARTUP_PLUGIN_LIB_HELP = auto(), "Use DIR for plugin Python dependencies."
    STARTUP_SHELL_ARG_HELP = (
        auto(),
        "Replace the default -i shell arguments; repeat as --shell-arg=ARG.",
    )
    STARTUP_LOG_FILE_HELP = (
        auto(),
        "Append logs to PATH; without PATH use ish's configured log file.",
    )
    STARTUP_LOG_LEVEL_HELP = auto(), "Set the log threshold (default: WARNING)."
    STARTUP_PLATFORM_ERROR = (
        auto(),
        "ish requires Linux/WSL with a controlling PTY.",
    )
    STARTUP_TTY_ERROR = auto(), "Standard input and output must be terminals."
    STARTUP_SHELL_ERROR = auto(), "Shell executable not found: {shell}"
    STARTUP_CWD_ERROR = auto(), "Working directory not found: {path}"
    STARTUP_DIAGNOSE_CWD = auto(), "Shell working directory"
    STARTUP_DIAGNOSE_ARGS = auto(), "Shell arguments"
    STARTUP_DIAGNOSE_SELECTED_PLUGINS = auto(), "Selected plugins"
    STARTUP_DIAGNOSE_PLUGIN_LIB = auto(), "Plugin library directory"
    STARTUP_DIAGNOSE_LOG_FILE = auto(), "Log file"
    STARTUP_DIAGNOSE_LOG_LEVEL = auto(), "Log threshold"

    # Optional plugin registration, rendering and shell integration.
    STARTUP_PLUGIN_CLEANUP = auto(), "Plugin setup must return a cleanup or None"

    # Native-stream diagnostics and optional sample UI.
    STARTUP_SAMPLE_POPUP = auto(), "Plugin popup | Enter accepts | Esc cancels"

    # Hookless VT plugin selection and optional sample UI.
    VT_PLUGIN_HELP = (
        auto(),
        "Load terminal plugin NAME; repeat to select more (none by default).",
    )
    VT_PLUGIN_DIR_HELP = (
        auto(),
        "Read selected terminal plugins from DIR (default: {path}).",
    )
    VT_PLUGIN_ONCE = auto(), "Terminal plugins can only be loaded once"
    VT_PLUGIN_MISSING = auto(), "Cannot load terminal plugin: {name}"
    VT_PLUGIN_API = auto(), "Unsupported terminal plugin API: {name}"
    VT_PLUGIN_SETUP = auto(), "Terminal plugin has no setup function: {name}"
    VT_PLUGIN_CLOSED = auto(), "Plugin UI is closed"
    VT_PLUGIN_KEY = auto(), "Terminal UI shortcuts reserve one key, not input prefixes"
    VT_BAR_HEIGHT = auto(), "Invalid bar height"
    VT_POPUP_UNAVAILABLE = auto(), "Cannot open another popup"
    VT_POPUP_GEOMETRY = auto(), "Popup geometry must be positive"
    VT_SAMPLE_BAR = auto(), "ish | native terminal | F2 popup"
    VT_DESCRIPTION = (
        auto(),
        "Run a native shell in a hookless libghostty-vt + PTK terminal; no UI plugins by default.",
    )
    VT_VERSION = auto(), "ish {version}"
    VT_LOGIN_HELP = (
        auto(),
        "Start the native shell with -l; shell startup files remain unchanged.",
    )
    VT_MOUSE_HELP = (
        auto(),
        "Mouse capture: off, auto (child or plugin request), or on (including local selection). Default: auto.",
    )
    VT_CLIPBOARD_HELP = (
        auto(),
        "Child clipboard access: off, write, or read-write (default: write; host permissions apply).",
    )
    VT_SCROLLBACK_LINES_HELP = (
        auto(),
        "Approximate native history line limit (default: 10000; page granularity).",
    )
    VT_SCROLLBACK_BYTES_HELP = (
        auto(),
        "Native history byte limit (default: 33554432; zero disables history).",
    )
    VT_SETTING_CHOICE = auto(), "{name} must be one of: {choices}"
    VT_SETTING_LIMIT = auto(), "{name} must be an integer from 0 to {maximum}"
    VT_SETTING_UNKNOWN = auto(), "Unknown setting: {name}"
    VT_FEATURES_MAPPING = (
        auto(),
        "config.features must be a dictionary of feature names and on/off/auto policies",
    )
    VT_FEATURE_ASSIGNMENT = auto(), "Feature selection must use NAME=on|off|auto"
    VT_FEATURE_HELP = (
        auto(),
        "Set one host feature to on, off, or auto; repeat for multiple features. CLI overrides the matching config.features entry. Auto uses implemented support queries only. clipboard_protocol selects OSC5522 host transport, not clipboard access permission.",
    )
    VT_DIAGNOSE_FEATURES = auto(), "Requested feature policies (host not probed)"
    VT_FEATURE_STATUS = (
        auto(),
        "{name}: policy={policy}, support={support}, active={active}, reason={reason}",
    )
    VT_DIAGNOSE_TITLE = auto(), "ish environment diagnostics"
    VT_DIAGNOSE_SCOPE = (
        auto(),
        "Read-only configuration report: rc, plugins, shell and native library are not executed; host features are not probed.",
    )
    VT_DIAGNOSE_NAMESPACE = auto(), "Settings object"
    VT_DIAGNOSE_MOUSE = auto(), "Mouse capture"
    VT_CURSOR_DEFAULT = auto(), "Invalid default cursor shape or blink setting"
    VT_LIBRARY_LOAD = (
        auto(),
        "Cannot load {path}. Run tools/build_ghostty_vt.py in Linux/WSL, or set {variable} to the built libish_vt.so. {error}",
    )
    VT_ENGINE_REBUILD = auto(), "Rebuild with tools/build_ghostty_vt.py."
    VT_ENGINE_ENTRY = auto(), "Missing native metadata entry point {name}. {rebuild}"
    VT_ENGINE_VERSION = auto(), "Invalid native version from {name}. {rebuild}"
    VT_ENGINE_ABI = (
        auto(),
        "Native bridge ABI {actual!r} does not match required ABI {expected}. {rebuild}",
    )
    VT_ENGINE_UNVERIFIED = (
        auto(),
        "Unverified libghostty-vt artifact: headers={headers}, runtime={version}; this build requires {expected}. {rebuild}",
    )
    VT_ENGINE_FEATURE = auto(), "Native build lacks required feature {name}. {rebuild}"
    VT_FRAMING_DROPS = (
        auto(),
        "Oversized terminal controls discarded: {count} (payload omitted)",
    )
    VT_DIAGNOSE_COLOR_DEPTH = auto(), "Color depth policy"
    VT_COLOR_DEPTH_HELP = (
        auto(),
        "Output color depth: inherit, 1, 4, 8, or 24 bits (default: inherit PTK/environment settings; this does not probe host support).",
    )
    VT_DIAGNOSE_CLIPBOARD = auto(), "Child clipboard access"
    VT_CLIPBOARD_LOCKED = (
        auto(),
        "Clipboard providers must be registered before the session starts",
    )
    VT_CLIPBOARD_DUPLICATE = auto(), "Only one clipboard provider can be selected"
    VT_CLIPBOARD_DELIVERY = auto(), "Clipboard response delivery failed"
    VT_CLIPBOARD_PROVIDER = (
        auto(),
        "Clipboard provider must define async read/write/aclose and close methods",
    )
    VT_CLIPBOARD_FAILURES = (
        auto(),
        "Clipboard connection failures: {count} (contents omitted)",
    )
    VT_KEY_MAP_LATE = (
        auto(),
        "Terminal key mappings are fixed before the first host input.",
    )
    VT_KEY_MAP_NAME = auto(), "Invalid or duplicate terminal key mapping name."
    VT_KEY_MAP_LIMIT = (
        auto(),
        "Terminal key mappings exceed the registration limit or are empty.",
    )
    VT_KEY_MAP_SEQUENCE = (
        auto(),
        "Terminal key mappings require complete numeric CSI ~ sequences.",
    )
    VT_KEY_MAP_RESERVED = (
        auto(),
        "Terminal key mapping overlaps a native key or reserved control sequence.",
    )
    VT_KEY_MAP_CONFLICT = (
        auto(),
        "Terminal key mapping is already owned by another registration.",
    )
    VT_KEY_MAP_EVENT = (
        auto(),
        "Terminal key mappings require a supported, consistent press event.",
    )
    VT_INPUT_DROPS = (
        auto(),
        "Unsupported host controls discarded: {count} (contents omitted)",
    )
    VT_DIAGNOSE_SCROLLBACK_LINES = auto(), "History line limit"
    VT_DIAGNOSE_SCROLLBACK_BYTES = auto(), "History byte limit"
    VT_DIAGNOSE_ENGINE = auto(), "Required native engine version"
    VT_DIAGNOSE_ABI = auto(), "Required native bridge ABI"
    VT_DIAGNOSE_LIBRARY = auto(), "Native library path"
    VT_DIAGNOSE_LIBRARY_PRESENT = auto(), "Native library file exists (not loaded)"
    VT_SESSION_START = auto(), "VT session start shell={shell} plugins={plugins}"
    VT_SESSION_EXIT = auto(), "VT session exit status={status}"
    VT_SESSION_ERROR = auto(), "ish session failed: {error}"
    VT_NATIVE_LOGS = (
        auto(),
        "Native diagnostics (payloads omitted): error={error}, warning={warning}, info={info}, debug={debug}",
    )
    VT_SEARCH_BUDGET = auto(), "Search budget must be between 1 and 64"
    VT_STATE_LOST = auto(), "libghostty-vt could not preserve terminal state"
    VT_SEARCH_TITLE = auto(), "Search scrollback"
    VT_SEARCH_HINT = auto(), "Enter: older · Ctrl+P: newer · Esc: close"
    VT_SEARCH_COUNT = auto(), "{count} matches"
    VT_SEARCH_ERROR = auto(), "Search failed: {error}"
    VT_PROTOCOL_LATE = auto(), "Protocols must be registered before terminal input"
    VT_PROTOCOL_NAME = auto(), "Invalid protocol name"
    VT_PROTOCOL_SELECTORS = (
        auto(),
        "Expected a control-string kind and tuple of prefixes",
    )
    VT_PROTOCOL_PREFIXES = auto(), "Invalid protocol prefixes"
    VT_PROTOCOL_OVERLAP = auto(), "Overlapping protocol prefixes"
    VT_PROTOCOL_LIMIT = auto(), "Too many protocol registrations"
    VT_PROTOCOL_CONFLICT = auto(), "Protocol registration conflicts with {name}"
    VT_PROTOCOL_PROVIDER = auto(), "Invalid protocol provider"
    VT_PROTOCOL_FACTORY = auto(), "Protocol adapter requires a factory"
    VT_PROTOCOL_METHODS = (
        auto(),
        "Protocol adapter must implement handle, cancel and close",
    )
    VT_PROTOCOL_CLOSED = auto(), "Protocol registry is closed"
    VT_PROTOCOL_SYNC = auto(), "Protocol lifecycle callbacks must be synchronous"
    VT_PROTOCOL_RESULT_TYPE = auto(), "Protocol handler must return ProtocolResult"
    VT_PROTOCOL_RESULT = auto(), "Invalid protocol result"
    VT_PROTOCOL_FAILURE = (
        auto(),
        "Protocol adapter failed: {name} ({count} errors; payload omitted)",
    )
    VT_PROTOCOL_REPLY = auto(), "Protocol replies require a session writer"
    VT_PROTOCOL_OSC = auto(), "External protocols require numeric OSC namespaces"
    VT_PROTOCOL_NUMBER = auto(), "Expected an exact numeric OSC prefix such as b'7400;'"
    VT_PROTOCOL_NATIVE = (
        auto(),
        "Native or invalid OSC namespaces cannot be intercepted",
    )
    VT_PLUGIN_MOUSE = auto(), "mouse must be bool"
    VT_INPUT_CLOSED = auto(), "Terminal input is closed"
    VT_CLIPBOARD_LATE = auto(), "Clipboard must be attached before terminal input"
    VT_DIMENSIONS = auto(), "Terminal dimensions exceed {limit} cells: {cols}x{rows}"
    VT_BRIDGE_ERROR = auto(), "libghostty-vt bridge failed (status {status})"
    VT_ALLOCATION_ERROR = auto(), "Cannot allocate libghostty-vt terminal"
    VT_CLOSED = auto(), "Terminal is closed"
    VT_NATIVE_BUFFER = auto(), "Invalid native buffer"
    VT_CELL_DIMENSIONS = (
        auto(),
        "Cell pixel dimensions must both be zero or positive 16-bit values",
    )
    VT_NATIVE_ROW = auto(), "Invalid native row size"
    VT_NATIVE_CELL = auto(), "Truncated native cell"
    VT_NATIVE_IMAGES = auto(), "Truncated native image list"
    VT_NATIVE_FRAME = auto(), "Unexpected native frame data"
    VT_COLOR_SCHEME = auto(), "Invalid terminal color scheme"
    VT_PALETTE_SIZE = auto(), "Appearance palette must contain 256 entries"
    VT_RGB_COLOR = auto(), "Appearance colors must be RGB byte triples"
    VT_IMAGE_LIMIT = auto(), "Image storage limit exceeds the bounded native buffer"
    VT_IMAGE_GEOMETRY = auto(), "Graphics require confirmed cell pixel dimensions"
    VT_IMAGE_DECODER = auto(), "PNG images require Pillow"
    VT_IMAGE_ID = auto(), "Image ID exceeds unsigned 32-bit range"
    VT_NATIVE_IMAGE = auto(), "Invalid native image payload"
    VT_ANIMATION_TIME = auto(), "Animation timestamp exceeds unsigned 64-bit range"
    VT_NATIVE_EFFECT = auto(), "Invalid native effect"
    VT_HISTORY_LIMIT = (
        auto(),
        "Scrollback limits must be nonnegative signed 64-bit values",
    )
    VT_SCROLL_DELTA = auto(), "Scroll delta exceeds signed 64-bit range"
    VT_MOUSE_EVENT = auto(), "Unknown mouse action or button"
    VT_MOUSE_MODIFIERS = auto(), "Unsupported mouse modifiers"
    VT_MOUSE_COORDINATES = auto(), "Mouse coordinates exceed signed 32-bit range"
    VT_MOUSE_GEOMETRY = auto(), "Pixel mouse requires confirmed cell geometry"
    VT_MOUSE_PIXELS = auto(), "Mouse pixels exceed signed 32-bit range"
    VT_SELECTION_POINT = auto(), "Selection point is outside the viewport"
    VT_SELECTION_ACTION = auto(), "Unknown selection action"
    VT_SELECTION_COORDINATES = (
        auto(),
        "Selection coordinates exceed signed 32-bit range",
    )
    VT_SELECTION_POINTER = auto(), "Invalid selection pointer position"
    VT_SELECTION_TIME = auto(), "Invalid selection event time"
    VT_REPEAT_CLICK = auto(), "Invalid repeat-click policy"
    VT_KEY_ACTION = auto(), "Unknown key action"
    VT_KEY_MODIFIERS = auto(), "Unsupported key modifiers"
    VT_INPUT_LIMIT = auto(), "Input is too large"
    VT_KEY_COUNT = auto(), "Modifiers and key actions require one key"
    VT_PASTE_LIMIT = auto(), "Paste is too large"
    VT_DIAGNOSE_CONTROLS = auto(), "Unsupported control policy"
    VT_CONTROL_POLICY = (
        auto(),
        "No general pass-through; native parsing and registered adapters only. Not every unsupported control is reported.",
    )
    VT_DIAGNOSE_HOST = auto(), "Host capability policy"
    VT_HOST_POLICY = (
        auto(),
        "Feature policy, observed support and active behavior are separate. Auto uses implemented queries only; on requests use without proving support. Clipboard access also requires permission and a selected provider or usable host protocol; --clipboard does not enable host support.",
    )
    VT_GRAPHICS_STATUS = auto(), "Image output: {reason}"
    VT_GRAPHICS_AVAILABLE = (
        auto(),
        "using the selected output protocol with measured cell dimensions",
    )
    VT_GRAPHICS_DISABLED = auto(), "image output protocols are disabled by policy"
    VT_GRAPHICS_UNAVAILABLE = (
        auto(),
        "no supported host image output protocol was confirmed",
    )
    VT_GRAPHICS_GEOMETRY = (
        auto(),
        "cell pixel dimensions are unavailable; image output is disabled",
    )
    VT_GRAPHICS_PROBE = (
        auto(),
        "host negotiation did not finish; image output is disabled",
    )
    VT_IMAGE_FAILURES = auto(), "Image input rejected: {count} errors (payload omitted)"
    VT_IMAGE_OUTPUT_ERROR = (
        auto(),
        "Image output failed: {count} errors (host response and payload omitted)",
    )
    VT_HOST_PROBE_ONCE = (
        auto(),
        "Host capabilities may be probed only once per connection",
    )
    VT_HOST_DIMENSIONS = auto(), "Invalid host dimensions"
    VT_HOST_PIXELS = auto(), "Invalid host pixel dimensions"
    VT_GRAPHICS_QUERY_ID = auto(), "Invalid graphics query identity"
    VT_CURSOR_RGB = auto(), "Invalid cursor RGB color"
    VT_HOST_INPUT = auto(), "HostInput requires an unwrapped PTK VT input"
    VT_EFFECT_LIMIT = auto(), "Pending terminal effects exceed the byte limit"


_DEFAULT_MESSAGES = MappingProxyType({item.value: item.english for item in Message})


class _TranslationError(ValueError):
    """Retain a validation message until the selected language is available."""

    def __init__(self, message: Message, **values):
        """Keep the ordinary English exception useful outside a language manager."""
        self.message, self.values = message, values
        super().__init__(message.english.format(**values))


class _Messages(dict):
    """Validate message mutations once, retaining ordinary dictionary reads."""

    FORMATTER = Formatter()
    FIELDS = MappingProxyType(
        {
            key: frozenset(
                field
                for _, field, _, _ in Formatter().parse(value)
                if field is not None
            )
            for key, value in _DEFAULT_MESSAGES.items()
        }
    )

    __slots__ = ("translate",)

    def __init__(self, *args, translate=None, **kwargs):
        """Copy validated values so caller-held dictionaries cannot bypass validation."""
        super().__init__()
        self.translate = translate
        self.update(*args, **kwargs)

    @classmethod
    def _validate_entry(cls, key, value):
        """Keep the same string-to-string contract for every mutation path."""
        if not isinstance(key, str) or not isinstance(value, str):
            raise _TranslationError(Message.I18N_MESSAGES_ERROR)
        if key in cls.FIELDS:
            try:
                fields = {
                    field
                    for _, field, _, _ in cls.FORMATTER.parse(value)
                    if field is not None
                }
            except ValueError as error:
                raise _TranslationError(
                    Message.I18N_FORMAT_ERROR, name=key, error=error
                ) from error
            if fields != cls.FIELDS[key]:
                raise _TranslationError(Message.I18N_FIELDS_ERROR, name=key)

    def _validate(self, key, value):
        """Localize active mapping errors while leaving staged load errors deferred."""
        try:
            self._validate_entry(key, value)
        except _TranslationError as error:
            if self.translate is not None:
                raise ValueError(
                    self.translate(error.message, **error.values)
                ) from error
            raise

    def __setitem__(self, key, value):
        """Reject malformed values before changing a single translation."""
        self._validate(key, value)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        """Validate an entire update before publishing any of its entries."""
        incoming = dict(*args, **kwargs)
        for key, value in incoming.items():
            self._validate(key, value)
        super().update(incoming)

    def setdefault(self, key, default=None):
        """Validate defaults only when a new entry would be inserted."""
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other):
        """Apply the same atomic validation to augmented mapping updates."""
        self.update(other)
        return self


class I18N:
    """Thread-safe message manager combining built-in defaults and language files."""

    DEFAULT_LANG = "en"
    DEFAULT_ENCODING = "utf-8"
    JSON_SUFFIX = ".json"
    LOCALE_SEPARATOR = "_"
    LOCALE_NAME = re.compile(
        r"(?P<language>[A-Za-z]{2,3})(?:[_-][A-Za-z0-9]+)*"
        r"(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)?(?:@[A-Za-z0-9][A-Za-z0-9_-]*)?\Z"
    )
    LANG_ENV = "LANG"
    BUNDLED_DIRECTORY = Path(__file__).with_name("locales")

    def __init__(self, encoder: Optional[str] = None):
        """Initialize the encoding, selected language, default messages, and update lock."""
        self.encoder = encoder or self.DEFAULT_ENCODING
        self.default_lang: str = self.DEFAULT_LANG
        self.current_lang: str = self._detect()
        self.base_path: Path = config.LANG_DIR

        self._lock = threading.RLock()
        self._messages = _Messages(_DEFAULT_MESSAGES, translate=self.get)

    @property
    def message(self) -> Dict[str, str]:
        """Return the currently active message mapping."""
        return self._messages

    @message.setter
    def message(self, value: Dict[str, str]) -> None:
        """Replace the active message mapping while holding the lock."""
        with self._lock:
            self._messages = _Messages(value, translate=self.get)

    def _language_path(self, lang: str) -> Path:
        """Map a language name to its JSON file under the current base directory."""
        if not valid_language(lang):
            raise ValueError(self.get(Message.I18N_LANGUAGE_ERROR))
        return self.base_path / (lang + self.JSON_SUFFIX)

    def _detect(self) -> str:
        """Read LANG only, independently of installed locales and LC_* overrides."""
        match = self.LOCALE_NAME.fullmatch(os.environ.get(self.LANG_ENV, ""))
        return match.group("language").lower() if match else self.default_lang

    def load(self, lang: Optional[str] = None, *, logger=None):
        """Merge English defaults, user fallback, shipped locale and user locale.

        Log invalid JSON and read errors, and discard stale translations from the
        previous language. Reading translations never creates or changes files.
        """
        logger = logger or logging.getLogger(DEFAULT_LOGGER_NAME)
        selected = self._detect() if lang is None else lang
        # Start fresh on each switch: old translations must not leak into a new locale.
        messages = _Messages(_DEFAULT_MESSAGES)
        selected_path = self._language_path(selected)
        default_path = self._language_path(self.default_lang)
        diagnostics = []
        # User English remains the fallback for untranslated keys. A shipped
        # locale then supplies its translations, and the selected user file wins.
        language = (
            selected.replace("-", self.LOCALE_SEPARATOR)
            .partition(self.LOCALE_SEPARATOR)[0]
            .lower()
        )
        paths = [default_path, self.BUNDLED_DIRECTORY / (language + self.JSON_SUFFIX)]
        if selected_path != default_path:
            paths.append(selected_path)
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding=self.encoder) as f:
                    translated = json.load(f)
                if not isinstance(translated, dict):
                    raise _TranslationError(Message.I18N_MESSAGES_ERROR)
                messages.update(translated)
            except (OSError, ValueError) as exc:
                diagnostics.append((Message.I18N_LOAD_ERROR, path, exc))
        with self._lock:
            messages.translate = self.get
            self._messages = messages
            self.current_lang = selected
            # Report in this load's language, even if an earlier fallback file
            # failed before the shipped locale could be read.
            for message, path, error in diagnostics:
                detail = (
                    self.get(error.message, **error.values)
                    if isinstance(error, _TranslationError)
                    else str(error)
                )
                logger.warning(self.get(message, path=path, error=detail))

    def update(self, messages: Dict[str, str]) -> None:
        """Add or override individual messages while holding the lock."""
        with self._lock:
            self._messages |= messages

    def get(self, key: str, **kwargs: Any) -> str:
        """Format a message, falling back to English for a broken known template."""
        msg = self._messages.get(key, key)
        try:
            return msg.format(**kwargs)
        except Exception:
            fallback = _DEFAULT_MESSAGES.get(key)
            if fallback is not None:
                try:
                    return fallback.format(**kwargs)
                except Exception:
                    pass
            return msg
