"""Resolve settings, plugin, and log paths without creating directories."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

__all__ = ["config"]


class Config:
    """Resolve configuration paths relative to the user's home directory.

    Reading properties does not create directories or execute configuration.
    """

    _HOME_DIRECTORY = "ish"
    _LANG_DIRECTORY = "lang"
    _PLUGIN_DIRECTORY = "plugin"
    _LIB_DIRECTORY = "lib"
    _SCRIPT_DIRECTORY = "script"
    _CACHE_DIRECTORY = ".cache"
    _RC_FILENAME = ".ishrc.py"
    _LOG_FILENAME = ".ish.log"

    def __init__(self) -> None:
        """Initialize the home override to use the default user path."""
        self._ish_home: Optional[Path] = None

    @property
    def ISH_HOME(self) -> Path:
        """Return the overridden home or the default ~/ish path."""
        if self._ish_home is None:
            return Path.home() / self._HOME_DIRECTORY
        return self._ish_home

    @ISH_HOME.setter
    def ISH_HOME(self, value: Union[str, Path]) -> None:
        """Change the base directory for subsequent user path lookups."""
        self._ish_home = Path(value)

    @property
    def LANG_DIR(self) -> Path:
        """Return the directory for language-specific JSON translations."""
        return self.ISH_HOME / self._LANG_DIRECTORY

    @property
    def PLUGIN_DIR(self) -> Path:
        """Return the common parent directory for plugin data."""
        return self.ISH_HOME / self._PLUGIN_DIRECTORY

    @property
    def PLUGIN_LIB_DIR(self) -> Path:
        """Return the separate installation directory for plugin Python dependencies."""
        return self.PLUGIN_DIR / self._LIB_DIRECTORY

    @property
    def PLUGIN_SCRIPT_DIR(self) -> Path:
        """Return the directory containing plugin sources to load."""
        return self.PLUGIN_DIR / self._SCRIPT_DIRECTORY

    @property
    def RC_FILE(self) -> Path:
        """Return the path to the user's Python initialization script."""
        return self.ISH_HOME / self._RC_FILENAME

    @property
    def LOG_FILE(self) -> Path:
        """Return the log path used when file logging is enabled."""
        return self.ISH_HOME / self._LOG_FILENAME

    @property
    def CACHE_DIR(self) -> Path:
        """Return the ish user cache directory."""
        return self.ISH_HOME / self._CACHE_DIRECTORY

    def reset(self) -> None:
        """Clear the home override and restore default path resolution."""
        self._ish_home = None


config = Config()
