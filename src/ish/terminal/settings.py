"""Resolve VT choices from the shared rc without loading stream UI plugins."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

from prompt_toolkit.output import ColorDepth

from ish.lang import Message
from ish.stream.settings import RcError, SettingsError
from ish.stream.settings import Settings as StreamSettings
from ish.stream.settings import load as load_common

from .features import validate as validate_features
from .mouse import DEFAULT_MOUSE, MOUSE_POLICIES
from .vt import DEFAULT_SCROLLBACK_BYTES, DEFAULT_SCROLLBACK_LINES

CLIPBOARD_POLICIES = ("off", "write", "read-write")
DEFAULT_CLIPBOARD = "write"
LIMIT_MAXIMUM = (1 << 63) - 1
DEFAULT_COLOR_DEPTH = "inherit"
COLOR_DEPTHS = {
    "inherit": None,
    "1": ColorDepth.DEPTH_1_BIT,
    "4": ColorDepth.DEPTH_4_BIT,
    "8": ColorDepth.DEPTH_8_BIT,
    "24": ColorDepth.DEPTH_24_BIT,
}
# These belong to the other execution path; explicit VT plugins are independent.
STREAM_ONLY = frozenset(("plugins", "integration_dir", "vt"))


def validate_color_depth(value):
    """Validate explicit color policy without inferring host capabilities."""
    if not isinstance(value, str) or value not in COLOR_DEPTHS:
        raise SettingsError(
            Message.VT_SETTING_CHOICE,
            name="color_depth",
            choices=", ".join(COLOR_DEPTHS),
        )
    return value


@dataclass(slots=True)
class Settings(StreamSettings):
    """Share startup choices, keeping terminal policy in the explicit VT namespace."""

    mouse: str = DEFAULT_MOUSE
    clipboard: str = DEFAULT_CLIPBOARD
    scrollback_lines: int = DEFAULT_SCROLLBACK_LINES
    scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES
    color_depth: str = DEFAULT_COLOR_DEPTH
    features: dict[str, str] = field(default_factory=dict)

    def validate(self, base: Path):
        """Reject invalid policies before acquiring a terminal or native library."""
        StreamSettings.validate(self, base)
        self.color_depth = validate_color_depth(self.color_depth)
        if not isinstance(self.features, dict):
            raise SettingsError(Message.VT_FEATURES_MAPPING)
        self.features = validate_features(self.features)
        for name, choices in (
            ("mouse", MOUSE_POLICIES),
            ("clipboard", CLIPBOARD_POLICIES),
        ):
            if getattr(self, name) not in choices:
                raise SettingsError(
                    Message.VT_SETTING_CHOICE, name=name, choices=", ".join(choices)
                )
        for name in ("scrollback_lines", "scrollback_bytes"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= LIMIT_MAXIMUM:
                raise SettingsError(
                    Message.VT_SETTING_LIMIT, name=name, maximum=LIMIT_MAXIMUM
                )


def load(path: Path) -> Settings:
    """Reuse the rc evaluator once; only config.vt selects terminal plugins."""
    common = load_common(path)
    settings = Settings()
    for item in fields(StreamSettings):
        if item.name not in STREAM_ONLY:
            setattr(settings, item.name, getattr(common, item.name))
    try:
        if not isinstance(common.vt, dict):
            raise SettingsError(Message.VT_SETTING_NAMESPACE)
        allowed = {item.name for item in fields(Settings)} - {"integration_dir", "vt"}
        for name, value in common.vt.items():
            if name not in allowed:
                raise SettingsError(Message.VT_SETTING_UNKNOWN, name=name)
            setattr(settings, name, value)
        settings.validate(path.parent)
    except (Exception, SystemExit) as error:
        raise RcError(path, settings, error) from error
    return settings
