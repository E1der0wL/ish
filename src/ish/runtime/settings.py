"""Declare ish startup settings and evaluate its optional Python rc file."""

from __future__ import annotations

import math
import os
import runpy
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ish.lang import Message, i18n
from ish.lang.i18n import valid_language
from ish.log import LOG_LEVELS
from ish.runtime.startup import login_shell

ESCAPE_DELAY = 0.02
MIN_ESCAPE_DELAY = 0.005
MAX_ESCAPE_DELAY = 2.0
INTERACTIVE_ARGS = ("-i",)
PATH_OPTIONS = ("cwd", "plugin_dir", "plugin_lib_dir", "log_file")
RC_RUN_NAME = "__ishrc__"


class SettingsError(ValueError):
    """Keep a settings diagnostic translatable until startup selects its language."""

    def __init__(self, message: Message, **values):
        """Retain the owned message and its data without freezing a locale."""
        super().__init__(message)
        self.message, self.values = message, values

    def __str__(self):
        """Render only after the caller has applied CLI and rc language precedence."""
        return i18n.get(self.message, **self.values)


class RcError(ValueError):
    """Retain a failed rc's partial settings and original exception for reporting."""

    def __init__(self, path: Path, settings: Settings, error: BaseException):
        """Preserve the explicit language even when later configuration fails."""
        super().__init__(path)
        self.path, self.settings, self.error = path, settings, error

    def __str__(self):
        """Translate the ish context while preserving foreign exception details."""
        error = str(self.error)
        if not isinstance(self.error, SettingsError):
            error = f"{type(self.error).__name__}: {error}"
        return i18n.get(Message.STARTUP_RC_ERROR, path=self.path, error=error)


def validate_command(value):
    """Accept a blank default or a quoted executable and its fixed arguments."""
    if not isinstance(value, str) or (value and not value.isprintable()):
        raise SettingsError(Message.MANAGE_COMMAND_INVALID)
    value = value.strip()
    if value:
        try:
            executable, *_ = shlex.split(value)
        except ValueError as error:
            raise SettingsError(Message.MANAGE_COMMAND_INVALID) from error
        if not executable:
            raise SettingsError(Message.MANAGE_COMMAND_INVALID)
    return value


def validate_escape_delay(value):
    """Return a finite byte-sequence wait in seconds, rejecting ambiguous values."""
    if (
        type(value) not in (int, float)
        or not MIN_ESCAPE_DELAY <= value <= MAX_ESCAPE_DELAY
        or not math.isfinite(value)
    ):
        raise SettingsError(
            Message.STARTUP_SETTING_ESCAPE,
            minimum=MIN_ESCAPE_DELAY,
            maximum=MAX_ESCAPE_DELAY,
        )
    return float(value)


def setting_path(value, base: Path, *, name: str) -> Path | None:
    """Resolve a setting against the rc directory used by the next session."""
    if value is None and name != "cwd":
        return None
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        raise SettingsError(Message.STARTUP_SETTING_PATH, name=name)
    try:
        return (base / Path(value).expanduser()).resolve()
    except RuntimeError as error:
        raise SettingsError(
            Message.STARTUP_PATH_RESOLUTION, name=name, error=error
        ) from error


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
    log_file: Path | None = None
    log_level: str = "WARNING"
    escape_delay: float = ESCAPE_DELAY

    def validate(self, base: Path):
        """Normalize file-relative paths and reject misspelled or invalid choices."""
        if self.lang is not None and not valid_language(self.lang):
            raise SettingsError(Message.I18N_LANGUAGE_ERROR)
        if not isinstance(self.shell, str) or not self.shell or "\0" in self.shell:
            raise SettingsError(Message.STARTUP_SETTING_SHELL)
        if "/" in self.shell or self.shell.startswith("~"):
            # Keep the selected symlink name: providers may interpret argv[0].
            try:
                self.shell = os.path.abspath(base / Path(self.shell).expanduser())
            except RuntimeError as error:
                raise SettingsError(
                    Message.STARTUP_PATH_RESOLUTION, name="shell", error=error
                ) from error
        for name in ("plugins", "shell_args"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(value, str) or "\0" in value for value in values
            ):
                raise SettingsError(Message.STARTUP_SETTING_LIST, name=name)
            setattr(self, name, list(values))
        if any(not name for name in self.plugins):
            raise SettingsError(Message.STARTUP_SETTING_PLUGINS)
        self.plugins = list(dict.fromkeys(self.plugins))
        for name in PATH_OPTIONS:
            setattr(self, name, setting_path(getattr(self, name), base, name=name))
        if (
            not isinstance(self.log_level, str)
            or self.log_level.upper() not in LOG_LEVELS
        ):
            raise SettingsError(
                Message.STARTUP_SETTING_LEVEL, levels=", ".join(LOG_LEVELS)
            )
        self.log_level = self.log_level.upper()
        self.escape_delay = validate_escape_delay(self.escape_delay)


def load(path: Path, *, settings_type=Settings) -> Settings:
    """Execute only ish's selected rc, without creating files or loading plugins."""
    settings = settings_type()
    try:
        if path.exists():
            if not path.is_file():
                raise SettingsError(Message.STARTUP_RC_FILE_ERROR)
            namespace = runpy.run_path(
                str(path), run_name=RC_RUN_NAME, init_globals={"config": settings}
            )
            if namespace.get("config") is not settings:
                raise SettingsError(Message.MANAGE_CONFIG_OBJECT)
        settings.validate(path.parent)
    except (Exception, SystemExit) as error:
        raise RcError(path, settings, error) from error
    return settings
