"""Merge built-in messages with JSON translations and synchronize language changes."""

from __future__ import annotations

import json
import locale
import logging
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
            "cli_shell_option_help": "Specify the shell to use. The login shell is selected by default.",
            "cli_lang_option_help": "Set the interface language. Default is 'en'",
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

    def _get_lang_file(self, lang: Optional[str]) -> Path:
        """Find the selected or default language file and save defaults if neither exists."""
        default_file = self.base_path / f"{self.default_lang}.json"

        lang = lang or self.current_lang
        lang_file = self.base_path / f"{lang}.json"
        if lang_file.exists():
            return lang_file

        if default_file.exists():
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

    def load_messages(self, lang: Optional[str] = None):
        """Build messages from built-in values, the default language, and the selected
        language.

        Log invalid JSON and read errors, and discard stale translations from the
        previous language.
        """
        selected = lang or self.current_lang
        # Start fresh on each switch: old translations must not leak into a new locale.
        messages = self._defaults.copy()
        selected_path = self._get_lang_file(selected)
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
