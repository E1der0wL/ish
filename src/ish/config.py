"""Resolve settings, plugin, and log paths and prepare required directories."""

from __future__ import annotations

import os
import shlex
import sys
from enum import StrEnum
from pathlib import Path
from typing import Optional, Union

from ish.runtime.distribution import LAUNCHER_NAME, bundled_root

__all__ = ["config"]

_HOME_DIRECTORY = "ish"
_XDG_HOME_ENV = "XDG_DATA_HOME"
_XDG_DEFAULT = "~/.local/share"
_LANG_DIRECTORY = "lang"
_PLUGIN_DIRECTORY = "plugin"
_LIB_DIRECTORY = "lib"
_SCRIPT_DIRECTORY = "script"
_CACHE_DIRECTORY = ".cache"
_RC_FILENAME = ".ishrc.py"
_LOG_FILENAME = ".ish.log"
_SOURCE_ENTRY = ("-m", "ish.main")


class BuiltinShell(StrEnum):
    """Built-in identities shared by portable configuration and the Linux adapter registry."""

    BASH = "bash"
    ZSH = "zsh"
    TCSH = "tcsh"
    CSH = "csh"
    SH = "sh"


class Config:
    """Resolve configuration paths relative to the user's home directory.

    Reading properties does not create directories; ensure_directories handles creation.
    """

    SHELLS = {shell.value for shell in BuiltinShell}

    def __init__(self) -> None:
        """Initialize the home override to use the default user path."""
        self._ish_home: Optional[Path] = None

    @property
    def ISH_HOME(self) -> Path:
        """Return the overridden home or the default ~/ish path."""
        if self._ish_home is None:
            return Path.home() / _HOME_DIRECTORY
        return self._ish_home

    @ISH_HOME.setter
    def ISH_HOME(self, value: Union[str, Path]) -> None:
        """Change the base directory for subsequent user path lookups."""
        self._ish_home = Path(value)

    @property
    def LANG_DIR(self) -> Path:
        """Return the directory for language-specific JSON translations."""
        return self.ISH_HOME / _LANG_DIRECTORY

    @property
    def PLUGIN_DIR(self) -> Path:
        """Return the common parent directory for plugin data."""
        return self.ISH_HOME / _PLUGIN_DIRECTORY

    @property
    def PLUGIN_LIB_DIR(self) -> Path:
        """Return the separate installation directory for plugin Python dependencies."""
        return self.PLUGIN_DIR / _LIB_DIRECTORY

    @property
    def PLUGIN_SCRIPT_DIR(self) -> Path:
        """Return the directory containing plugin sources to load."""
        return self.PLUGIN_DIR / _SCRIPT_DIRECTORY

    @property
    def RC_FILE(self) -> Path:
        """Return the path to the user's Python initialization script."""
        return self.ISH_HOME / _RC_FILENAME

    @property
    def LOG_FILE(self) -> Path:
        """Return the log path used when file logging is enabled."""
        return self.ISH_HOME / _LOG_FILENAME

    @property
    def CACHE_DIR(self) -> Path:
        """Return the ish user cache directory."""
        return self.ISH_HOME / _CACHE_DIRECTORY

    @property
    def XDG_DATA_HOME(self) -> Path:
        """Return the ish directory under XDG_DATA_HOME or its standard default."""
        base = os.environ.get(_XDG_HOME_ENV)
        if base is None:
            base = os.path.expanduser(_XDG_DEFAULT)
        return Path(base) / _HOME_DIRECTORY

    @property
    def EXEC_CMD(self) -> str:
        """Quote a restart command using the launcher or the active interpreter."""
        root = bundled_root()
        command = (
            [str(root / LAUNCHER_NAME)] if root else [sys.executable, *_SOURCE_ENTRY]
        )
        return shlex.join([*command, *sys.argv[1:]])

    def ensure_directories(self) -> None:
        """Create settings, translation, plugin, and cache directories as needed."""
        directories = [
            self.ISH_HOME,
            self.LANG_DIR,
            self.PLUGIN_DIR,
            self.PLUGIN_LIB_DIR,
            self.PLUGIN_SCRIPT_DIR,
            self.CACHE_DIR,
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def ensure_rc_file(self) -> None:
        """Create an empty initialization script without modifying an existing file."""
        self.ISH_HOME.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive creation also preserves a file created by another session.
            self.RC_FILE.touch(exist_ok=False)
        except FileExistsError:
            pass

    def reset(self) -> None:
        """Clear the home override and restore default path resolution."""
        self._ish_home = None


config = Config()
