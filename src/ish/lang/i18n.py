"""Merge built-in messages with JSON translations and synchronize language changes."""

from __future__ import annotations

import errno
import json
import locale
import logging
import shutil
import tempfile
import textwrap
import threading
from enum import StrEnum, auto
from pathlib import Path
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
_INVALID_MESSAGES = "Language messages must map strings to strings"


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
        "Set the interface language. Defaults to the system language, with English fallback.",
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
    PIP_INSTALL_FAIL = auto(), "Failed to install library '{library}'"


_DEFAULT_MESSAGES = MappingProxyType({item.value: item.english for item in Message})


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


class _Messages(dict):
    """Validate message mutations once, retaining ordinary dictionary reads."""

    __slots__ = ()

    def __init__(self, *args, **kwargs):
        """Copy validated values so caller-held dictionaries cannot bypass validation."""
        super().__init__()
        self.update(*args, **kwargs)

    @staticmethod
    def _validate(key, value):
        """Keep the same string-to-string contract for every mutation path."""
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(_INVALID_MESSAGES)

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
        self._messages = _Messages(_DEFAULT_MESSAGES)

    @property
    def message(self) -> Dict[str, str]:
        """Return the currently active message mapping."""
        return self._messages

    @message.setter
    def message(self, value: Dict[str, str]) -> None:
        """Replace the active message mapping while holding the lock."""
        with self._lock:
            self._messages = _Messages(value)

    def _language_path(self, lang: str) -> Path:
        """Map a language name to its JSON file under the current base directory."""
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

        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=default_file.parent, prefix=_LANG_STAGE_PREFIX
            ) as temporary:
                staged = Path(temporary) / default_file.name
                with staged.open("w", encoding=self.encoder) as stream:
                    json.dump(
                        self._messages, stream, ensure_ascii=False, indent=_JSON_INDENT
                    )
                _publish_missing(staged, default_file)
        except OSError as exc:
            logging.getLogger(DEFAULT_LOGGER_NAME).warning(
                "Cannot create language file %s: %s", default_file, exc
            )

        return default_file

    def _detect(self) -> str:
        """Read the system locale's language code, falling back to the default on failure."""
        try:
            lang, _ = locale.getdefaultlocale()
            if lang:
                return lang.partition(_LOCALE_SEPARATOR)[0]
        except (ValueError, TypeError):
            pass
        return self.default_lang

    def load(self, lang: Optional[str] = None, *, create: bool = True):
        """Build messages from built-in values, the default language, and the selected
        language.

        Log invalid JSON and read errors, and discard stale translations from the
        previous language. Set create=False for CLI queries that must not write files.
        """
        selected = lang or self.current_lang
        # Start fresh on each switch: old translations must not leak into a new locale.
        messages = _Messages(_DEFAULT_MESSAGES)
        selected_path = self._get_lang_file(selected, create=create)
        paths = dict.fromkeys([self._language_path(self.default_lang), selected_path])
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding=self.encoder) as f:
                    translated = json.load(f)
                if not isinstance(translated, dict):
                    raise ValueError(_INVALID_MESSAGES)
                messages.update(translated)
            except (OSError, ValueError) as exc:
                logging.getLogger(DEFAULT_LOGGER_NAME).warning(
                    "Cannot load language file %s: %s", path, exc
                )
        with self._lock:
            self._messages = messages
            self.current_lang = selected

    def update(self, messages: Dict[str, str]) -> None:
        """Add or override individual messages while holding the lock."""
        with self._lock:
            self._messages |= messages

    def get(self, key: str, **kwargs: Any) -> str:
        """Format a translation, returning the original text if a key or argument is
        missing.
        """
        msg = self._messages.get(key, key)
        try:
            return msg.format(**kwargs)
        except Exception:
            return msg
