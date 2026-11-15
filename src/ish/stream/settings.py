"""Declare ish startup settings and evaluate its optional Python rc file."""

from __future__ import annotations

import math
import os
import runpy
from dataclasses import dataclass, field
from pathlib import Path

from ish.log import LOG_LEVELS
from ish.runtime.startup import login_shell

ESCAPE_DELAY = 0.05
MIN_ESCAPE_DELAY = 0.005
MAX_ESCAPE_DELAY = 2.0
DEFAULT_LOG_LEVEL = "WARNING"
INTERACTIVE_ARGS = ("-i",)
PATH_OPTIONS = ("cwd", "plugin_dir", "plugin_lib_dir", "integration_dir", "log_file")
RC_RUN_NAME = "__ishrc__"


def validate_escape_delay(value):
    """Return a finite byte-sequence wait in seconds, rejecting ambiguous values."""
    if (
        type(value) not in (int, float)
        or not MIN_ESCAPE_DELAY <= value <= MAX_ESCAPE_DELAY
        or not math.isfinite(value)
    ):
        raise ValueError(
            f"escape_delay must be between {MIN_ESCAPE_DELAY} and {MAX_ESCAPE_DELAY} seconds"
        )
    return float(value)


@dataclass(slots=True)
class Settings:
    """Hold next-session choices; PluginManager remains responsible for loading."""

    lang: str | None = None
    shell: str = field(default_factory=login_shell)
    shell_args: list[str] = field(default_factory=lambda: list(INTERACTIVE_ARGS))
    cwd: Path = field(default_factory=Path.cwd)
    plugins: list[str] = field(default_factory=list)
    plugin_dir: Path | None = None
    plugin_lib_dir: Path | None = None
    integration_dir: Path | None = None
    log_file: Path | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    escape_delay: float = ESCAPE_DELAY

    def validate(self, base: Path):
        """Normalize file-relative paths and reject misspelled or invalid choices."""
        if self.lang is not None and (not isinstance(self.lang, str) or not self.lang):
            raise ValueError("lang must be a nonempty language code or None")
        if not isinstance(self.shell, str) or not self.shell or "\0" in self.shell:
            raise ValueError("shell must be a nonempty executable name or path")
        if "/" in self.shell or self.shell.startswith("~"):
            # Keep the selected symlink name: providers may interpret argv[0].
            self.shell = os.path.abspath(base / Path(self.shell).expanduser())
        for name in ("plugins", "shell_args"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(value, str) or "\0" in value for value in values
            ):
                raise ValueError(f"{name} must be a list of strings")
            setattr(self, name, list(values))
        if any(not name for name in self.plugins):
            raise ValueError("plugins must contain nonempty names")
        self.plugins = list(dict.fromkeys(self.plugins))
        for name in PATH_OPTIONS:
            value = getattr(self, name)
            if value is None and name != "cwd":
                continue
            if (
                not isinstance(value, (str, Path))
                or not str(value)
                or "\0" in str(value)
            ):
                raise ValueError(f"{name} must be a path")
            path = Path(value).expanduser()
            setattr(self, name, (base / path).resolve())
        if (
            not isinstance(self.log_level, str)
            or self.log_level.upper() not in LOG_LEVELS
        ):
            raise ValueError("log_level must be one of " + ", ".join(LOG_LEVELS))
        self.log_level = self.log_level.upper()
        self.escape_delay = validate_escape_delay(self.escape_delay)


def load(path: Path) -> Settings:
    """Execute only ish's selected rc, without creating files or loading plugins."""
    settings = Settings()
    try:
        if path.exists():
            if not path.is_file():
                raise ValueError("configuration must be a regular Python file")
            runpy.run_path(
                str(path), run_name=RC_RUN_NAME, init_globals={"config": settings}
            )
        settings.validate(path.parent)
    except (Exception, SystemExit) as error:
        raise ValueError(f"{path}: {type(error).__name__}: {error}") from error
    return settings
