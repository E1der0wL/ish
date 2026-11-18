"""Declare the terminal settings used by ish and its configuration window."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.output import ColorDepth

from ish.lang import Message
from ish.runtime.settings import Settings as BaseSettings
from ish.runtime.settings import SettingsError
from ish.runtime.settings import load as load_common

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
class Settings(BaseSettings):
    """Share startup choices, keeping terminal policy in the explicit VT namespace."""

    mouse: str = DEFAULT_MOUSE
    clipboard: str = DEFAULT_CLIPBOARD
    scrollback_lines: int = DEFAULT_SCROLLBACK_LINES
    scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES
    color_depth: str = DEFAULT_COLOR_DEPTH
    features: dict[str, str] = field(default_factory=dict)

    def validate(self, base: Path):
        """Reject invalid policies before acquiring a terminal or native library."""
        BaseSettings.validate(self, base)
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
    """Evaluate the one Python configuration exactly once using terminal fields."""
    return load_common(path, settings_type=Settings)
