"""Merge built-in messages with JSON translations and synchronize language changes."""

from __future__ import annotations

import json
import locale
import logging
import textwrap
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional

from ish.config import config

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["I18N"]


class I18N:
    """Thread-safe message manager combining built-in defaults and language files."""

    def __init__(self, encoder: Optional[str] = None):
        """Initialize the encoding, selected language, default messages, and update lock."""
        self.encoder = encoder or "utf-8"
        self.default_lang: str = "en"
        self.current_lang: str = self._detect_language()
        self.base_path: Path = config.LANG_DIR

        self._lock = threading.RLock()
        self._messages: Dict[str, str] = {
            "error": "An error has been occurred: {error}",
            "file_not_found": '"{path}" not found.',
            "cli_description": textwrap.dedent("""\
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
            "cli_shell_support": textwrap.dedent("""\
                Supported shell versions:
                  Bash 5.3.9 or newer
                  zsh 5.5.1 or newer
                  tcsh 6.21.00 or newer
                  BSD csh and dash/sh: see README for support limits.

                Shell versions are not checked automatically. Select a supported version;
                older versions may fail to initialize or process commands incorrectly.
                """),
            "cli_arguments_title": "positional arguments",
            "cli_options_title": "options",
            "cli_help_option_help": "Show this help message and exit.",
            "cli_shell_option_help": "Specify the shell to use. The login shell is selected by default.",
            "cli_lang_option_help": "Set the interface language. Defaults to the system language, with English fallback.",
            "cli_version_option_help": "Show the ish version and exit.",
            "cli_version": "ish {version}",
            "cli_no_rc_option_help": "Skip loading ish's .ishrc.py startup file. Shell startup files are still read.",
            "cli_no_plugins_option_help": "Skip automatic plugin loading and dependency installation.",
            "cli_home_option_help": "Use DIR for ish settings, plugins, logs, and cache (default: ~/ish). Does not change HOME.",
            "cli_home_error": "Cannot use ish home {path!r}: {error}",
            "cli_diagnose_option_help": "Print environment diagnostics and exit without loading user code or starting a shell.",
            "cli_diagnose_title": "ish environment diagnostics",
            "cli_diagnose_scope": "Read-only snapshot before user rc. No shell, helper, compiler, or plugin was executed; this is not an interactive health check.",
            "cli_diagnose_version": "ish version",
            "cli_diagnose_runtime": "Runtime",
            "cli_diagnose_source": "Python source",
            "cli_diagnose_bundled": "Bundled CPython",
            "cli_diagnose_python": "Python version",
            "cli_diagnose_executable": "Executable",
            "cli_diagnose_platform": "Platform",
            "cli_diagnose_libc": "Host libc",
            "cli_diagnose_shell": "Requested shell",
            "cli_diagnose_shell_path": "Shell path",
            "cli_diagnose_shell_realpath": "Resolved shell path",
            "cli_diagnose_adapter": "Shell adapter",
            "cli_diagnose_unsupported": "Unsupported shell",
            "cli_diagnose_missing": "Not found",
            "cli_diagnose_unavailable": "Unavailable",
            "cli_diagnose_home": "ish home",
            "cli_diagnose_rc": "Startup file",
            "cli_diagnose_rc_policy": "Startup file loading",
            "cli_diagnose_plugins": "Plugin directory",
            "cli_diagnose_plugin_policy": "Automatic plugin loading",
            "cli_diagnose_enabled": "Enabled for interactive startup",
            "cli_diagnose_disabled": "Disabled",
            "cli_diagnose_language": "Selected language",
            "cli_diagnose_language_dir": "Language directory",
            "cli_diagnose_cache": "Session cache",
            "cli_diagnose_cache_access": "Cache directory access",
            "cli_diagnose_cache_writable": "Write/search access to {path}; creation and execution were not tested",
            "cli_diagnose_cache_denied": "No directory write/search access to {path}",
            "cli_diagnose_cache_noexec": "Cache filesystem noexec",
            "cli_diagnose_yes": "Yes",
            "cli_diagnose_no": "No",
            "cli_diagnose_helper": "State helper",
            "cli_diagnose_helper_source": "Built at session startup using GCC: {compiler}",
            "cli_diagnose_helper_bundled": "Bundled file present: {path}; execution was not tested",
            "cli_diagnose_helper_missing": "Bundled file missing: {path}",
            "cli_diagnose_terminal": "TERM",
            "cli_diagnose_stdin_tty": "Standard input is a TTY",
            "cli_diagnose_stdout_tty": "Standard output is a TTY",
            "cli_diagnose_terminal_size": "Terminal size",
            "cli_diagnose_terminal_dimensions": "{columns} columns x {rows} rows",
            "prompt_os_error": "This program is designed to run only on 'Linux' operating systmes.",
            "prompt_shell_init_error": "An error occurred during shell initialization:\r\n{error}",
            "prompt_shell_path_not_found": "Shell path not found: {path}",
            "prompt_rc_not_found": "{path} not found. Running default shell.",
            "prompt_binary_build_done": "Binary build completed successfully. (Path: {path})",
            "pm_load_error": "An Error has been occurred while resolving plugin {plugin_file}:\r\n{error}",
            "pm_load_fail": "Failed to load plugin '{plugin_file}'",
            "pm_python_not_found": "Failed to find python. Cannot load",
            "pm_python_version_mismatch": "{value} != {req}",
            "pip_install_fail": "Failed to install library '{library}'",
        }
        self._defaults = self._messages.copy()

    @property
    def message(self) -> Dict[str, str]:
        """Return the currently active message mapping."""
        return self._messages

    @message.setter
    def message(self, value: Dict[str, str]) -> None:
        """Replace the active message mapping while holding the lock."""
        with self._lock:
            self._messages = value

    def _get_lang_file(self, lang: Optional[str], *, create: bool = True) -> Path:
        """Find the selected or default language file, optionally saving missing defaults."""
        default_file = self.base_path / f"{self.default_lang}.json"

        lang = lang or self.current_lang
        lang_file = self.base_path / f"{lang}.json"
        if lang_file.exists():
            return lang_file

        if default_file.exists():
            return default_file

        if not create:
            return default_file

        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            with default_file.open("w", encoding=self.encoder) as f:
                json.dump(self._messages, f, ensure_ascii=False, indent=4)
        except OSError as exc:
            logging.getLogger("global").warning(
                "Cannot create language file %s: %s", default_file, exc
            )

        return default_file

    def _detect_language(self) -> str:
        """Read the system locale's language code, falling back to the default on failure."""
        try:
            lang, _ = locale.getdefaultlocale()
            if lang:
                return lang.split("_")[0]
        except (ValueError, TypeError):
            pass
        return self.default_lang

    def load_messages(self, lang: Optional[str] = None, *, create: bool = True):
        """Build messages from built-in values, the default language, and the selected
        language.

        Log invalid JSON and read errors, and discard stale translations from the
        previous language. Set create=False for CLI queries that must not write files.
        """
        selected = lang or self.current_lang
        # Start fresh on each switch: old translations must not leak into a new locale.
        messages = self._defaults.copy()
        selected_path = self._get_lang_file(selected, create=create)
        paths = dict.fromkeys(
            [self.base_path / f"{self.default_lang}.json", selected_path]
        )
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding=self.encoder) as f:
                    translated = json.load(f)
                if not isinstance(translated, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in translated.items()
                ):
                    raise ValueError("Language messages must map strings to strings")
                messages.update(translated)
            except (OSError, ValueError) as exc:
                logging.getLogger("global").warning(
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
