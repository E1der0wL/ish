"""Merge built-in messages with JSON translations and synchronize language changes."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import tempfile
import textwrap
import threading
from enum import StrEnum, auto
from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Any, Dict, Optional

from ish.config import config
from ish.log import DEFAULT_LOGGER_NAME

__all__ = ["I18N", "Message"]


_DEFAULT_LANG = "en"
_DEFAULT_ENCODING = "utf-8"
_JSON_SUFFIX = ".json"
_JSON_INDENT = 4
_LANG_STAGE_PREFIX = ".ish-lang-"
_UNSUPPORTED_HARD_LINK = frozenset({errno.ENOTSUP, errno.EPERM, errno.ENOSYS})
_FALLBACK_COPY_BYTES = 64 * 1024
_LOCALE_SEPARATOR = "_"
_LANGUAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_LOCALE_NAME = re.compile(
    r"(?P<language>[A-Za-z]{2,3})(?:[_-][A-Za-z0-9]+)*"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)?(?:@[A-Za-z0-9][A-Za-z0-9_-]*)?\Z"
)
_LANG_ENV = "LANG"
_BUNDLED_DIRECTORY = Path(__file__).with_name("locales")
_FORMATTER = Formatter()


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

    ERROR = auto(), "An error has been occurred: {error}"
    FILE_NOT_FOUND = auto(), '"{path}" not found.'
    CLI_DESCRIPTION = (
        auto(),
        textwrap.dedent("""\
        ish - An interactive command editor for Linux shells.

        Run your shell with multiline editing, syntax highlighting,
        completion, and Python extensions.

        Usage:
          ish [options] [shell]

        Supported shells:
          Bash, zsh, dash/sh, BSD csh, and tcsh.
          The login shell is selected by default.

        Examples:
          ish
          ish bash
          ish /bin/tcsh

        On Windows, run ish inside WSL.

        """),
    )
    CLI_SHELL_SUPPORT = (
        auto(),
        textwrap.dedent("""\
        Supported shell versions:
          Bash 5.3.9 or newer
          zsh 5.5.1 or newer
          tcsh 6.21.00 or newer
            Requires a dynamically linked Linux tcsh with preload support.
          BSD csh and dash/sh: see README for support limits.

        Shell versions are not checked automatically. Select a supported version;
        older versions may fail to initialize or process commands incorrectly.

        Continuation prompts are fixed to Bash '> ', zsh '%_> ', and tcsh '%R? '.
        Custom PS2 / prompt2 text and command substitutions are replaced at each
        primary prompt, including after ish_recover. Primary prompts are customizable.
        """),
    )
    CLI_ZSH_MODULE_HELP = (
        auto(),
        textwrap.dedent("""\
        zsh continuation prompt setup:
          ish enables PROMPT_SUBST at startup and on ish_recover.
          Hiding queued continuation prompts also needs zsh/zselect.
          zselect is a zsh module, not a separate program or Python package.

          Load/check inside the zsh selected for ish:
            builtin zmodload -F zsh/zselect b:zselect

          Install the distribution's zsh and its matching modules:
            RHEL 8 / AlmaLinux 8: sudo dnf install zsh
            Debian / Ubuntu: sudo apt-get install zsh zsh-common
          To repair missing packaged files, use dnf reinstall zsh or
          apt-get install --reinstall zsh zsh-common with sudo.

          Offline: transfer packages and dependencies from a matching OS
          release and architecture, or build zsh from an official source
          archive with its modules. See README, "Installing zsh/zselect",
          for offline RPM commands and installation under your home.
          Custom zsh builds need their own matching modules.
          ish does not install packages or enforce module availability.
        """),
    )
    CLI_ARGUMENTS_TITLE = auto(), "positional arguments"
    CLI_OPTIONS_TITLE = auto(), "options"
    CLI_HELP_OPTION_HELP = auto(), "Show this help message and exit."
    CLI_SHELL_OPTION_HELP = (
        auto(),
        "Specify the shell to use. The login shell is selected by default.",
    )
    CLI_LANG_OPTION_HELP = (
        auto(),
        "Set the interface language. Otherwise use LANG, with English fallback.",
    )
    CLI_VERSION_OPTION_HELP = auto(), "Show the ish version and exit."
    CLI_VERSION = auto(), "ish {version}"
    CLI_NO_RC_OPTION_HELP = (
        auto(),
        "Skip loading ish's .ishrc.py startup file. Shell startup files are still read.",
    )
    CLI_NO_PLUGINS_OPTION_HELP = (
        auto(),
        "Skip automatic plugin loading and dependency installation.",
    )
    CLI_HOME_OPTION_HELP = (
        auto(),
        "Use DIR for ish settings, plugins, logs, and cache (default: ~/ish). Does not change HOME.",
    )
    CLI_HOME_ERROR = auto(), "Cannot use ish home {path!r}: {error}"
    CLI_DIAGNOSE_OPTION_HELP = (
        auto(),
        "Print environment diagnostics and exit without loading user code or starting a shell.",
    )
    CLI_DIAGNOSE_TITLE = auto(), "ish environment diagnostics"
    CLI_DIAGNOSE_SCOPE = (
        auto(),
        "Read-only snapshot before user rc. No shell, helper, compiler, or plugin was executed; this is not an interactive health check.",
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
    CLI_DIAGNOSE_ADAPTER = auto(), "Shell adapter"
    CLI_DIAGNOSE_UNSUPPORTED = auto(), "Unsupported shell"
    CLI_DIAGNOSE_MISSING = auto(), "Not found"
    CLI_DIAGNOSE_UNAVAILABLE = auto(), "Unavailable"
    CLI_DIAGNOSE_HOME = auto(), "ish home"
    CLI_DIAGNOSE_RC = auto(), "Startup file"
    CLI_DIAGNOSE_RC_POLICY = auto(), "Startup file loading"
    CLI_DIAGNOSE_PLUGINS = auto(), "Plugin directory"
    CLI_DIAGNOSE_PLUGIN_POLICY = auto(), "Automatic plugin loading"
    CLI_DIAGNOSE_ENABLED = auto(), "Enabled for interactive startup"
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
    CLI_DIAGNOSE_HELPER = auto(), "State helper"
    CLI_DIAGNOSE_HELPER_SOURCE = (
        auto(),
        "Built at session startup using GCC: {compiler}",
    )
    CLI_DIAGNOSE_HELPER_BUNDLED = (
        auto(),
        "Bundled file present: {path}; execution was not tested",
    )
    CLI_DIAGNOSE_HELPER_MISSING = auto(), "Bundled file missing: {path}"
    CLI_DIAGNOSE_TERMINAL = auto(), "TERM"
    CLI_DIAGNOSE_STDIN_TTY = auto(), "Standard input is a TTY"
    CLI_DIAGNOSE_STDOUT_TTY = auto(), "Standard output is a TTY"
    CLI_DIAGNOSE_TERMINAL_SIZE = auto(), "Terminal size"
    CLI_DIAGNOSE_TERMINAL_DIMENSIONS = auto(), "{columns} columns x {rows} rows"
    PROMPT_OS_ERROR = (
        auto(),
        "This program is designed to run only on 'Linux' operating systmes.",
    )
    PROMPT_SHELL_INIT_ERROR = (
        auto(),
        "An error occurred during shell initialization:\r\n{error}",
    )
    PROMPT_SHELL_PATH_NOT_FOUND = auto(), "Shell path not found: {path}"
    PROMPT_RC_NOT_FOUND = auto(), "{path} not found. Running default shell."
    PROMPT_BINARY_BUILD_DONE = (
        auto(),
        "Binary build completed successfully. (Path: {path})",
    )
    PM_LOAD_ERROR = (
        auto(),
        "An Error has been occurred while resolving plugin {plugin_file}:\r\n{error}",
    )
    PM_LOAD_FAIL = auto(), "Failed to load plugin '{plugin_file}'"
    PM_PYTHON_NOT_FOUND = auto(), "Failed to find python. Cannot load"
    PM_PYTHON_VERSION_MISMATCH = auto(), "{value} != {req}"
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
    I18N_CREATE_ERROR = auto(), "Cannot create language file {path}: {error}"
    I18N_LOAD_ERROR = auto(), "Cannot load language file {path}: {error}"

    # Shared logging and native transport diagnostics.
    LOG_DESTINATION = auto(), "Log destination must be a regular file"
    LOG_OUTPUT_ERROR = auto(), "Log output failed: {error}"
    LOG_LEVEL_ERROR = auto(), "Unknown log level: {value}"
    INPUT_PREFIX_ERROR = auto(), "Native prefix is outside the input payload"
    WRITER_CLOSED = auto(), "Writer is closed"
    WRITE_LIMIT = auto(), "Pending terminal writes exceed the byte limit"
    WRITE_EMPTY = auto(), "write returned zero bytes"
    SESSION_STARTED = auto(), "The terminal session has already started"
    INPUT_LIMIT = auto(), "Pending startup input exceeds the byte limit"

    # Native-stream startup and UI messages.
    STREAM_SETTING_LANG = auto(), "lang must be a nonempty language code or None"
    STREAM_SETTING_SHELL = auto(), "shell must be a nonempty executable name or path"
    STREAM_SETTING_LIST = auto(), "{name} must be a list of strings"
    STREAM_SETTING_PLUGINS = auto(), "plugins must contain nonempty names"
    STREAM_SETTING_PATH = auto(), "{name} must be a path"
    STREAM_SETTING_LEVEL = auto(), "log_level must be one of {levels}"
    STREAM_SETTING_ESCAPE = (
        auto(),
        "escape_delay must be between {minimum} and {maximum} seconds",
    )
    STREAM_RC_FILE_ERROR = auto(), "configuration must be a regular Python file"
    STREAM_RC_ERROR = auto(), "Cannot load configuration {path}: {error}"
    STREAM_ARG_ERROR = auto(), "argument {argument}: {error}"
    STREAM_ARG_UNKNOWN = auto(), "unrecognized arguments: {arguments}"
    STREAM_ARG_EXPECTED = auto(), "expected one argument"
    STREAM_ARG_CONFLICT = auto(), "not allowed with argument {argument}"
    STREAM_ARG_CHOICE = auto(), "invalid choice: {value} (choose from {choices})"
    STREAM_ARG_VALUE = auto(), "invalid {kind} value: {value}"
    STREAM_ARG_EXPLICIT = auto(), "ignored explicit argument {value}"
    STREAM_META_DIR = auto(), "DIR"
    STREAM_META_SECONDS = auto(), "SECONDS"
    STREAM_META_SHELL = auto(), "SHELL"
    STREAM_META_NAME = auto(), "NAME"
    STREAM_META_PATH = auto(), "PATH"
    STREAM_META_ARG = auto(), "ARG"
    STREAM_META_LANG = auto(), "LANG"
    STREAM_DESCRIPTION = (
        auto(),
        "ish - Run a native shell with direct terminal output and optional prompt-time Python plugins.",
    )
    STREAM_VERSION = auto(), "ish {version}"
    STREAM_NO_RC_HELP = auto(), "Skip ish's .ishrc.py for this session."
    STREAM_NO_PLUGINS_HELP = auto(), "Disable all selected plugins for this session."
    STREAM_ESCAPE_DELAY_HELP = (
        auto(),
        "Wait SECONDS to distinguish ESC from Alt sequences (default: 0.05).",
    )
    STREAM_RC_NOT_EXECUTED = auto(), "Not executed (read-only query)"
    STREAM_DIAGNOSE_ESCAPE_DELAY = auto(), "Escape sequence delay (seconds)"
    STREAM_USAGE = auto(), "usage: "
    STREAM_PARSE_ERROR = auto(), "error: {error}"
    STREAM_SHELL_HELP = (
        auto(),
        "Run SHELL; the account's login shell is selected by default.",
    )
    STREAM_CWD_HELP = auto(), "Start the shell in DIR (default: current directory)."
    STREAM_PLUGIN_HELP = (
        auto(),
        "Select Python plugins instead of the rc list; repeat to select more.",
    )
    STREAM_PLUGIN_DIR_HELP = (
        auto(),
        "Read selected Python plugins from DIR instead of the bundled directory.",
    )
    STREAM_PLUGIN_LIB_HELP = auto(), "Use DIR for plugin Python dependencies."
    STREAM_INTEGRATION_HELP = (
        auto(),
        "Read the selected shell integration's text resources from DIR.",
    )
    STREAM_LOGIN_HELP = (
        auto(),
        "Start a login shell; unsupported plugin startup modes fall back to native operation.",
    )
    STREAM_SHELL_ARG_HELP = (
        auto(),
        "Replace the default -i shell arguments; repeat as --shell-arg=ARG.",
    )
    STREAM_LOG_FILE_HELP = (
        auto(),
        "Append logs to PATH; without PATH use ish's configured log file.",
    )
    STREAM_LOG_LEVEL_HELP = auto(), "Set the log threshold (default: WARNING)."
    STREAM_PLATFORM_ERROR = (
        auto(),
        "ish requires Linux/WSL with a controlling PTY.",
    )
    STREAM_TTY_ERROR = auto(), "Standard input and output must be terminals."
    STREAM_SHELL_ERROR = auto(), "Shell executable not found: {shell}"
    STREAM_CWD_ERROR = auto(), "Working directory not found: {path}"
    STREAM_FALLBACK = auto(), "{error}; starting without UI"
    STREAM_UI_ERROR = auto(), "Plugin UI disabled: {error}"
    STREAM_CLEANUP_ERROR = auto(), "Plugin cleanup failed: {error}"
    STREAM_SESSION_ERROR = auto(), "Session error: {error}"
    STREAM_DIAGNOSE_TITLE = auto(), "ish environment diagnostics"
    STREAM_DIAGNOSE_SCOPE = (
        auto(),
        "Read-only CLI/default settings; .ishrc.py was not executed. No shell or plugin was executed; selected plugins do not certify runtime support or readiness.",
    )
    STREAM_DIAGNOSE_CWD = auto(), "Shell working directory"
    STREAM_DIAGNOSE_ARGS = auto(), "Shell arguments"
    STREAM_DIAGNOSE_SELECTED_PLUGINS = auto(), "Selected plugins"
    STREAM_DIAGNOSE_PLUGIN_LIB = auto(), "Plugin library directory"
    STREAM_DIAGNOSE_INTEGRATIONS = auto(), "Shell integration directory"
    STREAM_DIAGNOSE_LOG_FILE = auto(), "Log file"
    STREAM_DIAGNOSE_LOG_LEVEL = auto(), "Log threshold"

    # Optional plugin registration, rendering and shell integration.
    STREAM_PLUGIN_ONCE = auto(), "Stream plugins can only be loaded once"
    STREAM_PLUGIN_MISSING = auto(), "Cannot load stream plugin: {name}"
    STREAM_PLUGIN_API = auto(), "Unsupported stream plugin API: {name}"
    STREAM_PLUGIN_SETUP = auto(), "Stream plugin has no setup function: {name}"
    STREAM_PLUGIN_CLEANUP = auto(), "Plugin setup must return a cleanup or None"
    STREAM_REGISTER_SETUP = auto(), "Stream registration is only available during setup"
    STREAM_BAR_EXISTS = auto(), "A stream bar is already registered"
    STREAM_BAR_REQUIRED = auto(), "A bar provider must not be None"
    STREAM_POPUP_EXISTS = auto(), "A stream popup is already registered"
    STREAM_POPUP_CALLABLE = auto(), "A popup factory must be callable"
    STREAM_SHELL_CALLBACKS = (
        auto(),
        "A shell registration needs matching and build functions",
    )
    STREAM_SHELL_MISSING = (
        auto(),
        "No prompt integration is installed for this shell in the selected plugins",
    )
    STREAM_SHELL_MULTIPLE = auto(), "Multiple selected plugins support this shell"
    STREAM_SCRIPT_READ = auto(), "Cannot read shell integration: {path}"
    STREAM_SCRIPT_CONTRACT = auto(), "Unsupported shell integration contract: {path}"
    STREAM_SCRIPT_TOKEN = auto(), "Unknown integration placeholder {token}: {path}"
    STREAM_NONCE_INVALID = auto(), "Invalid shell notification nonce"
    STREAM_HOME_ABSOLUTE = auto(), "Shell integration requires an absolute HOME"
    STREAM_PANEL_ROW = auto(), "Panel top must be a positive terminal row"
    STREAM_PANEL_ONCE = auto(), "Panel can only be started once"
    STREAM_PANEL_SIZE = auto(), "Panel dimensions must be positive"
    STREAM_PANEL_NOT_RUNNING = auto(), "Panel is not running"
    STREAM_POPUP_VIEW = auto(), "A popup factory must return PopupView"
    STREAM_BASH_STARTUP = (
        auto(),
        "Bash prompt decorations require a non-login interactive shell",
    )
    STREAM_ZSH_STARTUP = (
        auto(),
        "zsh prompt decorations require non-login interactive startup",
    )
    STREAM_TCSH_STARTUP = (
        auto(),
        "tcsh prompt decorations require non-login interactive startup",
    )
    STREAM_SHELL_PATH = (
        auto(),
        "Shell integration paths must not contain line breaks or NUL",
    )
    STREAM_BASH_VERSION = auto(), "ish prompt decorations require Bash 5.1 or later"

    # Native-stream diagnostics and optional sample UI.
    STREAM_CHILD_EXIT = auto(), "Child exit status={status}; draining output"
    STREAM_SHELL_EVENT = auto(), "Shell event kind={kind} serial={serial}"
    STREAM_POPUP_REQUEST = auto(), "Popup request stage={stage} serial={serial}"
    STREAM_POPUP_FOCUS = auto(), "Popup focus={focus}"
    STREAM_RESIZE = auto(), "Terminal resize columns={columns} rows={rows}"
    STREAM_SESSION_START = auto(), "Session start shell={shell} plugins={plugins}"
    STREAM_SESSION_EXIT = auto(), "Session exit status={status}"
    STREAM_SAMPLE_BAR = auto(), "ish stream | native shell | F2 popup"
    STREAM_SAMPLE_POPUP = auto(), "Plugin popup | Enter accepts | Esc cancels"

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
    VT_VERSION = auto(), "ish-vt {version} (separate VT experiment)"
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
    VT_SETTING_NAMESPACE = auto(), "config.vt must be a dictionary of VT settings"
    VT_SETTING_UNKNOWN = auto(), "Unknown config.vt setting: {name}"
    VT_FEATURES_MAPPING = (
        auto(),
        "config.vt.features must be a dictionary of feature names and on/off/auto policies",
    )
    VT_FEATURE_ASSIGNMENT = auto(), "Feature selection must use NAME=on|off|auto"
    VT_FEATURE_HELP = (
        auto(),
        "Set one host feature to on, off, or auto; repeat for multiple features. CLI overrides the matching config.vt.features entry. Auto uses implemented support queries only. clipboard_protocol selects OSC5522 host transport, not clipboard access permission.",
    )
    VT_DIAGNOSE_FEATURES = auto(), "Requested feature policies (host not probed)"
    VT_FEATURE_STATUS = (
        auto(),
        "{name}: policy={policy}, support={support}, active={active}, reason={reason}",
    )
    VT_DIAGNOSE_TITLE = auto(), "ish-vt environment diagnostics"
    VT_DIAGNOSE_SCOPE = (
        auto(),
        "Read-only configuration report: rc, plugins, shell and native library are not executed; host features are not probed. This is separate from ish's stream path.",
    )
    VT_DIAGNOSE_NAMESPACE = auto(), "VT settings namespace"
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
    VT_SESSION_ERROR = auto(), "ish-vt session failed: {error}"
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
_MESSAGE_FIELDS = {
    key: {field for _, field, _, _ in _FORMATTER.parse(value) if field is not None}
    for key, value in _DEFAULT_MESSAGES.items()
}


def _publish_missing(staged: Path, destination: Path) -> None:
    """Publish completed JSON without replacing a concurrently created language file."""
    try:
        destination.hardlink_to(staged)
        return
    except FileExistsError:
        return
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_HARD_LINK:
            raise

    # FAT-like mounts may allow ordinary files but forbid links. Exclusive
    # creation still preserves another writer's file; a reader can observe
    # this fallback copy in progress.
    with staged.open("rb") as source:
        try:
            target = destination.open("xb")
        except FileExistsError:
            return
        with target:
            shutil.copyfileobj(source, target, _FALLBACK_COPY_BYTES)


class _TranslationError(ValueError):
    """Retain a validation message until the selected language is available."""

    def __init__(self, message: Message, **values):
        """Keep the ordinary English exception useful outside a language manager."""
        self.message, self.values = message, values
        super().__init__(message.english.format(**values))


class _Messages(dict):
    """Validate message mutations once, retaining ordinary dictionary reads."""

    __slots__ = ("translate",)

    def __init__(self, *args, translate=None, **kwargs):
        """Copy validated values so caller-held dictionaries cannot bypass validation."""
        super().__init__()
        self.translate = translate
        self.update(*args, **kwargs)

    @staticmethod
    def _validate_entry(key, value):
        """Keep the same string-to-string contract for every mutation path."""
        if not isinstance(key, str) or not isinstance(value, str):
            raise _TranslationError(Message.I18N_MESSAGES_ERROR)
        if key in _MESSAGE_FIELDS:
            try:
                fields = {
                    field
                    for _, field, _, _ in _FORMATTER.parse(value)
                    if field is not None
                }
            except ValueError as error:
                raise _TranslationError(
                    Message.I18N_FORMAT_ERROR, name=key, error=error
                ) from error
            if fields != _MESSAGE_FIELDS[key]:
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

    def __init__(self, encoder: Optional[str] = None):
        """Initialize the encoding, selected language, default messages, and update lock."""
        self.encoder = encoder or _DEFAULT_ENCODING
        self.default_lang: str = _DEFAULT_LANG
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
        if not isinstance(lang, str) or not _LANGUAGE_NAME.fullmatch(lang):
            raise ValueError(self.get(Message.I18N_LANGUAGE_ERROR))
        return self.base_path / (lang + _JSON_SUFFIX)

    def _get_lang_file(self, lang: Optional[str], *, create: bool = True) -> Path:
        """Find the selected or default language file, optionally saving missing defaults."""
        default_file = self._language_path(self.default_lang)

        lang = lang or self.current_lang
        lang_file = self._language_path(lang)
        if lang_file.exists():
            return lang_file

        if default_file.exists():
            return default_file

        if not create:
            return default_file

        self.base_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=default_file.parent, prefix=_LANG_STAGE_PREFIX
        ) as temporary:
            staged = Path(temporary) / default_file.name
            with staged.open("w", encoding=self.encoder) as stream:
                json.dump(
                    dict(_DEFAULT_MESSAGES),
                    stream,
                    ensure_ascii=False,
                    indent=_JSON_INDENT,
                )
            _publish_missing(staged, default_file)

        return default_file

    def _detect(self) -> str:
        """Read LANG only, independently of installed locales and LC_* overrides."""
        match = _LOCALE_NAME.fullmatch(os.environ.get(_LANG_ENV, ""))
        return match.group("language").lower() if match else self.default_lang

    def load(self, lang: Optional[str] = None, *, create: bool = True, logger=None):
        """Merge English defaults, user fallback, shipped locale and user locale.

        Log invalid JSON and read errors, and discard stale translations from the
        previous language. Set create=False for CLI queries that must not write files.
        """
        logger = logger or logging.getLogger(DEFAULT_LOGGER_NAME)
        selected = self._detect() if lang is None else lang
        # Start fresh on each switch: old translations must not leak into a new locale.
        messages = _Messages(_DEFAULT_MESSAGES)
        selected_path = self._language_path(selected)
        default_path = self._language_path(self.default_lang)
        diagnostics = []
        try:
            self._get_lang_file(selected, create=create)
        except OSError as error:
            diagnostics.append((Message.I18N_CREATE_ERROR, default_path, error))
        # User English remains the fallback for untranslated keys. A shipped
        # locale then supplies its translations, and the selected user file wins.
        language = (
            selected.replace("-", _LOCALE_SEPARATOR)
            .partition(_LOCALE_SEPARATOR)[0]
            .lower()
        )
        paths = [default_path, _BUNDLED_DIRECTORY / (language + _JSON_SUFFIX)]
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
