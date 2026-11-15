"""Negotiate the outer terminal through the one PTK input reader.

Host responses are control traffic, never shell keystrokes. The optional probe
only selects presentation capabilities; it does not determine shell readiness.
The identity-free size reports are requested once per input session. A closed
probe cannot be reopened or upgraded by a delayed response.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import Keys

from .hosts import UNKNOWN, HostAdapter, identify_host
from .mouse import decode_mouse
from .vt import ALT, CTRL, SHIFT

ESC = "\x1b"
ST = ESC + "\\"
PASTE_START = ESC + "[200~"
PASTE_END = ESC + "[201~"
MAX_CONTROL = 64 * 1024
MAX_DIMENSION = 65_535
MAX_CELL_PIXELS = 4096
MAX_TITLE = 1024
MAX_GRAPHICS_ID = 2**32 - 1
MIN_PROBE_ID = 2**31
CSI_FINAL = re.compile(r"[\x40-\x7e]")
CONTROL_START = re.compile(r"[\x1b\x90\x9b\x9d\x9e\x9f]")
C1_PREFIXES = {"\x90": "P", "\x9b": "[", "\x9d": "]", "\x9e": "^", "\x9f": "_"}
SIZE_REPORT = re.compile(r"\x1b\[([46]);([0-9]{1,5});([0-9]{1,5})t\Z")
SYNC_REPORT = re.compile(r"\x1b\[\?2026;([0-4])\$y\Z")
CLIPBOARD_REPORT = re.compile(r"\x1b\[\?5522;([0-4])\$y\Z")
SCHEME_MODE_REPORT = re.compile(r"\x1b\[\?2031;([0-4])\$y\Z")
SCHEME_REPORT = re.compile(r"\x1b\[\?997;([12])n\Z")
RGB_REPORT = re.compile(
    r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})\Z"
)
PALETTE_SIZE = 256
COLOR_NAMES = {10: "foreground", 11: "background", 12: "cursor"}
SCHEMES = {1: "dark", 2: "light"}
GRAPHICS_REPLY = re.compile(r"Gi=([0-9]{1,10})(?:,p=[0-9]{1,10})?;([^\x00-\x1f]*)\Z")
GRAPHICS_PENDING = "host-probe-pending"
GRAPHICS_REJECTED = "host-did-not-acknowledge-kitty"
GRAPHICS_NO_SIZE = "verified-cell-size-unavailable"
GRAPHICS_RESIZED = "cell-size-unavailable-after-resize"
GRAPHICS_CANCELLED = "host-probe-cancelled"
KEYBOARD_FLAGS = 3  # Disambiguation and event types; no all-key/associated-text claim.
KEY_ACTIONS = {1: "press", 2: "repeat", 3: "release"}
KEY_REPORT = re.compile(r"\x1b\[([0-9:;]*)([u~ABCDFHPQS])\Z")
PIXEL_MOUSE_REPORT = re.compile(
    r"\x1b\[<([0-9]{1,3});([0-9]{1,5});([0-9]{1,5})([Mm])\Z"
)
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
    **{57363 + number: f"f{number}" for number in range(1, 13)},
    # Disambiguation also changes non-text keypad encodings. Preserve their
    # ordinary navigation meaning without claiming native keypad distinction.
    57414: "enter",
    57417: "left",
    57418: "right",
    57419: "up",
    57420: "down",
    57421: "pageup",
    57422: "pagedown",
    57423: "home",
    57424: "end",
    57425: "insert",
    57426: "delete",
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
    **{f"f{number}": Keys(f"f{number}") for number in range(1, 13)},
}
NAMED_TEXT = {"escape": ESC, "enter": "\r", "tab": "\t", "backspace": "\x7f"}
CONTROL_TEXT = {
    " ": "\x00",
    "@": "\x00",
    "[": ESC,
    "\\": "\x1c",
    "]": "\x1d",
    "^": "\x1e",
    "_": "\x1f",
    "?": "\x7f",
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
class HostProfile:
    """Capabilities verified for this outer presentation connection."""

    kitty_graphics: bool = False
    hyperlinks: bool = False
    synchronized_output: bool = False
    cell_width: int = 0
    cell_height: int = 0
    pixel_width: int = 0
    pixel_height: int = 0
    enhanced_keyboard: bool = False
    extended_styles: bool = False
    pixel_mouse: bool = False
    true_color: bool = False
    clipboard_protocol: bool = False
    image_protocol: str | None = None
    appearance: HostAppearance = HostAppearance()
    initial_appearance: HostAppearance = HostAppearance()
    title_stack: bool = False
    scheme_notifications: bool = False
    cursor_color_control: bool = False
    overline: bool = False
    initial_title: str | None = None
    title_control: bool = False

    @property
    def has_cell_size(self) -> bool:
        """Whether explicit host geometry permits pixel/cell conversion."""
        return self.cell_width > 0 and self.cell_height > 0


@dataclass(frozen=True, slots=True)
class StructuredKey:
    """A mapped host key, retaining its wire identity and observed event action.

    Modifiers use the native bridge's SHIFT/CTRL/ALT bits. Lock modifiers are
    not physical key identities, and Super/Hyper/Meta or unmapped functional
    keys are outside this deliberately bounded contract. Keypad navigation is
    normalized to ordinary navigation while ``codepoint`` keeps its identity.
    """

    key: str
    codepoint: int
    modifiers: int = 0
    action: str = "press"
    text: str | None = None
    shifted: int | None = None
    base: int | None = None


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
    if event.key in NAMED_KEYS:
        key = NAMED_KEYS[event.key]
        text = NAMED_TEXT.get(event.key, "")
        if event.key == "tab" and modifiers & SHIFT:
            key = Keys.BackTab
        elif event.key == "escape" and modifiers & SHIFT:
            key = Keys.ShiftEscape
        elif event.key not in NAMED_TEXT:
            name = event.key
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
    if modifiers & CTRL:
        character = event.key.lower()
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
    text = event.text
    if text is None:
        text = chr(event.shifted) if modifiers & SHIFT and event.shifted else event.key
        if modifiers & SHIFT and "a" <= text <= "z":
            text = text.upper()
    return prefix + [KeyPress(character, character) for character in text]


def scrub_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove host identities that a program inside the virtual PTY cannot use."""
    return {
        key: value for key, value in environment.items() if key not in HOST_ENVIRONMENT
    }


class HostBroker:
    """Collect one bounded probe and consume subsequent host control replies.

    ``ready`` concerns only the optional host capability probe. Callers may wait
    with a deadline and call ``finish_probe`` to retain a conservative profile.
    No timeout grants input ownership or implies that a shell is ready.
    """

    def __init__(
        self,
        *,
        on_focus: Callable[[bool], None] | None = None,
        on_graphics_error: Callable[[int, str], None] | None = None,
        on_key: Callable[[StructuredKey], bool] | None = None,
        on_clipboard_reply: Callable[[str], bool] | None = None,
        on_appearance: Callable[[HostAppearance], None] | None = None,
    ):
        """Keep host state independent of the child VT and PTY lifetime."""
        self.profile = HostProfile()
        self.graphics_reason = GRAPHICS_PENDING
        self.ready = asyncio.Event()
        self.on_focus = on_focus
        self.on_graphics_error = on_graphics_error
        self.on_key = on_key
        self.on_clipboard_reply = on_clipboard_reply
        self.on_appearance = on_appearance
        self.input_modes_active = False
        self.pixel_mouse_active = False
        self._started = False
        self._closed = False
        self._query_id = None
        self._cols = self._rows = 0
        self._kitty_seen = self._kitty_ok = False
        self._sync_seen = self._sync_ok = False
        self._clipboard_seen = self._clipboard_ok = False
        self._version_seen = False
        self._adapter = UNKNOWN
        self._appearance = HostAppearance()
        self._color_seen = set()
        self._cursor_extended_seen = False
        self._scheme_seen = False
        self._scheme_mode = None
        self._appearance_epoch = 0
        self._metadata_active = False
        self._title_saved = False
        self._title_fallback = False
        self._initial_title = None
        self._theme_enabled = False
        self._cell = self._pixels = None
        self._accept_size_reports = True
        self._layout_size = None
        self._kernel_size = None
        self._kernel_inset = None
        self._probe_grid = None
        self._probe_grid_valid = True
        self._reported_cell = self._reported_pixels = None
        self._cancelled = False

    @property
    def adapter(self) -> HostAdapter:
        """Expose fixed host policy separately from the active capability profile."""
        return self._adapter

    def begin_probe(self, cols: int, rows: int, *, query_id: int | None = None) -> str:
        """Register an upper-half identity, disjoint from presenter image IDs."""
        if self._started or self._closed:
            raise RuntimeError(
                "Host capabilities may be probed only once per connection"
            )
        if not 0 < cols <= MAX_DIMENSION or not 0 < rows <= MAX_DIMENSION:
            raise ValueError("Invalid host dimensions")
        if query_id is None:
            query_id = secrets.randbelow(MIN_PROBE_ID) + MIN_PROBE_ID
        if not MIN_PROBE_ID <= query_id <= MAX_GRAPHICS_ID:
            raise ValueError("Invalid graphics query identity")
        self._started = True
        self._appearance_epoch += 1
        self._cols, self._rows = cols, rows
        self._layout_size = cols, rows
        self._probe_grid = cols, rows
        self._query_id = query_id
        return (
            f"{ESC}[16t{ESC}[14t{ESC}[?2026$p{ESC}[?5522$p{ESC}[?2031$p{ESC}[?996n{ESC}[>q{ESC}[21t"
            + "".join(f"{ESC}]{number};?{ST}" for number in COLOR_NAMES)
            + f"{ESC}]21;cursor=?{ST}"
            + "".join(f"{ESC}]4;{number};?{ST}" for number in range(PALETTE_SIZE))
            + f"{ESC}_Ga=q,i={query_id},s=1,v=1,f=24,t=d;AAAA{ST}"
        )

    def _geometry(self) -> tuple[int, int]:
        if self._cell is not None:
            return self._cell
        if self._pixels is not None:
            width, height = self._pixels
            if width % self._cols == 0 and height % self._rows == 0:
                cell = width // self._cols, height // self._rows
                if all(0 < value <= MAX_CELL_PIXELS for value in cell):
                    return cell
        return 0, 0

    def finish_probe(self) -> HostProfile:
        """Freeze probe results under host policy; late replies cannot upgrade them."""
        if not self._closed:
            self._calibrate_geometry()
            adapter = self._adapter
            if adapter.extended_cursor and not self._cursor_extended_seen:
                # OSC12 alone cannot distinguish a dynamic Kitty cursor from
                # fixed black. Do not authorize a lossy change/restore pair.
                self._appearance = replace(self._appearance, cursor=None)
            width, height = self._geometry()
            pixels = self._pixels or (0, 0)
            self.profile = HostProfile(
                kitty_graphics=self._kitty_ok and width > 0 and height > 0,
                hyperlinks=adapter.hyperlinks,
                synchronized_output=self._sync_ok,
                cell_width=width,
                cell_height=height,
                pixel_width=pixels[0],
                pixel_height=pixels[1],
                enhanced_keyboard=adapter.enhanced_keyboard,
                extended_styles=adapter.extended_styles,
                pixel_mouse=adapter.pixel_mouse and width > 0 and height > 0,
                true_color=adapter.true_color,
                clipboard_protocol=self._clipboard_ok,
                image_protocol=self._image_protocol(width, height),
                appearance=self._appearance,
                initial_appearance=self._appearance,
                title_stack=adapter.title_stack,
                scheme_notifications=self._scheme_mode in (1, 2, 3)
                and self._scheme_seen,
                cursor_color_control=adapter.cursor_color
                and (
                    self._appearance.cursor is not None
                    or self._appearance.cursor_dynamic is True
                ),
                overline=adapter.overline,
                initial_title=self._initial_title,
                title_control=adapter.title_stack or self._initial_title is not None,
            )
            self._closed = True
            self._appearance_epoch += 1
            self.graphics_reason = (
                ""
                if self.profile.image_protocol
                else GRAPHICS_NO_SIZE
                if self._kitty_ok
                else GRAPHICS_REJECTED
            )
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
        return self._adapter.graphics(kitty_ack=self._kitty_ok)

    def cancel_probe(self) -> HostProfile:
        """Close negotiation with no optional capabilities, consuming late replies."""
        if not self._closed:
            self._closed = True
            self._cancelled = True
            self._kitty_ok = self._sync_ok = False
            self._adapter = UNKNOWN
            self._clipboard_ok = False
            self._appearance_epoch += 1
            self.profile = HostProfile()
            self.graphics_reason = GRAPHICS_CANCELLED
            self.ready.set()
        return self.profile

    def _changed(self) -> None:
        if (
            self._kitty_seen
            and self._sync_seen
            and self._version_seen
            and self._clipboard_seen
            and self._scheme_mode is not None
            and (self._scheme_seen or self._scheme_mode in (0, 4))
            and len(self._color_seen) == PALETTE_SIZE + len(COLOR_NAMES)
            and (self._cell or self._pixels)
        ):
            self.ready.set()

    def enter_metadata_modes(self) -> str:
        """Save a verified host title and enable observed scheme notifications.

        OSC 7 has no portable query for the host's previous cwd. This method
        never claims that the process cwd is a queried terminal metadata value.
        """
        if not self._closed or self._cancelled or self._metadata_active:
            return ""
        self._metadata_active = True
        self._appearance_epoch += 1
        sequences = []
        if self.profile.title_stack:
            sequences.append(f"{ESC}[22;2t")
            self._title_saved = True
        elif self.profile.initial_title is not None:
            self._title_fallback = True
        if self.profile.scheme_notifications and self._scheme_mode == 2:
            sequences.append(f"{ESC}[?2031h")
            self._theme_enabled = True
        return "".join(sequences)

    def leave_metadata_modes(self) -> str:
        """Restore only state this broker changed, once, without terminal queries."""
        self._metadata_active = False
        self._appearance_epoch += 1
        sequences = []
        if self._theme_enabled:
            sequences.append(f"{ESC}[?2031l")
            self._theme_enabled = False
        if self._title_saved:
            sequences.append(f"{ESC}[23;2t")
            self._title_saved = False
        elif self._title_fallback:
            sequences.append(f"{ESC}]2;{self.profile.initial_title}{ST}")
            self._title_fallback = False
        return "".join(sequences)

    def restore_cursor(self) -> str:
        """Encode a saved concrete or explicitly dynamic cursor, if observed."""
        if not self.profile.cursor_color_control:
            return ""
        original = self.profile.initial_appearance
        if original.cursor_dynamic is True:
            return f"{ESC}]21;cursor={ST}"
        if original.cursor is not None:
            red, green, blue = original.cursor
            return f"{ESC}]12;rgb:{red:02x}/{green:02x}/{blue:02x}{ST}"
        return ""

    def set_cursor_color(self, color: tuple[int, int, int]) -> str:
        """Normalize an explicitly requested cursor color only with safe restoration."""
        if len(color) != 3 or any(
            type(value) is not int or not 0 <= value <= 255 for value in color
        ):
            raise ValueError("Invalid cursor RGB color")
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
        scheme = SCHEME_REPORT.fullmatch(sequence)
        if scheme is not None:
            value = SCHEMES[int(scheme[1])]
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
        if sequence.startswith(ESC + "]l"):
            title = sequence[3:-ending]
            if (
                self._initial_title is None
                and len(title) <= MAX_TITLE
                and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in title)
            ):
                self._initial_title = title
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
                if number not in COLOR_NAMES or color is None:
                    return
                updates.append((COLOR_NAMES[number], color))
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

    def enter_input_modes(self) -> str:
        """Reconcile verified input modes after PTK enables its cell mouse mode.

        Call after the first PTK render and after a geometry profile change.
        Pixel mode supersedes cell encoding; loss of measured geometry restores
        PTK's cell format instead of guessing a pixel-to-cell scale.
        """
        if not self._closed:
            return ""
        sequences = []
        if self.profile.enhanced_keyboard and not self.input_modes_active:
            self.input_modes_active = True
            sequences.append(f"{ESC}[>{KEYBOARD_FLAGS}u")
        if self.profile.pixel_mouse and not self.pixel_mouse_active:
            self.pixel_mouse_active = True
            sequences.append(f"{ESC}[?1016h")
        elif not self.profile.pixel_mouse and self.pixel_mouse_active:
            self.pixel_mouse_active = False
            sequences.append(f"{ESC}[?1016l{ESC}[?1006h")
        return "".join(sequences)

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

    def refresh_geometry(
        self, cols: int, rows: int, pixel_width: int, pixel_height: int
    ) -> HostProfile:
        """Accept actual kernel winsize data, never ratios guessed from prior frames.

        Missing or non-divisible startup pixels retain the one-shot query fallback.
        Later geometry changes without usable pixel data revoke graphics until
        a valid kernel measurement is available. The original ACK is retained;
        no new identity-free terminal query is issued.
        """
        if not 0 < cols <= MAX_DIMENSION or not 0 < rows <= MAX_DIMENSION:
            raise ValueError("Invalid host dimensions")
        if (
            not 0 <= pixel_width <= MAX_DIMENSION
            or not 0 <= pixel_height <= MAX_DIMENSION
        ):
            raise ValueError("Invalid host pixel dimensions")
        measured = cols, rows, pixel_width, pixel_height
        if measured == self._kernel_size:
            return self.profile
        changed = self._layout_size is not None and self._layout_size != (cols, rows)
        lost_pixels = self._kernel_size is not None and any(self._kernel_size[2:])
        self._kernel_size = measured
        self._layout_size = cols, rows
        self._cols, self._rows = cols, rows
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
            and pixel_width // cols <= MAX_CELL_PIXELS
            and pixel_height // rows <= MAX_CELL_PIXELS
        )
        if valid:
            self._cell = pixel_width // cols, pixel_height // rows
            self._pixels = pixel_width, pixel_height
            self._accept_size_reports = False
            self.profile = replace(
                self.profile,
                kitty_graphics=self._closed and self._kitty_ok and not self._cancelled,
                cell_width=self._cell[0],
                cell_height=self._cell[1],
                pixel_width=pixel_width,
                pixel_height=pixel_height,
                pixel_mouse=self._closed
                and self._adapter.pixel_mouse
                and not self._cancelled,
                image_protocol=self._image_protocol(*self._cell)
                if self._closed
                else None,
            )
            if self._closed:
                self.graphics_reason = (
                    ""
                    if self.profile.image_protocol
                    else GRAPHICS_CANCELLED
                    if self._cancelled
                    else GRAPHICS_REJECTED
                )
            self._changed()
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
                cell_width=0,
                cell_height=0,
                pixel_width=0,
                pixel_height=0,
                pixel_mouse=False,
                image_protocol=None,
            )
            self.graphics_reason = GRAPHICS_RESIZED
        return self.profile

    def control(self, sequence: str) -> bool:
        """Consume a complete control reply; return false only for keyboard traffic."""
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
            match = GRAPHICS_REPLY.fullmatch(body)
            if match:
                identity, message = int(match[1]), match[2]
                if 0 < identity <= MAX_GRAPHICS_ID:
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
        size = SIZE_REPORT.fullmatch(sequence)
        if size:
            if self._started and not self._closed and self._probe_grid_valid:
                kind, height, width = map(int, size.groups())
                if 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION:
                    if (
                        kind == 6
                        and self._reported_cell is None
                        and width <= MAX_CELL_PIXELS
                        and height <= MAX_CELL_PIXELS
                    ):
                        self._reported_cell = width, height
                    elif kind == 4 and self._reported_pixels is None:
                        self._reported_pixels = width, height
            if self._started and not self._closed and self._accept_size_reports:
                kind, height, width = map(int, size.groups())
                if 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION:
                    if kind == 6 and self._cell is None:
                        if width <= MAX_CELL_PIXELS and height <= MAX_CELL_PIXELS:
                            self._cell = width, height
                    elif kind == 4 and self._pixels is None:
                        self._pixels = width, height
                    self._changed()
            return True
        sync = SYNC_REPORT.fullmatch(sequence)
        if sync:
            if self._started and not self._closed and not self._sync_seen:
                self._sync_seen = True
                self._sync_ok = int(sync[1]) in (1, 2)
                self._changed()
            return True
        clipboard = CLIPBOARD_REPORT.fullmatch(sequence)
        if clipboard:
            if self._started and not self._closed and not self._clipboard_seen:
                self._clipboard_seen = True
                self._clipboard_ok = int(clipboard[1]) in (1, 2, 3)
                self._changed()
            return True
        scheme_mode = SCHEME_MODE_REPORT.fullmatch(sequence)
        if scheme_mode:
            if self._started and not self._closed and self._scheme_mode is None:
                self._scheme_mode = int(scheme_mode[1])
                self._changed()
            return True
        # Retired/unsolicited graphics-capacity replies still are control
        # traffic. They cannot enable a backend or become keyboard text.
        if sequence.startswith(ESC + "[?1;") and sequence.endswith("S"):
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

    def __init__(self, parser, broker: HostBroker):
        """Install framing ahead of a single existing PTK key parser."""
        self.parser, self.broker = parser, broker
        self._pending = ""
        self._paste = False
        self._discard = None
        self._discard_escape = False

    def reset(self, request: bool = False) -> None:
        """Reset both parsers when PTK explicitly resets its input state."""
        self._pending = ""
        self._paste = False
        self._discard = None
        self._discard_escape = False
        self.parser.reset(request=request)

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
        self.parser.flush()

    def _key(self, sequence: str) -> bool:
        if not self.broker.input_modes_active:
            return False
        event = decode_key(sequence)
        if event is None:
            # Unknown event-type reports must not fall back to PTK's unmatched
            # escape handling, which would insert their decimal parameters.
            return (
                sequence.endswith("u")
                or (sequence.startswith(ESC + "[") and ":" in sequence)
                or KEY_REPORT.fullmatch(sequence) is not None
            )
        self.parser.flush()
        route = _KeyRoute(event)
        for keypress in _legacy_keys(event):
            keypress.structured_key = event
            keypress._host_key_route = route
            self.parser.feed_key_callback(keypress)
        return True

    def _mouse(self, sequence: str) -> bool:
        if not self.broker.pixel_mouse_active or not sequence.startswith(ESC + "[<"):
            return False
        match = PIXEL_MOUSE_REPORT.fullmatch(sequence)
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
                ESC + "]l",
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
            ended = (
                bool(CSI_FINAL.fullmatch(character))
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
        self._pending += data
        while self._pending:
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
            if self._pending[0] in C1_PREFIXES:
                self._pending = ESC + C1_PREFIXES[self._pending[0]] + self._pending[1:]
            if not self._pending.startswith(ESC):
                control = CONTROL_START.search(self._pending)
                end = control.start() if control else len(self._pending)
                self.parser.feed(self._pending[:end])
                self._pending = self._pending[end:]
                continue
            if len(self._pending) == 1:
                break
            kind = self._pending[1]
            if kind == "[":
                final = CSI_FINAL.search(self._pending, 2)
                if final is None:
                    if len(self._pending) > MAX_CONTROL:
                        self._pending = ""
                        self._discard = "csi"
                    break
                end = final.end()
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
                    if len(self._pending) > MAX_CONTROL:
                        self._discard_escape = self._pending.endswith(ESC)
                        self._pending = ""
                        self._discard = "osc" if kind == "]" else "string"
                    break
                end = min(endings)
            else:
                self.parser.feed(self._pending[:2])
                self._pending = self._pending[2:]
                continue
            sequence, self._pending = self._pending[:end], self._pending[end:]
            if sequence.endswith("\x9c"):
                sequence = sequence[:-1] + ST
            if sequence == PASTE_START:
                self._paste = True
                self.parser.feed(sequence)
            elif self._focus(sequence):
                continue
            elif len(sequence) <= MAX_CONTROL and self._clipboard(sequence):
                continue
            elif len(sequence) <= MAX_CONTROL and self._appearance(sequence):
                continue
            elif len(sequence) <= MAX_CONTROL and self._mouse(sequence):
                continue
            elif len(sequence) <= MAX_CONTROL and self._key(sequence):
                continue
            elif len(sequence) <= MAX_CONTROL and not self.broker.control(sequence):
                self.parser.feed(sequence)


class HostInput(Input):
    """Wrap a PTK VT input without adding another descriptor reader.

    The parser attribute is the narrow version-pinned PTK extension point.
    Native Windows input has a different contract and is not supported here.
    """

    def __init__(self, source: Input, broker: HostBroker):
        """Attach to the version-pinned VT parser without creating an input source."""
        parser = getattr(source, "vt100_parser", None)
        if parser is None or isinstance(parser, HostParser):
            raise TypeError("HostInput requires an unwrapped PTK VT input")
        self.source = source
        self._parser = parser
        self.parser = HostParser(parser, broker)
        source.vt100_parser = self.parser

    def fileno(self):
        """Retain the existing descriptor."""
        return self.source.fileno()

    def typeahead_hash(self):
        """Retain PTK's input identity."""
        return self.source.typeahead_hash()

    def read_keys(self):
        """Read through the sole existing PTK reader."""
        return self.source.read_keys()

    def flush_keys(self):
        """Use the framed parser for PTK's Escape flush."""
        return self.source.flush_keys()

    def flush(self):
        """Delegate the input lifecycle operation."""
        return self.source.flush()

    @property
    def closed(self):
        """Expose the original EOF state."""
        return self.source.closed

    def raw_mode(self):
        """Retain PTK's terminal mode restoration context."""
        return self.source.raw_mode()

    def cooked_mode(self):
        """Retain PTK's cooked mode context."""
        return self.source.cooked_mode()

    def attach(self, input_ready_callback):
        """Register the existing reader once."""
        return self.source.attach(input_ready_callback)

    def detach(self):
        """Detach the existing reader."""
        return self.source.detach()

    def close(self):
        """Release the wrapped input and its parser attachment."""
        self.source.vt100_parser = self._parser
        self.source.close()
