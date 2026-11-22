"""Negotiate the outer terminal through the one PTK input reader.

Host responses are control traffic, never shell keystrokes. The optional probe
only selects presentation capabilities; it does not determine shell readiness.
The capability probe remains one-shot. Optional geometry refreshes use one
outstanding size-report pair at a time, requiring an ordered responding host.
Internal generations cannot identify arbitrary delayed duplicates on the wire.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import select
import socket
import termios
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType
from urllib.parse import quote

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import KEY_ALIASES, Keys

from ish.lang import Message, i18n

from .features import FEATURES, FeatureState, observation, resolve
from .features import validate as validate_features
from .hosts import UNKNOWN, HostAdapter, identify_host
from .identity import CONTROL_TEXT, NAMED_TEXT
from .mouse import decode_mouse
from .vt import ALT, CTRL, SHIFT

ESC = "\x1b"
ST = ESC + "\\"
PASTE_START = ESC + "[200~"
PASTE_END = ESC + "[201~"
MAX_DIMENSION = 65_535
RGB_REPORT = re.compile(
    r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})\Z"
)
PALETTE_SIZE = 256
GRAPHICS_PENDING = "host-probe-pending"
GRAPHICS_NO_SIZE = "verified-cell-size-unavailable"
GRAPHICS_RESIZED = "cell-size-unavailable-after-resize"
GRAPHICS_CANCELLED = "host-probe-cancelled"
GRAPHICS_DISABLED = "image-protocols-disabled"
KEY_ACTIONS = {1: "press", 2: "repeat", 3: "release"}
KEY_REPORT = re.compile(r"\x1b\[([0-9:;]*)([u~ABCDFHPQS])\Z")
FUNCTION_KEYS = {
    27: "escape",
    13: "enter",
    9: "tab",
    127: "backspace",
    57344: "escape",
    57345: "enter",
    57346: "tab",
    57347: "backspace",
    57348: "insert",
    57349: "delete",
    57350: "left",
    57351: "right",
    57352: "up",
    57353: "down",
    57354: "pageup",
    57355: "pagedown",
    57356: "home",
    57357: "end",
    **{57363 + number: f"f{number}" for number in range(1, 26)},
    **{57399 + number: f"kp{number}" for number in range(10)},
    57409: "kpdecimal",
    57410: "kpdivide",
    57411: "kpmultiply",
    57412: "kpsubtract",
    57413: "kpadd",
    57414: "kpenter",
    57415: "kpequal",
    57416: "kpseparator",
    57417: "kpleft",
    57418: "kpright",
    57419: "kpup",
    57420: "kpdown",
    57421: "kppageup",
    57422: "kppagedown",
    57423: "kphome",
    57424: "kpend",
    57425: "kpinsert",
    57426: "kpdelete",
    57427: "kpbegin",
}
KEYPAD_LEGACY = {
    **{f"kp{number}": str(number) for number in range(10)},
    "kpdecimal": ".",
    "kpdivide": "/",
    "kpmultiply": "*",
    "kpsubtract": "-",
    "kpadd": "+",
    "kpenter": "enter",
    "kpequal": "=",
    "kpseparator": ",",
    **{
        "kp" + key: key
        for key in (
            "left",
            "right",
            "up",
            "down",
            "pageup",
            "pagedown",
            "home",
            "end",
            "insert",
            "delete",
        )
    },
}
LETTER_KEYS = {
    "A": 57352,
    "B": 57353,
    "C": 57351,
    "D": 57350,
    "F": 57357,
    "H": 57356,
    "P": 57364,
    "Q": 57365,
    "S": 57367,
}
TILDE_KEYS = {
    2: 57348,
    3: 57349,
    5: 57354,
    6: 57355,
    7: 57356,
    8: 57357,
    11: 57364,
    12: 57365,
    13: 57366,
    14: 57367,
    15: 57368,
    17: 57369,
    18: 57370,
    19: 57371,
    20: 57372,
    21: 57373,
    23: 57374,
    24: 57375,
}
NAMED_KEYS = {
    "escape": Keys.Escape,
    "enter": Keys.ControlM,
    "tab": Keys.ControlI,
    "backspace": Keys.ControlH,
    "insert": Keys.Insert,
    "delete": Keys.Delete,
    "left": Keys.Left,
    "right": Keys.Right,
    "up": Keys.Up,
    "down": Keys.Down,
    "pageup": Keys.PageUp,
    "pagedown": Keys.PageDown,
    "home": Keys.Home,
    "end": Keys.End,
    **{f"f{number}": Keys(f"f{number}") for number in range(1, 25)},
}
CONTROL_KEYS = {
    " ": Keys.ControlAt,
    "@": Keys.ControlAt,
    "[": Keys.Escape,
    "\\": Keys.ControlBackslash,
    "]": Keys.ControlSquareClose,
    "^": Keys.ControlCircumflex,
    "_": Keys.ControlUnderscore,
    "?": Keys.ControlH,
}
HOST_ENVIRONMENT = frozenset(
    (
        "KITTY_WINDOW_ID",
        "KITTY_LISTEN_ON",
        "KITTY_PID",
        "WEZTERM_PANE",
        "WEZTERM_UNIX_SOCKET",
        "WEZTERM_EXECUTABLE",
        "WEZTERM_EXECUTABLE_DIR",
        "TMUX",
        "TMUX_PANE",
        "STY",
        "WINDOWID",
        "ITERM_SESSION_ID",
        "ITERM_PROFILE",
        "LC_TERMINAL",
        "LC_TERMINAL_VERSION",
        "TERM_PROGRAM_VERSION",
        "VTE_VERSION",
        "WT_SESSION",
        "WT_PROFILE_ID",
    )
)


@dataclass(frozen=True, slots=True)
class HostAppearance:
    """Observed host colors; None is unknown, never a guessed black or white.

    Defaults describe color queries, not explicit cell styling. A renderer must
    continue to use terminal-default SGR for inherited foreground/background.
    """

    foreground: tuple[int, int, int] | None = None
    background: tuple[int, int, int] | None = None
    cursor: tuple[int, int, int] | None = None
    cursor_dynamic: bool | None = None
    palette: tuple[tuple[int, int, int] | None, ...] = (None,) * PALETTE_SIZE
    scheme: str | None = None


@dataclass(frozen=True, slots=True)
class HostCursorStyle:
    """One concrete DECRQSS observation; zero/default is deliberately unrepresented."""

    CURSOR_STYLES = {
        1: ("block", True),
        2: ("block", False),
        3: ("underline", True),
        4: ("underline", False),
        5: ("bar", True),
        6: ("bar", False),
    }

    code: int

    @property
    def shape(self):
        """Interpret the host wire value at its owning boundary."""
        return self.CURSOR_STYLES[self.code][0]

    @property
    def blink(self):
        """Keep blink tied to the same observed cursor value."""
        return self.CURSOR_STYLES[self.code][1]


@dataclass(frozen=True, slots=True)
class HostProfile:
    """Effective presentation choices; support observations remain in the broker."""

    kitty_graphics: bool = False
    sixel_graphics: bool = False
    desktop_notifications: bool = False
    hyperlinks: bool = False
    synchronized_output: bool = False
    cell_width: int = 0
    cell_height: int = 0
    pixel_width: int = 0
    pixel_height: int = 0
    enhanced_keyboard: bool = False
    extended_styles: bool = False
    pixel_mouse: bool = False
    clipboard_protocol: bool = False
    image_protocol: str | None = None
    appearance: HostAppearance = HostAppearance()
    initial_appearance: HostAppearance = HostAppearance()
    scheme_notifications: bool = False
    cursor_color_control: bool = False
    overline: bool = False
    initial_cursor_style: HostCursorStyle | None = None
    color_depth: int | None = None

    @property
    def has_cell_size(self) -> bool:
        """Whether explicit host geometry permits pixel/cell conversion."""
        return self.cell_width > 0 and self.cell_height > 0


@dataclass(frozen=True, slots=True)
class StructuredKey:
    """A mapped host key, retaining its wire identity and observed event action.

    Modifiers use the native bridge's SHIFT/CTRL/ALT bits. Lock modifiers are
    not physical key identities, and Super/Hyper/Meta or unmapped functional
    keys are outside this deliberately bounded contract. Keypad identities stay
    native; only the popup fallback converts them to ordinary editing keys.
    """

    key: str
    codepoint: int
    modifiers: int = 0
    action: str = "press"
    text: str | None = None
    shifted: int | None = None
    base: int | None = None
    # Capture the source contract at receipt. A later host negotiation cannot
    # turn a queued conventional keystroke into a physical press/release pair.
    release_events: bool = False

    @property
    def input_text(self):
        """Keep the produced text separate from the unshifted key identity."""
        if len(self.key) != 1:
            return None
        if self.text is not None:
            return self.text
        if self.modifiers & SHIFT:
            if self.shifted and chr(self.shifted).isprintable():
                return chr(self.shifted)
            if "a" <= self.key <= "z":
                return self.key.upper()
        return self.key

    def matches_shortcut(self, binding):
        """Test a PTK single-key binding against the whole observed stroke."""
        if self.action == "release":
            return False
        if binding == Keys.Any:
            return True
        if self.modifiers & ALT:
            # PTK exposes Alt as two widget keys; neither alone owns the stroke.
            return False
        if self.modifiers in (0, SHIFT) and len(self.key) == 1:
            return self.input_text == binding
        ctrl = bool(self.modifiers & CTRL)
        name = "@" if ctrl and self.key == " " else self.key
        stroke = (
            ("c-" if ctrl else "") + ("s-" if self.modifiers & SHIFT else "") + name
        )
        return KEY_ALIASES.get(stroke, stroke) == KEY_ALIASES.get(binding, binding)


class KeyMappings:
    """Own exact optional press mappings, fixed before the first host input.

    The public namespace is intentionally limited to unclaimed numeric CSI ~
    sequences. It cannot replace replies, paste framing, mouse reports or an
    existing PTK/native key, and it cannot generate event reporting semantics.
    """

    MAX_KEY_MAPPINGS = 16
    MAX_MAPPED_KEYS = 256
    MAX_KEY_SEQUENCE = 32
    KEY_MAPPING_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
    MAPPED_KEY_SEQUENCE = re.compile(r"\x1b\[[1-9][0-9]{0,5}(?:;[1-8])?~\Z")

    def __init__(self):
        """Start with no selected optional mappings and no retained host input."""
        self._events = {}
        self._owners = {}
        self._sealed = False

    @property
    def registrations(self) -> dict[str, int]:
        """Expose names and counts, never mutable maps or captured wire input."""
        return dict(self._owners)

    def seal(self) -> None:
        """Freeze the registry even when the first input is only a query reply."""
        self._sealed = True

    def register(self, name: str, mappings: Mapping[str, StructuredKey]) -> None:
        """Validate the whole registration before committing an immutable copy."""
        if self._sealed:
            raise RuntimeError(i18n.get(Message.VT_KEY_MAP_LATE))
        if (
            not isinstance(name, str)
            or self.KEY_MAPPING_NAME.fullmatch(name) is None
            or name in self._owners
        ):
            raise ValueError(i18n.get(Message.VT_KEY_MAP_NAME))
        if (
            not isinstance(mappings, Mapping)
            or not mappings
            or len(self._owners) >= self.MAX_KEY_MAPPINGS
            or len(self._events) + len(mappings) > self.MAX_MAPPED_KEYS
        ):
            raise ValueError(i18n.get(Message.VT_KEY_MAP_LIMIT))
        validated = {}
        for sequence, event in mappings.items():
            if (
                not isinstance(sequence, str)
                or len(sequence) > self.MAX_KEY_SEQUENCE
                or self.MAPPED_KEY_SEQUENCE.fullmatch(sequence) is None
            ):
                raise ValueError(i18n.get(Message.VT_KEY_MAP_SEQUENCE))
            if (
                sequence in (PASTE_START, PASTE_END)
                or int(sequence[2:].split(";", 1)[0].rstrip("~")) in (200, 201)
                or sequence in ANSI_SEQUENCES
                or decode_key(sequence) is not None
            ):
                raise ValueError(i18n.get(Message.VT_KEY_MAP_RESERVED))
            if sequence in self._events:
                raise ValueError(i18n.get(Message.VT_KEY_MAP_CONFLICT))
            if not self._valid_event(event):
                raise ValueError(i18n.get(Message.VT_KEY_MAP_EVENT))
            validated[sequence] = event
        self._events.update(validated)
        self._owners[name] = len(validated)

    @staticmethod
    def _valid_event(event) -> bool:
        if (
            type(event) is not StructuredKey
            or type(event.key) is not str
            or type(event.codepoint) is not int
            or type(event.modifiers) is not int
            or not 0 <= event.modifiers <= 7
            or event.action != "press"
            or event.release_events is not False
            or any(
                value is not None for value in (event.text, event.shifted, event.base)
            )
        ):
            return False
        native_name = FUNCTION_KEYS.get(event.codepoint)
        if native_name is not None:
            return native_name == event.key
        return (
            len(event.key) == 1
            and event.codepoint == ord(event.key)
            and _scalar(event.codepoint)
            and event.codepoint >= 32
            and not 127 <= event.codepoint < 160
            and not 57344 <= event.codepoint <= 63743
        )

    def get(self, sequence: str) -> StructuredKey | None:
        """A missing route remains a control; no decoder callback is executed."""
        return self._events.get(sequence)


@dataclass(slots=True)
class _KeyRoute:
    """Share one terminal dispatch result across PTK's legacy Alt key sequence."""

    event: StructuredKey
    handled: bool | None = None


@dataclass(slots=True)
class _FocusRoute:
    """Keep focus changes in the same input queue as keys and mouse events."""

    focused: bool
    handled: bool = False


@dataclass(slots=True)
class _ClipboardRoute:
    """A protocol reply whose child response must preserve input-reader order."""

    sequence: str
    handled: bool = False


@dataclass(slots=True)
class _AppearanceRoute:
    """Keep appearance changes ordered with keys without reviving closed probes."""

    sequence: str
    epoch: int
    handled: bool = False


@dataclass(slots=True)
class _GeometryQuery:
    """One identity-free wire pair and the layout it was requested for."""

    serial: int
    epoch: int
    grid: tuple[int, int]
    cell: tuple[int, int] | None = None
    pixels: tuple[int, int] | None = None


def _rgb(value: str) -> tuple[int, int, int] | None:
    """Decode only the concrete XTerm RGB response format, without color guesses."""
    match = RGB_REPORT.fullmatch(value)
    if match is None:
        return None
    return tuple(
        (int(part, 16) * 255 + (16 ** len(part) - 1) // 2) // (16 ** len(part) - 1)
        for part in match.groups()
    )


def _scalar(value: int) -> bool:
    return 0 <= value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF


def decode_key(sequence: str) -> StructuredKey | None:
    """Decode the documented CSI-u and legacy functional subset without guessing."""
    match = KEY_REPORT.fullmatch(sequence)
    if match is None:
        return None
    parameters, final = match.groups()
    fields = parameters.split(";")
    if len(fields) > 3:
        return None
    try:
        codes = [int(value) if value else None for value in fields[0].split(":")]
        modifiers = fields[1].split(":") if len(fields) > 1 else []
        if len(codes) > 3 or len(modifiers) > 2:
            return None
        wire_modifiers = (int(modifiers[0]) if modifiers and modifiers[0] else 1) - 1
        action = int(modifiers[1]) if len(modifiers) == 2 and modifiers[1] else 1
        if not 0 <= wire_modifiers <= 255 or wire_modifiers & (8 | 16 | 32):
            return None
        if action not in KEY_ACTIONS:
            return None
        identity = codes[0]
        if final in LETTER_KEYS:
            if identity not in (None, 1) or len(codes) > 1 or len(fields) > 2:
                return None
            identity = LETTER_KEYS[final]
        elif final == "~":
            if len(codes) > 1 or len(fields) > 2:
                return None
            identity = TILDE_KEYS.get(identity)
        if identity is None or not _scalar(identity):
            return None
        key = FUNCTION_KEYS.get(identity)
        if key is None:
            if identity < 32 or 127 <= identity < 160 or 57344 <= identity <= 63743:
                return None
            key = chr(identity)
        alternate = codes[1:] if final == "u" else []
        if any(value is not None and not _scalar(value) for value in alternate):
            return None
        text = None
        if len(fields) == 3 and fields[2]:
            points = [int(value) for value in fields[2].split(":")]
            if len(points) > 64 or any(
                not _scalar(value) or value < 32 or 127 <= value < 160
                for value in points
            ):
                return None
            text = "".join(map(chr, points))
    except ValueError:
        return None
    native_modifiers = (
        (SHIFT if wire_modifiers & 1 else 0)
        | (ALT if wire_modifiers & 2 else 0)
        | (CTRL if wire_modifiers & 4 else 0)
    )
    return StructuredKey(
        key,
        identity,
        native_modifiers,
        KEY_ACTIONS[action],
        text,
        alternate[0] if alternate else None,
        alternate[1] if len(alternate) > 1 else None,
    )


def _legacy_keys(event: StructuredKey) -> list[KeyPress]:
    """Map one structured event to PTK's existing popup editing vocabulary."""
    if event.action == "release":
        return [KeyPress(Keys.Ignore, "")]
    modifiers = event.modifiers
    prefix = [KeyPress(Keys.Escape, ESC)] if modifiers & ALT else []
    name = KEYPAD_LEGACY.get(event.key, event.key)
    if name in NAMED_KEYS:
        key = NAMED_KEYS[name]
        text = NAMED_TEXT.get(name, "")
        if name == "tab" and modifiers & SHIFT:
            key = Keys.BackTab
        elif name == "escape" and modifiers & SHIFT:
            key = Keys.ShiftEscape
        elif name not in NAMED_TEXT:
            if name.startswith("f") and modifiers & SHIFT:
                name = f"f{int(name[1:]) + 12}"
            else:
                name = ("s-" if modifiers & SHIFT else "") + name
            name = ("c-" if modifiers & CTRL else "") + name
            try:
                key = Keys(name)
            except ValueError:
                return [KeyPress(Keys.Ignore, "")]
        return prefix + [KeyPress(key, text)]
    if len(name) != 1:
        return [KeyPress(Keys.Ignore, "")]
    if modifiers & CTRL:
        character = name.lower()
        text = CONTROL_TEXT.get(character, "")
        if "a" <= character <= "z":
            text = chr(ord(character) - ord("a") + 1)
        name = (
            "c-"
            + (
                "s-"
                if modifiers & SHIFT and character.isascii() and character.isdigit()
                else ""
            )
            + character
        )
        if character == " ":
            name = "c-@"
        try:
            key = CONTROL_KEYS[character] if character in CONTROL_KEYS else Keys(name)
        except ValueError:
            return [KeyPress(Keys.Ignore, "")]
        return prefix + [KeyPress(key, text)]
    text = event.input_text if len(event.key) == 1 else name
    return prefix + [KeyPress(character, character) for character in text]


def scrub_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove host identities that a program inside the virtual PTY cannot use."""
    return {
        key: value for key, value in environment.items() if key not in HOST_ENVIRONMENT
    }


def directory_uri(value: str) -> str | None:
    """Normalize native absolute paths without guessing a relative directory."""
    if value.startswith("/"):
        return "file://" + socket.gethostname() + quote(value, safe="/")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    if value.startswith("file://"):
        return value
    return None


class _ColorDepthProbe:
    """Observe the outer renderer at startup, independently of its RGB palette."""

    QUERY = f"{ESC}P+q524742{ST}{ESC}P+q436f{ST}"
    REPORT = re.compile(
        r"\x1bP([01])\+r(524742|436[fF])(?:=((?:[0-9a-fA-F]{2}){0,16}))?\x1b\\\Z"
    )
    COLOR_COUNTS = {2: 1, 8: 4, 16: 4, 88: 4, 256: 8, 16777216: 24}

    def __init__(self, write: Callable[[str], None]):
        self.write = write
        self.depth: int | None = None
        self._seen: set[str] = set()
        self._samples: tuple[str, ...] = ()
        self._sample_received = False

    @property
    def complete(self) -> bool:
        """A definite RGB result supersedes indexed capabilities."""
        return self.depth == 24 or (
            len(self._seen) == 2 and (not self._samples or self._sample_received)
        )

    def capability(self, report) -> None:
        """Decode only requested names and concrete, representable color depths."""
        status, name, encoded = report.groups()
        name = name.lower()
        if name in self._seen:
            return
        self._seen.add(name)
        if status != "1" or not encoded or self.depth == 24:
            return
        value = bytes.fromhex(encoded)
        if name == "436f" and value.isdigit():
            self.depth = self.COLOR_COUNTS.get(int(value))
        elif name == "524742":
            # A boolean RGB capability alone does not specify channel widths.
            widths = value.split(b"/")
            if len(widths) in (1, 3) and all(
                width.isdigit() and int(width) >= 8 for width in widths
            ):
                self.depth = 24

    def sample(self, initial: str) -> None:
        """Set, query and restore SGR in one write, without drawing a character."""
        if self.depth == 24:
            return
        red, green, blue = secrets.token_bytes(3)
        channels = f"{red}:{green}:{blue}"
        semicolon = f"38;2;{red};{green};{blue}"
        self._samples = (
            semicolon,
            f"38:2::{channels}",
            f"38:2:0:{channels}",
            f"38:2:{channels}",
        )
        self.write(f"{ESC}[0;{semicolon}m{ESC}P$qm{ST}{ESC}[0;{initial}m")

    def observe_sample(self, report: str) -> None:
        """A delayed unrelated report or an indexed approximation cannot qualify."""
        self._sample_received = True
        if report.removeprefix("0;") in self._samples:
            self.depth = 24


class HostBroker:
    """Collect one bounded probe and consume subsequent host control replies.

    ``ready`` concerns only the optional host capability probe. Callers may wait
    with a deadline and call ``finish_probe`` to resolve policy and observations.
    No timeout grants input ownership or implies that a shell is ready.
    """

    MAX_CELL_PIXELS = 4096
    GEOMETRY_QUERY = "\x1b[16t\x1b[14t"
    GEOMETRY_FEATURES = (
        "kitty_graphics",
        "iterm_graphics",
        "sixel_graphics",
        "pixel_mouse",
    )
    MAX_GRAPHICS_ID = 2**32 - 1
    MIN_PROBE_ID = 2**31
    SIZE_REPORT = re.compile(r"\x1b\[([46]);([0-9]{1,5});([0-9]{1,5})t\Z")
    SYNC_REPORT = re.compile(r"\x1b\[\?2026;([0-4])\$y\Z")
    CLIPBOARD_REPORT = re.compile(r"\x1b\[\?5522;([0-4])\$y\Z")
    KEYBOARD_REPORT = re.compile(r"\x1b\[\?([0-9]{1,2})u\Z")
    SCHEME_MODE_REPORT = re.compile(r"\x1b\[\?2031;([0-4])\$y\Z")
    SCHEME_REPORT = re.compile(r"\x1b\[\?997;([12])n\Z")
    MODE_REPORT = re.compile(r"\x1b\[\?([0-9]{1,4});([0-4])\$y\Z")
    # These are the outer modes changed by PTK or this adapter. Unknown reports do
    # not imply a reset mode. Only a concrete observed value can be restored.
    RESTORE_MODES = (
        1,
        6,
        7,
        12,
        25,
        80,
        1000,
        1002,
        1003,
        1004,
        1005,
        1006,
        1015,
        1016,
        2004,
        2026,
    )
    MOTION_MODE = 1003
    MOUSE_MODES = (1000, 1002, MOTION_MODE, 1005, 1006, 1015, 1016)
    SCREEN_MODES = tuple(mode for mode in RESTORE_MODES if mode not in (1004, 2026))
    COLOR_NAMES = {10: "foreground", 11: "background", 12: "cursor"}
    SCHEMES = {1: "dark", 2: "light"}
    GRAPHICS_REPLY = re.compile(
        r"Gi=([0-9]{1,10})(?:,p=[0-9]{1,10})?;([^\x00-\x1f]*)\Z"
    )
    GRAPHICS_REJECTED = "host-did-not-confirm-image-protocol"
    ATTRIBUTES_REPLY = re.compile(r"\x1b\[\?([0-9;]{1,256})c\Z")
    REASON_ACTIVE = ""
    REASON_OFF = "policy-off"
    REASON_CANCELLED = "probe-cancelled"
    REASON_PENDING = "probe-pending"
    REASON_REJECTED = "support-rejected"
    REASON_UNKNOWN = "support-unconfirmed"
    REASON_IMAGE_SELECTED = "other-image-protocol-selected"
    REASON_CELL_SIZE = "cell-size-unavailable"
    REASON_CURSOR_STATE = "cursor-state-unavailable"
    REASON_SCHEME_STATE = "initial-scheme-unavailable"
    REASON_MODE_STATE = "initial-mode-unavailable"
    KEYBOARD_FLAGS = (
        3  # Disambiguation and event types; no all-key/associated-text claim.
    )
    CURSOR_STYLE_REPORT = re.compile(r"\x1bP([01])\$r([0-9]*) q\x1b\\\Z")
    ATTRIBUTES_REPORT = re.compile(r"\x1bP([01])\$r([0-9:;]{0,512})m\x1b\\\Z")

    def __init__(
        self,
        *,
        features: dict[str, str] | None = None,
        on_focus: Callable[[bool], None] | None = None,
        on_graphics_error: Callable[[int, str], None] | None = None,
        on_key: Callable[[StructuredKey], bool] | None = None,
        on_clipboard_reply: Callable[[str], bool] | None = None,
        on_appearance: Callable[[HostAppearance], None] | None = None,
        on_geometry: Callable[[], None] | None = None,
        on_color_probe: Callable[[str], None] | None = None,
    ):
        """Keep host state independent of the child VT and PTY lifetime."""
        self.features = MappingProxyType(validate_features(features))
        self.profile = HostProfile()
        self.graphics_reason = GRAPHICS_PENDING
        self.ready = asyncio.Event()
        self.on_focus = on_focus
        self.on_graphics_error = on_graphics_error
        self.on_key = on_key
        self.on_clipboard_reply = on_clipboard_reply
        self.on_appearance = on_appearance
        self.on_geometry = on_geometry
        self._depth_probe = _ColorDepthProbe(on_color_probe) if on_color_probe else None
        self.input_modes_active = False
        self.pixel_mouse_active = False
        self._keys = KeyMappings()
        self.dropped_controls = 0
        self._started = False
        self._closed = False
        self._query_id = None
        self._cols = self._rows = 0
        self._kitty_seen = self._kitty_ok = False
        self._sixel_support = None
        self._sync_seen = self._sync_ok = False
        self._clipboard_seen = self._clipboard_ok = False
        self._keyboard_flags = None
        self._version_seen = False
        self._cursor_style_seen = False
        self._initial_cursor_style = None
        self._attributes_seen = False
        self._initial_attributes = None
        self._attributes_changed = False
        self._mode_values = {}
        self.mouse_release_pending = False
        self._changed_modes = set()
        self._adapter = UNKNOWN
        self._appearance = HostAppearance()
        self._color_seen = set()
        self._cursor_extended_seen = False
        self._scheme_seen = False
        self._scheme_mode = None
        self._appearance_epoch = 0
        self._metadata_active = False
        self._theme_enabled = False
        self._cell = self._pixels = None
        self._accept_size_reports = True
        self._layout_size = None
        self._kernel_size = None
        self._kernel_inset = None
        self._probe_grid = None
        self._probe_grid_valid = True
        self._reported_cell = self._reported_pixels = None
        self._initial_geometry_seen = set()
        self._geometry_epoch = 0
        self._geometry_serial = 0
        self._geometry_target = None
        self._geometry_request = None
        self._geometry_blocked = False
        self._cancelled = False
        self._restore_only = False

    def _query_attributes(self) -> str:
        """Ask for color support before the initial SGR, allowing a read-only answer."""
        query = self._depth_probe.QUERY if self._depth_probe is not None else ""
        return query + f"{ESC}P$qm{ST}"

    def begin_restore_probe(self) -> str:
        """Observe PTK screen state and optional text color depth without VT features."""
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_HOST_PROBE_ONCE))
        self._started = self._restore_only = True
        return (
            f"{ESC}P$q q{ST}"
            + self._query_attributes()
            + f"{ESC}[?2026$p"
            + "".join(f"{ESC}[?{mode}$p" for mode in self.SCREEN_MODES)
        )

    @property
    def can_open_sync_frame(self) -> bool:
        """Open a bounded update only when the host reported the mode initially reset."""
        return self._closed and self._mode_values.get(2026) == 2

    @property
    def adapter(self) -> HostAdapter:
        """Expose known interpretation quirks separately from effective features."""
        return self._adapter

    def _support(self) -> dict[str, bool | None]:
        """Describe queried capabilities without turning user choices into evidence."""
        pixel = self._mode_values.get(1016)
        scheme = None if self._scheme_mode is None else self._scheme_mode in (1, 2, 3)
        return {
            "kitty_graphics": self._kitty_ok if self._kitty_seen else None,
            "sixel_graphics": self._sixel_support,
            "synchronized_output": self._sync_ok if self._sync_seen else None,
            "clipboard_protocol": self._clipboard_ok if self._clipboard_seen else None,
            "enhanced_keyboard": True if self._keyboard_flags is not None else None,
            "pixel_mouse": pixel in (1, 2) if pixel is not None else None,
            "scheme_notifications": scheme,
        }

    def _feature_values(self, width: int, height: int) -> dict[str, bool]:
        """Resolve once from policy and observations, including hard prerequisites."""
        enabled = self._feature_choices()
        geometry = width > 0 and height > 0
        for name in self.GEOMETRY_FEATURES:
            enabled[name] &= geometry
        return enabled

    def _feature_choices(self) -> dict[str, bool]:
        """Resolve eligibility before independently measured pixel geometry."""
        support = self._support()
        enabled = {
            name: not self._cancelled and resolve(policy, support.get(name))
            for name, policy in self.features.items()
        }
        # Exactly one backend owns image presentation. Explicit iTerm selection
        # wins over Kitty, without changing the independent Kitty observation.
        enabled["kitty_graphics"] &= not enabled["iterm_graphics"]
        enabled["sixel_graphics"] &= (
            not enabled["iterm_graphics"]
            and not enabled["kitty_graphics"]
            and self._mode_values.get(80) in (1, 2)
        )
        enabled["cursor_color"] &= (
            self._appearance.cursor is not None
            or self._appearance.cursor_dynamic is True
        ) and (not self._adapter.extended_cursor or self._cursor_extended_seen)
        enabled["scheme_notifications"] &= (
            self._scheme_mode in (1, 2, 3) and self._scheme_seen
        )
        return enabled

    @property
    def feature_status(self) -> dict[str, FeatureState]:
        """Expose requested policy, observed support and effective selection separately."""
        support = self._support()
        effective = {
            name: getattr(self.profile, name)
            for name in FEATURES
            if hasattr(self.profile, name)
        }
        effective["cursor_color"] = self.profile.cursor_color_control
        effective["iterm_graphics"] = self.profile.image_protocol == "iterm"
        states = {}
        for name, policy in self.features.items():
            active = effective[name]
            evidence = observation(name, support.get(name))
            if not self._started and FEATURES[name].query is not None:
                evidence = "unqueried"
            if active:
                reason = self.REASON_ACTIVE
            elif policy == "off":
                reason = self.REASON_OFF
            elif self._cancelled:
                reason = self.REASON_CANCELLED
            elif not self._closed:
                reason = self.REASON_PENDING
            elif not resolve(policy, support.get(name)):
                reason = (
                    self.REASON_REJECTED
                    if support.get(name) is False
                    else self.REASON_UNKNOWN
                )
            elif name in self.GEOMETRY_FEATURES:
                reason = (
                    self.REASON_IMAGE_SELECTED
                    if name != "pixel_mouse" and self.profile.image_protocol is not None
                    else self.REASON_MODE_STATE
                    if name == "sixel_graphics"
                    and self._mode_values.get(80) not in (1, 2)
                    else self.REASON_CELL_SIZE
                )
            elif name == "cursor_color":
                reason = self.REASON_CURSOR_STATE
            elif name == "scheme_notifications" and not self._scheme_seen:
                reason = self.REASON_SCHEME_STATE
            else:
                reason = self.REASON_MODE_STATE
            states[name] = FeatureState(policy, evidence, active, reason)
        return states

    @property
    def key_mappings(self) -> dict[str, int]:
        """Return payload-free optional input registrations for diagnostics."""
        return self._keys.registrations

    def register_keys(self, name: str, mappings: Mapping[str, StructuredKey]) -> None:
        """Register a bounded exact map through the session's only input broker."""
        self._keys.register(name, mappings)

    def seal_keys(self) -> None:
        """Close registration before input, independently of host probe readiness."""
        self._keys.seal()

    def begin_probe(self, cols: int, rows: int, *, query_id: int | None = None) -> str:
        """Register an upper-half identity, disjoint from presenter image IDs."""
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_HOST_PROBE_ONCE))
        if not 0 < cols <= MAX_DIMENSION or not 0 < rows <= MAX_DIMENSION:
            raise ValueError(i18n.get(Message.VT_HOST_DIMENSIONS))
        if query_id is None:
            query_id = secrets.randbelow(self.MIN_PROBE_ID) + self.MIN_PROBE_ID
        if not self.MIN_PROBE_ID <= query_id <= self.MAX_GRAPHICS_ID:
            raise ValueError(i18n.get(Message.VT_GRAPHICS_QUERY_ID))
        self._started = True
        self._appearance_epoch += 1
        self._cols, self._rows = cols, rows
        self._layout_size = cols, rows
        self._probe_grid = cols, rows
        self._query_id = query_id
        return (
            f"{ESC}P$q q{ST}{ESC}[16t{ESC}[14t{ESC}[?2026$p{ESC}[?5522$p{ESC}[?2031$p{ESC}[?996n{ESC}[?u{ESC}[>q"
            + "".join(f"{ESC}]{number};?{ST}" for number in self.COLOR_NAMES)
            + f"{ESC}]21;cursor=?{ST}"
            + "".join(f"{ESC}]4;{number};?{ST}" for number in range(PALETTE_SIZE))
            + f"{ESC}_Ga=q,i={query_id},s=1,v=1,f=24,t=d;AAAA{ST}"
            + "".join(f"{ESC}[?{mode}$p" for mode in self.RESTORE_MODES if mode != 2026)
            + self._query_attributes()
            + f"{ESC}[c"
        )

    def change_modes(self, *modes: int) -> None:
        """Record only modes written by this connection's outer renderer."""
        self._changed_modes.update(modes)

    def restore_modes(self, *, before_screen=False) -> str:
        """Restore observed values after PTK cleanup; never guess missing replies."""
        # DECOM writes reposition the cursor. Restore it on the alternate
        # screen before 1049 restores the primary cursor, never afterwards.
        modes = {mode for mode in self._changed_modes if (mode == 6) == before_screen}
        self._changed_modes.difference_update(modes)
        # Tracking modes are mutually exclusive on hosts such as Windows
        # Terminal: resetting another mode can cancel the active one. Apply
        # all observed off values before restoring any observed on value.
        return "".join(
            f"{ESC}[?{mode}{'h' if value == 1 else 'l'}"
            for value in (2, 1)
            for mode in self.RESTORE_MODES
            if mode in modes and self._mode_values.get(mode) == value
        )

    def begin_mouse_release(self) -> str:
        """Order a single shutdown acknowledgement after tracking has been disabled."""
        if self._closed and self._mode_values.get(self.MOTION_MODE) in (1, 2):
            self.mouse_release_pending = True
            return f"{ESC}[?{self.MOTION_MODE}$p"
        return ""

    def enter_modes(self) -> str:
        """Normalize inherited input capture and origin for the owned screen."""
        modes = (6, *self.MOUSE_MODES)
        self.change_modes(*modes)
        return "".join(f"{ESC}[?{mode}l" for mode in modes)

    def change_attributes(self) -> None:
        """Record renderer use of SGR separately from the native screen state."""
        self._attributes_changed = True

    def restore_attributes(self) -> str:
        """Restore a concrete initial SGR report after the final PTK reset."""
        changed, self._attributes_changed = self._attributes_changed, False
        if changed and self._initial_attributes is not None:
            return f"{ESC}[{self._initial_attributes}m"
        return ""

    def _geometry(self) -> tuple[int, int]:
        if self._cell is not None:
            return self._cell
        if self._pixels is not None:
            width, height = self._pixels
            if width % self._cols == 0 and height % self._rows == 0:
                cell = width // self._cols, height // self._rows
                if all(0 < value <= self.MAX_CELL_PIXELS for value in cell):
                    return cell
        return 0, 0

    def finish_probe(self) -> HostProfile:
        """Freeze probe results under host policy; late replies cannot upgrade them."""
        if not self._closed:
            color_depth = self._depth_probe.depth if self._depth_probe else None
            if self._restore_only:
                self.profile = HostProfile(
                    initial_cursor_style=self._initial_cursor_style,
                    color_depth=color_depth,
                )
                self._closed = True
                self.ready.set()
                return self.profile
            self._calibrate_geometry()
            adapter = self._adapter
            if adapter.extended_cursor and not self._cursor_extended_seen:
                # OSC12 alone cannot distinguish a dynamic Kitty cursor from
                # fixed black. Do not authorize a lossy change/restore pair.
                self._appearance = replace(self._appearance, cursor=None)
            width, height = self._geometry()
            pixels = self._pixels or (0, 0)
            enabled = self._feature_values(width, height)
            self.profile = HostProfile(
                kitty_graphics=enabled["kitty_graphics"],
                sixel_graphics=enabled["sixel_graphics"],
                desktop_notifications=enabled["desktop_notifications"],
                hyperlinks=enabled["hyperlinks"],
                synchronized_output=enabled["synchronized_output"],
                cell_width=width,
                cell_height=height,
                pixel_width=pixels[0],
                pixel_height=pixels[1],
                enhanced_keyboard=enabled["enhanced_keyboard"],
                extended_styles=enabled["extended_styles"],
                pixel_mouse=enabled["pixel_mouse"],
                clipboard_protocol=enabled["clipboard_protocol"],
                image_protocol=self._image_protocol(width, height),
                appearance=self._appearance,
                initial_appearance=self._appearance,
                scheme_notifications=enabled["scheme_notifications"],
                cursor_color_control=enabled["cursor_color"],
                overline=enabled["overline"],
                initial_cursor_style=self._initial_cursor_style,
                color_depth=color_depth,
            )
            self._closed = True
            self._appearance_epoch += 1
            self.graphics_reason = self._graphics_status()
            self.ready.set()
        return self.profile

    def _calibrate_geometry(self):
        """Apply host geometry policy only to independent same-grid reports."""
        if (
            not self._probe_grid_valid
            or self._kernel_size is None
            or self._reported_cell is None
            or self._reported_pixels is None
            or self._kernel_size[:2] != self._probe_grid
        ):
            return
        inset = self._adapter.kernel_inset(
            self._kernel_size, self._reported_cell, self._reported_pixels
        )
        if inset is None:
            return
        self._kernel_inset = inset
        self._cell, self._pixels = self._reported_cell, self._reported_pixels

    def _image_protocol(self, width, height):
        if not width or not height or self._cancelled:
            return None
        enabled = self._feature_values(width, height)
        return (
            "iterm"
            if enabled["iterm_graphics"]
            else "kitty"
            if enabled["kitty_graphics"]
            else "sixel"
            if enabled["sixel_graphics"]
            else None
        )

    def _graphics_status(self, *, resized=False) -> str:
        """Explain selection failure without treating disabled policy as host rejection."""
        if self._cancelled:
            return GRAPHICS_CANCELLED
        if not self._closed:
            return GRAPHICS_PENDING
        if self.profile.image_protocol:
            return ""
        names = ("kitty_graphics", "iterm_graphics", "sixel_graphics")
        if all(self.features[name] == "off" for name in names):
            return GRAPHICS_DISABLED
        support = self._support()
        if any(resolve(self.features[name], support.get(name)) for name in names):
            if self.profile.has_cell_size:
                return self.REASON_MODE_STATE
            return GRAPHICS_RESIZED if resized else GRAPHICS_NO_SIZE
        return self.GRAPHICS_REJECTED

    def cancel_probe(self) -> HostProfile:
        """Close negotiation with no optional capabilities, consuming late replies."""
        if not self._closed:
            self._closed = True
            self._cancelled = True
            self._adapter = UNKNOWN
            self._appearance_epoch += 1
            self.profile = HostProfile()
            self.graphics_reason = GRAPHICS_CANCELLED
            self.ready.set()
        return self.profile

    def _changed(self) -> None:
        if self._depth_probe is not None and not self._depth_probe.complete:
            return
        if self._restore_only:
            if (
                all(mode in self._mode_values for mode in self.SCREEN_MODES)
                and self._attributes_seen
                and self._cursor_style_seen
            ):
                self.ready.set()
            return
        if (
            self._kitty_seen
            and self._sixel_support is not None
            and self._sync_seen
            and self._version_seen
            and self._clipboard_seen
            and self._keyboard_flags is not None
            and self._scheme_mode is not None
            and (self._scheme_seen or self._scheme_mode in (0, 4))
            and len(self._color_seen) == PALETTE_SIZE + len(self.COLOR_NAMES)
            and (self._cell or self._pixels)
            and all(mode in self._mode_values for mode in self.RESTORE_MODES)
            and self._attributes_seen
            and self._cursor_style_seen
        ):
            self.ready.set()

    def enter_metadata_modes(self) -> str:
        """Enable observed scheme notifications for the active presentation."""
        if not self._closed or self._cancelled:
            return ""
        first = not self._metadata_active
        self._metadata_active = True
        if first:
            self._appearance_epoch += 1
        sequences = []
        if self.profile.sixel_graphics and 80 not in self._changed_modes:
            self.change_modes(80)
            sequences.append(f"{ESC}[?80h")
        if first and self.profile.scheme_notifications and self._scheme_mode == 2:
            sequences.append(f"{ESC}[?2031h")
            self._theme_enabled = True
        return "".join(sequences)

    def notification(self, value) -> str:
        """Authorize a normalized notification for the single presentation writer."""
        if self._closed and not self._cancelled and self.profile.desktop_notifications:
            return value.osc9()
        return ""

    def leave_metadata_modes(self) -> str:
        """Restore only state this broker changed, once, without terminal queries."""
        self._metadata_active = False
        self._appearance_epoch += 1
        sequences = []
        if self._theme_enabled:
            sequences.append(f"{ESC}[?2031l")
            self._theme_enabled = False
        return "".join(sequences)

    def restore_cursor(self) -> str:
        """Encode a saved concrete or explicitly dynamic cursor, if observed."""
        original = self.profile.initial_appearance
        if original.cursor_dynamic is True:
            return f"{ESC}]21;cursor={ST}"
        if original.cursor is not None:
            red, green, blue = original.cursor
            return f"{ESC}]12;rgb:{red:02x}/{green:02x}/{blue:02x}{ST}"
        return ""

    def restore_cursor_shape(self) -> str:
        """Restore the concrete session-start observation, not an inferred default."""
        original = self.profile.initial_cursor_style
        return f"{ESC}[{original.code} q" if original is not None else ""

    def set_cursor_color(self, color: tuple[int, int, int]) -> str:
        """Normalize an explicitly requested cursor color only with safe restoration."""
        if len(color) != 3 or any(
            type(value) is not int or not 0 <= value <= 255 for value in color
        ):
            raise ValueError(i18n.get(Message.VT_CURSOR_RGB))
        if not self.profile.cursor_color_control:
            return ""
        red, green, blue = color
        return f"{ESC}]12;rgb:{red:02x}/{green:02x}/{blue:02x}{ST}"

    def _appearance_reply(self, route: _AppearanceRoute) -> None:
        """Apply an observed initial reply or subscribed change at queue dispatch."""
        if (
            not self._started
            or route.epoch != self._appearance_epoch
            or self._cancelled
        ):
            return
        sequence = route.sequence
        scheme = self.SCHEME_REPORT.fullmatch(sequence)
        if scheme is not None:
            value = self.SCHEMES[int(scheme[1])]
            if not self._closed:
                if not self._scheme_seen:
                    self._scheme_seen = True
                    self._appearance = replace(self._appearance, scheme=value)
                    self._changed()
            elif self._metadata_active and self.profile.scheme_notifications:
                # A palette-change notification reports only dark/light, not
                # new RGB values. Retire the initial colors rather than reuse
                # them as current facts or issue ambiguous repeated queries.
                appearance = HostAppearance(scheme=value)
                self.profile = replace(self.profile, appearance=appearance)
                if self.on_appearance is not None:
                    self.on_appearance(appearance)
            return
        if not self._started or self._closed:
            return
        ending = 2 if sequence.endswith(ST) else 1 if sequence.endswith("\x07") else 0
        if not ending:
            return
        parts = sequence[2:-ending].split(";")
        if parts[0] == "21":
            # Kitty's OSC12 response uses black for a dynamic cursor; OSC21
            # distinguishes that state from an explicitly configured black.
            if self._cursor_extended_seen or len(parts) != 2:
                return
            key, equal, value = parts[1].partition("=")
            color = _rgb(value)
            if key != "cursor" or not equal or (value and color is None):
                return
            self._cursor_extended_seen = True
            self._color_seen.add("cursor")
            self._appearance = replace(
                self._appearance, cursor=color, cursor_dynamic=not value
            )
            self._changed()
            return
        updates = []
        if parts[0] == "4" and len(parts) >= 3 and len(parts) % 2 == 1:
            for offset in range(1, len(parts), 2):
                index = parts[offset]
                if not index.isascii() or not index.isdecimal() or len(index) > 3:
                    return
                index = int(index)
                color = _rgb(parts[offset + 1])
                if index >= PALETTE_SIZE or color is None:
                    return
                updates.append((index, color))
        elif parts[0] in ("10", "11", "12"):
            first = int(parts[0])
            for offset, spec in enumerate(parts[1:]):
                number, color = first + offset, _rgb(spec)
                if number not in self.COLOR_NAMES or color is None:
                    return
                updates.append((self.COLOR_NAMES[number], color))
        else:
            return
        palette = list(self._appearance.palette)
        values = {}
        for key, color in updates:
            if key == "cursor" and self._cursor_extended_seen:
                continue
            if key in self._color_seen:
                continue
            self._color_seen.add(key)
            if isinstance(key, int):
                palette[key] = color
            else:
                values[key] = color
        self._appearance = replace(self._appearance, palette=tuple(palette), **values)
        self._changed()

    def enter_input_modes(self, *, mouse=True) -> str:
        """Reconcile selected input modes after PTK enables its cell mouse mode.

        Call after the first PTK render and after a geometry profile change.
        Pixel mode supersedes cell encoding; loss of measured geometry restores
        PTK's cell format instead of guessing a pixel-to-cell scale.
        """
        if not self._closed:
            return ""
        sequences = []
        if self.profile.enhanced_keyboard and not self.input_modes_active:
            self.input_modes_active = True
            sequences.append(f"{ESC}[>{self.KEYBOARD_FLAGS}u")
        sequences.append(self.mouse_mode(mouse))
        return "".join(sequences)

    def mouse_mode(self, enabled: bool) -> str:
        """Match pixel encoding to PTK's actual capture without enabling tracking."""
        if not self._closed:
            return ""
        if enabled and self.profile.pixel_mouse and not self.pixel_mouse_active:
            self.pixel_mouse_active = True
            self.change_modes(1016)
            return f"{ESC}[?1016h"
        if (not enabled or not self.profile.pixel_mouse) and self.pixel_mouse_active:
            self.pixel_mouse_active = False
            return f"{ESC}[?1016l" + (f"{ESC}[?1006h" if enabled else "")
        return ""

    def leave_input_modes(self) -> str:
        """Undo exactly the input modes this broker enabled before leaving the screen."""
        sequences = []
        if self.pixel_mouse_active:
            self.pixel_mouse_active = False
            sequences.append(f"{ESC}[?1016l")
        if self.input_modes_active:
            self.input_modes_active = False
            sequences.append(f"{ESC}[<u")
        return "".join(sequences)

    def handle_key(self, keypress: KeyPress) -> bool:
        """Dispatch metadata at PTK key-processing time, preserving reader order.

        Call only from the terminal-focused handler. Popup editing consumes the
        ordinary PTK key meaning. Several legacy key events may represent one
        Alt key; their shared route prevents duplicate terminal delivery.
        """
        route = getattr(keypress, "_host_key_route", None)
        if route is None:
            return False
        if route.handled is None:
            route.handled = bool(self.on_key is not None and self.on_key(route.event))
        return route.handled

    def key_consumed(self, keypress: KeyPress) -> bool:
        """Whether a sibling fallback token already consumed this physical event."""
        route = getattr(keypress, "_host_key_route", None)
        return route is not None and route.handled is True

    def consume_key(self, keypress: KeyPress) -> bool:
        """Claim a complete structured event for its current UI handler.

        PTK may express one Alt key as Escape plus another key. Mark the shared
        route at the exact handler key-sequence boundary, so a focus change
        cannot deliver the remaining token to the new input consumer. Ordinary
        input has no shared physical identity and is left untouched.
        """
        route = getattr(keypress, "_host_key_route", None)
        if route is None:
            return False
        route.handled = True
        return True

    def handle_control(self, keypress: KeyPress) -> bool:
        """Dispatch queued focus and clipboard controls regardless of UI focus.

        Both the terminal handler and the application's global Ignore binding
        call this method. Structured key releases also use Ignore, but have no
        control metadata and remain owned by the focused key handler.
        """
        appearance = getattr(keypress, "_host_appearance_route", None)
        if appearance is not None:
            if not appearance.handled:
                appearance.handled = True
                self._appearance_reply(appearance)
            return True
        clipboard = getattr(keypress, "_host_clipboard_route", None)
        if clipboard is not None:
            if not clipboard.handled:
                clipboard.handled = True
                if self.on_clipboard_reply is not None:
                    self.on_clipboard_reply(clipboard.sequence)
            return True
        route = getattr(keypress, "_host_focus_route", None)
        if route is None:
            return False
        if not route.handled:
            route.handled = True
            if self.on_focus is not None:
                self.on_focus(route.focused)
        return True

    @property
    def geometry_pending(self) -> bool:
        """Whether a requested pair is queued, in flight, or being retired."""
        return self._geometry_target is not None or self._geometry_request is not None

    def request_geometry(self) -> bool:
        """Request the latest measured grid, coalescing changes without overlap.

        A missing initial response or a failed refresh prohibits new queries in
        this session. Kernel geometry can still provide independent evidence.
        The caller decides whether to pause child output while this is pending.
        """
        if (
            not self._started
            or not self._closed
            or self._cancelled
            or self._restore_only
            or self._geometry_blocked
            or self._initial_geometry_seen != {4, 6}
            or self._layout_size is None
        ):
            return False
        choices = self._feature_choices()
        if not any(choices[name] for name in self.GEOMETRY_FEATURES):
            return False
        if not self.profile.has_cell_size:
            self._geometry_target = (self._geometry_epoch, self._layout_size)
        return self.geometry_pending

    def take_geometry_query(self) -> tuple[int, str] | None:
        """Arm exactly one pair when the sole writer is ready to emit it.

        The serial identifies this local timer, not a terminal response. Do not
        call this early merely to put the returned bytes in another work queue.
        """
        if (
            self._geometry_target is None
            or self._geometry_request is not None
            or self._geometry_blocked
        ):
            return None
        epoch, grid = self._geometry_target
        self._geometry_serial += 1
        self._geometry_request = _GeometryQuery(self._geometry_serial, epoch, grid)
        return self._geometry_serial, self.GEOMETRY_QUERY

    def expire_geometry(self, serial: int) -> None:
        """Fail closed on an unresolved pair; old timer callbacks are inert."""
        if (
            self._geometry_request is not None
            and self._geometry_request.serial == serial
        ):
            self.cancel_geometry()

    def cancel_geometry(self) -> None:
        """Retire outstanding replies and prohibit another identity-free query."""
        changed = self.geometry_pending
        self._geometry_blocked = True
        self._geometry_target = self._geometry_request = None
        if changed and self.on_geometry is not None:
            self.on_geometry()

    def _geometry_reply(self, kind, width, height):
        request = self._geometry_request
        if request is None:
            return
        if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION) or (
            kind == 6
            and (width > self.MAX_CELL_PIXELS or height > self.MAX_CELL_PIXELS)
        ):
            self.cancel_geometry()
            return
        value = width, height
        previous = request.cell if kind == 6 else request.pixels
        if previous is not None:
            if previous != value:
                self.cancel_geometry()
            return
        if kind == 6:
            request.cell = value
        else:
            request.pixels = value
        if request.cell is None or request.pixels is None:
            return
        self._geometry_request = None
        current = (self._geometry_epoch, self._layout_size)
        if (request.epoch, request.grid) != current:
            # Both old replies were consumed. A later render may issue the
            # latest queued pair; never arm it while parsing this input batch.
            if self.on_geometry is not None:
                self.on_geometry()
            return
        cols, rows = request.grid
        width, height = request.cell
        if request.pixels != (cols * width, rows * height):
            self.cancel_geometry()
            return
        self._geometry_target = None
        self._apply_geometry(request.cell, request.pixels)
        if self.on_geometry is not None:
            self.on_geometry()

    def _apply_geometry(self, cell, pixels):
        """Apply measured geometry without changing capability observations."""
        self._cell, self._pixels = cell, pixels
        self._accept_size_reports = False
        enabled = self._feature_values(*cell)
        self.profile = replace(
            self.profile,
            kitty_graphics=self._closed and enabled["kitty_graphics"],
            sixel_graphics=self._closed and enabled["sixel_graphics"],
            cell_width=cell[0],
            cell_height=cell[1],
            pixel_width=pixels[0],
            pixel_height=pixels[1],
            pixel_mouse=self._closed and enabled["pixel_mouse"],
            image_protocol=self._image_protocol(*cell) if self._closed else None,
        )
        if self._closed:
            self.graphics_reason = self._graphics_status()
        self._changed()

    def refresh_geometry(
        self, cols: int, rows: int, pixel_width: int, pixel_height: int
    ) -> HostProfile:
        """Accept actual kernel winsize data, never ratios guessed from prior frames.

        Missing or non-divisible startup pixels retain the one-shot query fallback.
        Later geometry changes without usable pixels revoke presentation. The
        caller may explicitly request a serialized geometry refresh afterwards;
        capability acknowledgements are never re-queried or changed here.
        """
        if not 0 < cols <= MAX_DIMENSION or not 0 < rows <= MAX_DIMENSION:
            raise ValueError(i18n.get(Message.VT_HOST_DIMENSIONS))
        if (
            not 0 <= pixel_width <= MAX_DIMENSION
            or not 0 <= pixel_height <= MAX_DIMENSION
        ):
            raise ValueError(i18n.get(Message.VT_HOST_PIXELS))
        measured = cols, rows, pixel_width, pixel_height
        if measured == self._kernel_size:
            return self.profile
        changed = self._layout_size is not None and self._layout_size != (cols, rows)
        lost_pixels = self._kernel_size is not None and any(self._kernel_size[2:])
        self._kernel_size = measured
        self._layout_size = cols, rows
        self._cols, self._rows = cols, rows
        self._geometry_epoch += 1
        if self.geometry_pending:
            self._geometry_target = (self._geometry_epoch, self._layout_size)
        if changed:
            self._accept_size_reports = False
            if self._started and not self._closed:
                self._probe_grid_valid = False
        if self._kernel_inset is not None:
            pixel_width -= self._kernel_inset[0]
            pixel_height -= self._kernel_inset[1]
        valid = (
            pixel_width > 0
            and pixel_height > 0
            and pixel_width % cols == 0
            and pixel_height % rows == 0
            and pixel_width // cols <= self.MAX_CELL_PIXELS
            and pixel_height // rows <= self.MAX_CELL_PIXELS
        )
        if valid:
            self._geometry_target = None
            self._apply_geometry(
                (pixel_width // cols, pixel_height // rows), (pixel_width, pixel_height)
            )
        elif not self._closed and not changed and self._accept_size_reports:
            # Some real hosts include a window border in TIOCGWINSZ pixels.
            # Unusable startup pixels cannot revoke the independent, explicit
            # cell/text-area reports already requested for this generation.
            pass
        elif changed or lost_pixels or pixel_width or pixel_height:
            self._cell = self._pixels = None
            self._accept_size_reports = False
            self.profile = replace(
                self.profile,
                kitty_graphics=False,
                sixel_graphics=False,
                cell_width=0,
                cell_height=0,
                pixel_width=0,
                pixel_height=0,
                pixel_mouse=False,
                image_protocol=None,
            )
            self.graphics_reason = self._graphics_status(resized=True)
        return self.profile

    def control(self, sequence: str) -> bool:
        """Consume a complete control reply; return false only for keyboard traffic."""
        negotiating_depth = (
            self._started and not self._closed and self._depth_probe is not None
        )
        if negotiating_depth and (
            report := self._depth_probe.REPORT.fullmatch(sequence)
        ):
            self._depth_probe.capability(report)
            self._changed()
            return True
        attributes = self.ATTRIBUTES_REPLY.fullmatch(sequence)
        if attributes:
            if self._started and not self._closed and self._sixel_support is None:
                # DA1's first parameter identifies the terminal class. Only
                # following attributes advertise optional capabilities.
                self._sixel_support = "4" in attributes[1].split(";")[1:]
                self._changed()
            return True
        keyboard = self.KEYBOARD_REPORT.fullmatch(sequence)
        if keyboard:
            flags = int(keyboard[1])
            if (
                self._started
                and not self._closed
                and self._keyboard_flags is None
                and flags <= 31
            ):
                self._keyboard_flags = flags
                self._changed()
            return True
        attributes = self.ATTRIBUTES_REPORT.fullmatch(sequence)
        if attributes:
            if self._started and not self._closed and not self._attributes_seen:
                self._attributes_seen = True
                if attributes[1] == "1":
                    self._initial_attributes = attributes[2] or "0"
                    if negotiating_depth:
                        self._depth_probe.sample(self._initial_attributes)
                self._changed()
            elif negotiating_depth and attributes[1] == "1":
                self._depth_probe.observe_sample(attributes[2])
                self._changed()
            return True
        mode = self.MODE_REPORT.fullmatch(sequence)
        if mode and int(mode[1]) in self.RESTORE_MODES:
            number, value = map(int, mode.groups())
            if (
                self.mouse_release_pending
                and number == self.MOTION_MODE
                and value in (2, 4)
            ):
                self.mouse_release_pending = False
            if self._started and not self._closed and number not in self._mode_values:
                self._mode_values[number] = value
                self._changed()
            # 2026 also supplies the existing presentation capability below.
            if number != 2026:
                return True
        cursor_style = self.CURSOR_STYLE_REPORT.fullmatch(sequence)
        if cursor_style:
            if self._started and not self._closed and not self._cursor_style_seen:
                self._cursor_style_seen = True
                status, value = cursor_style.groups()
                if status == "1" and value in ("1", "2", "3", "4", "5", "6"):
                    self._initial_cursor_style = HostCursorStyle(int(value))
                self._changed()
            return True
        if sequence.startswith((ESC + "]52;", ESC + "]5522;")):
            # HostParser queues replies that may produce child input. Direct
            # control ingestion must not bypass that ordered dispatch boundary.
            return True
        if sequence in (ESC + "[I", ESC + "[O"):
            # The parser queues focus before reaching this method. Direct
            # control ingestion must never reorder focus ahead of queued keys.
            return True
        if sequence.startswith(ESC + "_"):
            body = sequence[2:-2] if sequence.endswith(ST) else ""
            match = self.GRAPHICS_REPLY.fullmatch(body)
            if match:
                identity, message = int(match[1]), match[2]
                if 0 < identity <= self.MAX_GRAPHICS_ID:
                    if identity == self._query_id:
                        if not self._closed and not self._kitty_seen:
                            self._kitty_seen, self._kitty_ok = True, message == "OK"
                            self._changed()
                    elif message != "OK" and self.on_graphics_error is not None:
                        self.on_graphics_error(identity, message)
            return True
        if sequence.startswith(ESC + "P>|"):
            if self._started and not self._closed and not self._version_seen:
                self._version_seen = True
                self._adapter = identify_host(sequence)
                self._changed()
            return True
        if sequence.startswith((ESC + "]", ESC + "P", ESC + "^")):
            # OSC/DCS/PM are terminal control traffic. Unrequested replies have
            # no active owner and are discarded rather than becoming keystrokes.
            return True
        if not sequence.startswith(ESC + "["):
            return False
        size = self.SIZE_REPORT.fullmatch(sequence)
        if size:
            kind, height, width = map(int, size.groups())
            if self._started and not self._closed:
                self._initial_geometry_seen.add(kind)
            if self._closed:
                self._geometry_reply(kind, width, height)
                return True
            if self._started and not self._closed and self._probe_grid_valid:
                kind, height, width = map(int, size.groups())
                if 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION:
                    if (
                        kind == 6
                        and self._reported_cell is None
                        and width <= self.MAX_CELL_PIXELS
                        and height <= self.MAX_CELL_PIXELS
                    ):
                        self._reported_cell = width, height
                    elif kind == 4 and self._reported_pixels is None:
                        self._reported_pixels = width, height
            if self._started and not self._closed and self._accept_size_reports:
                kind, height, width = map(int, size.groups())
                if 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION:
                    if kind == 6 and self._cell is None:
                        if (
                            width <= self.MAX_CELL_PIXELS
                            and height <= self.MAX_CELL_PIXELS
                        ):
                            self._cell = width, height
                    elif kind == 4 and self._pixels is None:
                        self._pixels = width, height
                    self._changed()
            return True
        sync = self.SYNC_REPORT.fullmatch(sequence)
        if sync:
            if self._started and not self._closed and not self._sync_seen:
                self._sync_seen = True
                self._sync_ok = int(sync[1]) in (1, 2)
                self._changed()
            return True
        clipboard = self.CLIPBOARD_REPORT.fullmatch(sequence)
        if clipboard:
            if self._started and not self._closed and not self._clipboard_seen:
                self._clipboard_seen = True
                self._clipboard_ok = int(clipboard[1]) in (1, 2, 3)
                self._changed()
            return True
        scheme_mode = self.SCHEME_MODE_REPORT.fullmatch(sequence)
        if scheme_mode:
            if self._started and not self._closed and self._scheme_mode is None:
                self._scheme_mode = int(scheme_mode[1])
                self._changed()
            return True
        # Retired/unsolicited graphics-capacity replies still are control
        # traffic. They cannot enable a backend or become keyboard text.
        if sequence.startswith((ESC + "[?1;", ESC + "[?2;")) and sequence.endswith("S"):
            return True
        # Preserve CPR for PTK's own cursor-position handling. Mouse and known
        # legacy key sequences also proceed to its normal parser unchanged.
        if sequence.endswith("R"):
            return False
        if sequence[-1] in ("c", "n", "t", "y"):
            return True
        # The parser handles the negotiated keyboard subset first. Unsupported
        # reports must not degrade into parameter text in the shell buffer.
        if sequence.endswith("u"):
            return True
        return False


class HostParser:
    """Frame host controls before the unchanged PTK key/paste parser.

    Complete paste payloads bypass control classification. Partial host replies
    survive PTK's Escape flush, so delayed chunks cannot leak into shell input.
    Oversized controls are discarded through their terminator with bounded RAM.
    """

    MAX_CONTROL = 64 * 1024
    CSI_FINAL = re.compile(r"[\x40-\x7e]")
    CSI_INPUT_BREAK = re.compile(r"[\x00-\x1f\x7f]")
    CONTROL_START = re.compile(r"[\x1b\x90\x9b\x9d\x9e\x9f]")
    C1_PREFIXES = {"\x90": "P", "\x9b": "[", "\x9d": "]", "\x9e": "^", "\x9f": "_"}
    # Conventional CSI function/navigation keys do not require enhanced-keyboard
    # negotiation. R is deliberately absent: legacy modified F3 can collide with
    # CPR, and a delayed cursor report must never become a new key event.
    LEGACY_KEY_REPORT = re.compile(r"\x1b\[[0-9]{0,3}(?:;[1-8])?[~ABCDFHPQS]\Z")
    PIXEL_MOUSE_REPORT = re.compile(
        r"\x1b\[<([0-9]{1,3});([0-9]{1,5});([0-9]{1,5})([Mm])\Z"
    )

    def __init__(self, parser, broker: HostBroker):
        """Install framing ahead of a single existing PTK key parser."""
        self.parser, self.broker = parser, broker
        self._return_to = None
        self._pending = ""
        self._retired_control = False
        self._paste = False
        self._discard = None
        self._discard_escape = False

    def reset(self, request: bool = False) -> None:
        """Reset both parsers when PTK explicitly resets its input state."""
        self._pending = ""
        self._retired_control = False
        self._paste = False
        self._discard = None
        self._discard_escape = False
        self.parser.reset(request=request)
        self._return_parser()

    def retire_mouse(self):
        """Only an unfinished control from the old screen can be a stale report."""
        self._retired_control = (
            bool(self._pending) and not self._paste and self._discard is None
        )

    def return_when_complete(self, source):
        """Return the borrowed parser after the old surface's last control."""
        self.retire_mouse()
        if self._retired_control or self._paste or self._discard:
            self._return_to = source
        else:
            source.vt100_parser = self.parser

    def _return_parser(self):
        if self._return_to is not None and not (
            self._retired_control or self._paste or self._discard
        ):
            self._return_to.vt100_parser = self.parser
            self._return_to = None
            tail, self._pending = self._pending, ""
            if tail:
                self.parser.feed(tail)

    def flush(self) -> None:
        """Flush a lone Escape, but never reclassify partial controls as typing."""
        if (
            self._pending == ESC
            and not self._paste
            and self._discard is None
            and not self.broker.input_modes_active
        ):
            self.parser.feed(self._pending)
            self._pending = ""
            self._retired_control = False
        self.parser.flush()
        self._return_parser()

    def _key(self, sequence: str) -> bool:
        if sequence.startswith(ESC + "[?") and sequence.endswith("u"):
            # Status replies describe a protocol; they are never key events,
            # even while its input encoding is active or the reply is invalid.
            return False
        mapped = self.broker._keys.get(sequence)
        if mapped is not None:
            self._emit_key(mapped)
            return True
        enhanced = self.broker.input_modes_active
        if not enhanced and self.LEGACY_KEY_REPORT.fullmatch(sequence) is None:
            # Unnegotiated extended reports remain control traffic, never
            # manufactured keystrokes or their decimal parameter text.
            unsupported = sequence.endswith("u") or (
                sequence.startswith(ESC + "[") and ":" in sequence
            )
            self.broker.dropped_controls += int(unsupported)
            return unsupported
        event = decode_key(sequence)
        if event is None:
            # Unknown event-type reports must not fall back to PTK's unmatched
            # escape handling, which would insert their decimal parameters.
            unsupported = enhanced and (
                sequence.endswith("u")
                or (sequence.startswith(ESC + "[") and ":" in sequence)
                or KEY_REPORT.fullmatch(sequence) is not None
            )
            self.broker.dropped_controls += int(unsupported)
            return unsupported
        event = replace(event, release_events=enhanced)
        self._emit_key(event)
        return True

    def _emit_key(self, event: StructuredKey) -> None:
        """Reuse one route for native forwarding and PTK popup fallback."""
        self.parser.flush()
        route = _KeyRoute(event)
        for keypress in _legacy_keys(event):
            keypress.structured_key = event
            keypress._host_key_route = route
            self.parser.feed_key_callback(keypress)

    def _mouse(self, sequence: str) -> bool:
        if not self.broker.pixel_mouse_active or not sequence.startswith(ESC + "[<"):
            return False
        match = self.PIXEL_MOUSE_REPORT.fullmatch(sequence)
        profile = self.broker.profile
        if match is None or not profile.has_cell_size:
            return True
        code, x, y = map(int, match.groups()[:3])
        if not 0 < x <= MAX_DIMENSION or not 0 < y <= MAX_DIMENSION:
            return True
        cell_x = (x - 1) // profile.cell_width + 1
        cell_y = (y - 1) // profile.cell_height + 1
        cell_report = f"{ESC}[<{code};{cell_x};{cell_y}{match[4]}"
        if decode_mouse(cell_report) is None:
            return True
        self.parser.flush()
        keypress = KeyPress(Keys.Vt100MouseEvent, cell_report)
        keypress.pixel_position = (x, y)
        self.parser.feed_key_callback(keypress)
        return True

    def _focus(self, sequence: str) -> bool:
        if sequence not in (ESC + "[I", ESC + "[O"):
            return False
        self.parser.flush()
        keypress = KeyPress(Keys.Ignore, "")
        keypress._host_focus_route = _FocusRoute(sequence.endswith("I"))
        self.parser.feed_key_callback(keypress)
        return True

    def _clipboard(self, sequence: str) -> bool:
        if not sequence.startswith((ESC + "]52;", ESC + "]5522;")):
            return False
        self.parser.flush()
        keypress = KeyPress(Keys.Ignore, "")
        keypress._host_clipboard_route = _ClipboardRoute(sequence)
        self.parser.feed_key_callback(keypress)
        return True

    def _appearance(self, sequence: str) -> bool:
        if not sequence.startswith(
            (
                ESC + "]4;",
                ESC + "]10;",
                ESC + "]11;",
                ESC + "]12;",
                ESC + "]21;",
                ESC + "[?997;",
            )
        ):
            return False
        self.parser.flush()
        keypress = KeyPress(Keys.Ignore, "")
        keypress._host_appearance_route = _AppearanceRoute(
            sequence, self.broker._appearance_epoch
        )
        self.parser.feed_key_callback(keypress)
        return True

    def _drain_discard(self) -> bool:
        for index, character in enumerate(self._pending):
            if self._discard == "csi" and self.CSI_INPUT_BREAK.fullmatch(character):
                # Even an oversized/incomplete report cannot retain real
                # interrupt, EOF or submit keys. CAN/SUB cancel silently;
                # other C0 input resumes through the normal PTK key parser.
                self._pending = self._pending[index + int(character in "\x18\x1a") :]
                self._discard = None
                self._discard_escape = False
                return True
            ended = (
                bool(self.CSI_FINAL.fullmatch(character))
                if self._discard == "csi"
                else (self._discard_escape and character == "\\")
                or character == "\x9c"
                or (self._discard == "osc" and character == "\x07")
            )
            self._discard_escape = character == ESC
            if ended:
                self._pending = self._pending[index + 1 :]
                self._discard = None
                self._discard_escape = False
                return True
        self._pending = ""
        return False

    def feed(self, data: str) -> None:
        """Accept arbitrary reader fragments while preserving key/paste order."""
        if data:
            self.broker.seal_keys()
        self._pending += data
        while self._pending and (
            self._return_to is None
            or self._retired_control
            or self._paste
            or self._discard is not None
        ):
            if self._discard is not None:
                if not self._drain_discard():
                    break
                continue
            if self._paste:
                ending = self._pending.find(PASTE_END)
                if ending >= 0:
                    end = ending + len(PASTE_END)
                    self.parser.feed(self._pending[:end])
                    self._pending = self._pending[end:]
                    self._paste = False
                    continue
                keep = 0
                for length in range(1, min(len(PASTE_END), len(self._pending) + 1)):
                    if self._pending.endswith(PASTE_END[:length]):
                        keep = length
                take = len(self._pending) - keep
                self.parser.feed(self._pending[:take])
                self._pending = self._pending[take:]
                break
            if self._pending[0] in self.C1_PREFIXES:
                self._pending = (
                    ESC + self.C1_PREFIXES[self._pending[0]] + self._pending[1:]
                )
            if not self._pending.startswith(ESC):
                control = self.CONTROL_START.search(self._pending)
                end = control.start() if control else len(self._pending)
                self.parser.feed(self._pending[:end])
                self._pending = self._pending[end:]
                continue
            if len(self._pending) == 1:
                break
            kind = self._pending[1]
            if kind not in ("[", "]", "P", "_", "^"):
                self._retired_control = False
                if self._return_to is not None:
                    break
            if kind == ESC or kind in self.C1_PREFIXES:
                # A user's Escape can immediately precede a host reply (or a
                # legacy Alt+arrow). Keep its introducer in this framing layer;
                # passing both bytes to PTK would expose the reply tail as text.
                self.parser.feed(ESC)
                self._pending = self._pending[1:]
                continue
            if kind == "[":
                final = self.CSI_FINAL.search(self._pending, 2)
                boundary = self.CSI_INPUT_BREAK.search(self._pending, 2)
                if boundary is not None:
                    if final is None or boundary.start() < final.start():
                        consume = boundary[0] in "\x18\x1a"
                        self._pending = self._pending[boundary.start() + int(consume) :]
                        self._retired_control = False
                        self.broker.dropped_controls += 1
                        continue
                if final is None:
                    if len(self._pending) > self.MAX_CONTROL:
                        self._pending = ""
                        self._discard = "csi"
                        self._retired_control = False
                        self.broker.dropped_controls += 1
                    break
                end = final.end()
                # X10 mouse has three coordinate characters after CSI M;
                # Linux console keys use CSI [ plus their actual final byte.
                if self._pending.startswith(ESC + "[M"):
                    if len(self._pending) < 6:
                        break
                    end = 6
                elif self._pending.startswith(ESC + "[["):
                    if len(self._pending) < 4:
                        break
                    end = 4
            elif kind in ("]", "P", "_", "^"):
                endings = []
                end_st = self._pending.find(ST, 2)
                if end_st >= 0:
                    endings.append(end_st + len(ST))
                end_c1 = self._pending.find("\x9c", 2)
                if end_c1 >= 0:
                    endings.append(end_c1 + 1)
                if kind == "]":
                    end_bel = self._pending.find("\x07", 2)
                    if end_bel >= 0:
                        endings.append(end_bel + 1)
                if not endings:
                    if len(self._pending) > self.MAX_CONTROL:
                        self._discard_escape = self._pending.endswith(ESC)
                        self._pending = ""
                        self._discard = "osc" if kind == "]" else "string"
                        self._retired_control = False
                        self.broker.dropped_controls += 1
                    break
                end = min(endings)
            else:
                self.parser.feed(self._pending[:2])
                self._pending = self._pending[2:]
                continue
            sequence, self._pending = self._pending[:end], self._pending[end:]
            if self._retired_control:
                self._retired_control = False
                if (
                    len(sequence) <= self.MAX_CONTROL
                    and decode_mouse(sequence) is not None
                ):
                    continue
                if self._return_to is not None and sequence != PASTE_START:
                    # The next PTK reader owns keyboard input. Replies to the
                    # retired host, including CPR, must finish with that host.
                    if kind == "[" and len(sequence) <= self.MAX_CONTROL:
                        key = self.parser._get_match(sequence)
                        if key is None:
                            self._key(sequence)
                        elif key not in (Keys.CPRResponse, Keys.Vt100MouseEvent):
                            self.parser.feed(sequence)
                    continue
            if sequence.endswith("\x9c"):
                sequence = sequence[:-1] + ST
            if sequence == PASTE_START:
                self._paste = True
                self.parser.feed(sequence)
            elif len(sequence) > self.MAX_CONTROL:
                self.broker.dropped_controls += 1
            elif sequence.startswith(ESC + "[M"):
                # Coordinates may end with response final letters such as n;
                # their meaning belongs to the complete legacy mouse record.
                if self.parser._get_match(sequence) == Keys.Vt100MouseEvent:
                    self.parser.feed(sequence)
                else:
                    self.broker.dropped_controls += 1
            elif self._focus(sequence):
                continue
            elif len(sequence) <= self.MAX_CONTROL and self._clipboard(sequence):
                continue
            elif len(sequence) <= self.MAX_CONTROL and self._appearance(sequence):
                continue
            elif len(sequence) <= self.MAX_CONTROL and self._mouse(sequence):
                continue
            elif len(sequence) <= self.MAX_CONTROL and self._key(sequence):
                continue
            elif len(sequence) <= self.MAX_CONTROL and not self.broker.control(
                sequence
            ):
                # PTK's unmatched CSI fallback can emit decimal parameters as
                # ordinary typing. Only its exact known key/mouse/CPR match is
                # eligible; unclaimed extension controls remain controls.
                if self.parser._get_match(sequence) is not None:
                    self.parser.feed(sequence)
                else:
                    self.broker.dropped_controls += 1

        self._return_parser()


class HostInput(Input):
    """Wrap a PTK VT input without adding another descriptor reader.

    The parser attribute is the narrow version-pinned PTK extension point.
    Native Windows input has a different contract and is not supported here.
    """

    RELEASE_TIMEOUT = 0.5

    def __init__(self, source: Input, broker: HostBroker):
        """Attach to the version-pinned VT parser without creating an input source."""
        parser = getattr(source, "vt100_parser", None)
        if isinstance(parser, HostParser) and parser._return_to is source:
            # The next ish screen can adopt a split report in place, along with
            # its retirement boundary, without creating a nested reader.
            parser.broker, parser._return_to = broker, None
            self.parser = parser
        elif parser is None or isinstance(parser, HostParser):
            raise TypeError(i18n.get(Message.VT_HOST_INPUT))
        else:
            self.parser = HostParser(parser, broker)
        self.source = source
        source.vt100_parser = self.parser
        self._initial_termios = None
        self._released = False

    def fileno(self):
        """Retain the existing descriptor."""
        return self.source.fileno()

    def typeahead_hash(self):
        """Retain PTK's input identity."""
        return self.source.typeahead_hash()

    def read_keys(self):
        """Read through the sole existing PTK reader."""
        return [] if self._released else self.source.read_keys()

    def flush_keys(self):
        """Use the framed parser for PTK's Escape flush."""
        return self.source.flush_keys()

    def retire_mouse(self):
        """Keep a split report with its old surface until the final byte arrives."""
        self.parser.retire_mouse()

    def finish_mouse_release(self, output):
        """Read through the host's ordered reply without reading the next owner's bytes."""
        broker = self.parser.broker
        reader = getattr(self.source, "stdin_reader", None)
        if reader is None or self.closed or not os.isatty(self.fileno()):
            return []
        query = broker.begin_mouse_release()
        if not query:
            return []
        keys = []
        decoder = self.parser.parser
        receive_key = decoder.feed_key_callback
        try:
            # Preserve completed keys as PTK typeahead and leave partial keys or paste
            # with the same parser. An Escape flush here would split their next read.
            decoder.feed_key_callback = keys.append
            output.write_raw(query)
            output.flush()
            deadline = time.monotonic() + self.RELEASE_TIMEOUT
            # This is the final synchronous read of the existing PTK reader, while
            # its TTY is still raw. A single OS write can contain the reply and the
            # next shell command. Leave everything after the reply in the kernel.
            fd = self.fileno()
            while broker.mouse_release_pending and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    break
                self.parser.feed(reader.read(count=1))
            self._released = not broker.mouse_release_pending
            return keys
        finally:
            decoder.feed_key_callback = receive_key
            broker.mouse_release_pending = False

    def flush(self):
        """Delegate the input lifecycle operation."""
        return self.source.flush()

    @property
    def closed(self):
        """Expose the original EOF state."""
        return self.source.closed

    @contextmanager
    def raw_mode(self):
        """Retain PTK cleanup and capture the actual initial TTY settings once."""
        if self._initial_termios is None:
            try:
                self._initial_termios = termios.tcgetattr(self.fileno())
            except (OSError, termios.error):
                pass  # Non-TTY inputs retain PTK's original no-op behavior.
        self._released = False
        with self.source.raw_mode():
            yield

    @contextmanager
    def cooked_mode(self):
        """Temporarily restore the saved TTY, not a guessed cooked default."""
        if self._initial_termios is None:
            with self.source.cooked_mode():
                yield
        else:
            current = termios.tcgetattr(self.fileno())
            try:
                termios.tcsetattr(self.fileno(), termios.TCSANOW, self._initial_termios)
                yield
            finally:
                termios.tcsetattr(self.fileno(), termios.TCSANOW, current)

    def attach(self, input_ready_callback):
        """Register the existing reader once."""
        return self.source.attach(input_ready_callback)

    def detach(self):
        """Detach the existing reader."""
        return self.source.detach()

    def close(self):
        """Release the wrapped input and its parser attachment."""
        self.restore_parser()
        self.source.close()

    def restore_parser(self):
        """Release interception without closing a caller-owned PTK input."""
        if self.source.vt100_parser is self.parser:
            self.parser.return_when_complete(self.source)
