"""Own host feature choices separately from observed support and active output.

``on`` requests a protocol; it never manufactures a successful query. ``auto``
requires positive evidence from the query listed here. The broker additionally
enforces geometry and restoration requirements for either choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ish.lang import Message
from ish.stream.settings import SettingsError

SWITCHES = ("on", "off")
QUERIED_SWITCHES = (*SWITCHES, "auto")


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Declare a feature's default and its implemented support query, if any."""

    default: str
    query: str | None = None

    @property
    def choices(self):
        """Offer automatic selection only for an implemented support query."""
        return QUERIED_SWITCHES if self.query is not None else SWITCHES


FEATURES = MappingProxyType(
    {
        "hyperlinks": FeatureSpec("on"),
        "extended_styles": FeatureSpec("on"),
        "overline": FeatureSpec("on"),
        "cursor_color": FeatureSpec("on"),
        "iterm_graphics": FeatureSpec("off"),
        "kitty_graphics": FeatureSpec("auto", "kitty-query"),
        "synchronized_output": FeatureSpec("auto", "DECRQM-2026"),
        "enhanced_keyboard": FeatureSpec("auto", "CSI-?u"),
        "pixel_mouse": FeatureSpec("auto", "DECRQM-1016"),
        "clipboard_protocol": FeatureSpec("auto", "DECRQM-5522"),
        "scheme_notifications": FeatureSpec("auto", "DECRQM-2031"),
    }
)


@dataclass(frozen=True, slots=True)
class FeatureState:
    """Report a requested choice, independent observation and actual selection."""

    policy: str
    support: str
    active: bool
    reason: str


def validate(values: Mapping | None = None) -> dict[str, str]:
    """Copy and validate overrides without querying a terminal or changing it."""
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise SettingsError(Message.VT_FEATURES_MAPPING)
    result = {name: spec.default for name, spec in FEATURES.items()}
    for name, policy in values.items():
        if not isinstance(name, str) or name not in FEATURES:
            raise SettingsError(Message.VT_SETTING_UNKNOWN, name=f"features.{name}")
        choices = FEATURES[name].choices
        if not isinstance(policy, str) or policy not in choices:
            raise SettingsError(
                Message.VT_SETTING_CHOICE,
                name=f"features.{name}",
                choices=", ".join(choices),
            )
        result[name] = policy
    return result


def parse_assignment(value: str) -> tuple[str, str]:
    """Interpret one CLI override using the same registry as Python settings."""
    name, separator, policy = value.partition("=")
    if not separator or not name or not policy:
        raise SettingsError(Message.VT_FEATURE_ASSIGNMENT)
    validate({name: policy})
    return name, policy


def merge(current: Mapping, assignments) -> dict[str, str]:
    """Override only the named rc features; the last CLI choice wins."""
    result = validate(current)
    result.update(assignments)
    return validate(result)


def resolve(policy: str, support: bool | None) -> bool:
    """Select a feature without replacing unknown/negative observations."""
    return policy == "on" or (policy == "auto" and support is True)


def observation(name: str, support: bool | None) -> str:
    """Keep no query, no answer and an explicit rejection distinguishable."""
    if FEATURES[name].query is None:
        return "unqueried"
    if support is None:
        return "unknown"
    return "supported" if support else "unsupported"
