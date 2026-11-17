"""Decode outer terminal mouse reports without interpreting rendered text.

Coordinates are physical terminal cells, independent of Unicode text width.
The caller must enable cell reporting (1006), not pixel reporting (1016).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ish.lang import Message, i18n

from .vt import ALT, CTRL, SHIFT

DEFAULT_MOUSE = "auto"
MOUSE_POLICIES = ("off", "auto", "on")
MAX_COORDINATE = 65_535
LEGACY_PREFIX = "\x1b[M"
LEGACY_LENGTH = 6
DECIMAL_MOUSE = re.compile(r"\x1b\[(<)?([0-9]{1,3});([0-9]{1,5});([0-9]{1,5})([Mm])")
BUTTONS = ("left", "middle", "right", None)
WHEELS = ("wheelup", "wheeldown", "wheelleft", "wheelright")
WIRE_MODIFIERS = ((4, SHIFT), (8, ALT), (16, CTRL))
WIRE_BUTTON = 3
WIRE_MOTION = 32
WIRE_WHEEL = 64
WIRE_MASK = 127
BYTE_OFFSET = 32
BYTE_MAX = 255
SURROGATE_MIN = 0xDC80
SURROGATE_MAX = 0xDCFF
SURROGATE_OFFSET = 0xDC00


def validate_mouse_policy(policy):
    """Validate the same capture contract for startup and live UI consumers."""
    if policy not in MOUSE_POLICIES:
        raise ValueError(
            i18n.get(
                Message.VT_SETTING_CHOICE,
                name="mouse",
                choices=", ".join(MOUSE_POLICIES),
            )
        )
    return policy


@dataclass(frozen=True)
class MouseEvent:
    """One mouse report with zero-based cell coordinates and VT modifier flags."""

    action: str
    button: str | None
    x: int
    y: int
    modifiers: int


def _legacy_byte(character: str) -> int | None:
    value = ord(character)
    if SURROGATE_MIN <= value <= SURROGATE_MAX:
        value -= SURROGATE_OFFSET
    return value if 0 <= value <= BYTE_MAX else None


def decode_mouse(data: str) -> MouseEvent | None:
    """Decode one SGR, legacy X10-format, or urxvt report; reject other input.

    Legacy release reports do not identify the released button and return
    ``button=None``. This function never invents a previously pressed button.
    Unknown buttons, malformed reports, and out-of-range coordinates are
    rejected, not returned as ordinary command input. It does not implement
    UTF-8 mouse mode (1005), pixel coordinates, or highlight tracking.
    """
    if data.startswith(LEGACY_PREFIX):
        if len(data) != LEGACY_LENGTH:
            return None
        values = tuple(_legacy_byte(character) for character in data[3:])
        if None in values:
            return None
        code, x, y = (value - BYTE_OFFSET for value in values)
        sgr, ending = False, "M"
    else:
        match = DECIMAL_MOUSE.fullmatch(data)
        if match is None:
            return None
        prefix, code, x, y, ending = match.groups()
        sgr = prefix is not None
        code, x, y = int(code), int(x), int(y)
        if not sgr:
            if ending != "M":
                return None
            code -= BYTE_OFFSET
    if not 1 <= x <= MAX_COORDINATE or not 1 <= y <= MAX_COORDINATE:
        return None
    if code < 0 or code & ~WIRE_MASK:
        return None

    modifiers = 0
    for wire, modifier in WIRE_MODIFIERS:
        if code & wire:
            modifiers |= modifier
    button = BUTTONS[code & WIRE_BUTTON]
    if code & WIRE_WHEEL:
        # Wheel reports are presses, never drags or releases.
        if code & WIRE_MOTION or ending == "m":
            return None
        action, button = "press", WHEELS[code & WIRE_BUTTON]
    elif code & WIRE_MOTION:
        if ending == "m":
            return None
        action = "move"
    elif sgr:
        if button is None:
            return None
        action = "release" if ending == "m" else "press"
    else:
        action = "release" if button is None else "press"
    return MouseEvent(action, button, x - 1, y - 1, modifiers)
