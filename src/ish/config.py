"""Resolve settings, plugin, and log paths and prepare required directories."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Optional, Union

__all__ = ["config"]


class Config:
    """Resolve configuration paths relative to the user's home directory.

    Reading properties does not create directories; ensure_directories handles creation.
    """

    SHELLS = {"sh", "bash", "zsh", "csh", "tcsh"}

    def __init__(self) -> None:
        """Initialize the home override to use the default user path."""
        self._ish_home: Optional[Path] = None

    @property
    def ISH_HOME(self) -> Path:
        """Return the overridden home or the default ~/ish path."""
        if self._ish_home is None:
            return Path.home() / "ish"
        return self._ish_home

    @ISH_HOME.setter
    def ISH_HOME(self, value: Union[str, Path]) -> None:
        """Change the base directory for subsequent user path lookups."""
        self._ish_home = Path(value)

    @property
    def LANG_DIR(self) -> Path:
        """Return the directory for language-specific JSON translations."""
        return self.ISH_HOME / "lang"

    @property
    def PLUGIN_DIR(self) -> Path:
        """Return the common parent directory for plugin data."""
        return self.ISH_HOME / "plugin"

    @property
    def PLUGIN_LIB_DIR(self) -> Path:
        """Return the separate installation directory for plugin Python dependencies."""
        return self.ISH_HOME / "plugin" / "lib"

    @property
    def PLUGIN_SCRIPT_DIR(self) -> Path:
        """Return the directory containing plugin sources to load."""
        return self.ISH_HOME / "plugin" / "script"

    @property
    def RC_FILE(self) -> Path:
        """Return the path to the user's Python initialization script."""
        return self.ISH_HOME / ".ishrc.py"

    @property
    def LOG_FILE(self) -> Path:
        """Return the log path used when file logging is enabled."""
        return self.ISH_HOME / ".ish.log"

    @property
    def CACHE_DIR(self) -> Path:
        """Return the ish user cache directory."""
        return self.ISH_HOME / ".cache"

    @property
    def XDG_DATA_HOME(self) -> Path:
        """Return the ish directory under XDG_DATA_HOME or its standard default."""
        base_data_dir: str = os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
        )
        xdg_data_home: Path = Path(base_data_dir) / "ish"
        return xdg_data_home

    @property
    def EXEC_CMD(self) -> Path:
        """Quote the current executable and arguments for reuse in a shell command."""
        exe, *args = sys.argv
        if os.path.sep in exe:
            exe_path = os.path.abspath(exe)
        else:
            exe_path = shutil.which(exe)
        quoted_args = [shlex.quote(arg) for arg in args]
        return f"{shlex.quote(exe_path)} {' '.join(quoted_args)}"

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

    def reset(self) -> None:
        """Clear the home override and restore default path resolution."""
        self._ish_home = None


config = Config()
