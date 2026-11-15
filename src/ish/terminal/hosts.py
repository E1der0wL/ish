"""Keep verified host policy separate from probe execution and engine support.

Replace an identity-based rule with a protocol observation when an independent
capability query provides the same guarantee and its real-host tests still pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KITTY_VERSION = re.compile(
    r"\x1bP>\|kitty\([0-9]+(?:\.[0-9]+){1,2}[^\x00-\x1f()]*\)\x1b\\\Z"
)
XTERM_VERSION = "\x1bP>|XTerm(407)\x1b\\"
WEZTERM_VERSION = "\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\"
XTERM_MAX_INSET = 64


@dataclass(frozen=True, slots=True)
class HostAdapter:
    """Verified behaviors; the broker owns observations and their lifetime."""

    name: str
    hyperlinks: bool = False
    enhanced_keyboard: bool = False
    extended_styles: bool = False
    pixel_mouse: bool = False
    true_color: bool = False
    title_stack: bool = False
    cursor_color: bool = False
    extended_cursor: bool = False
    overline: bool = False
    raster_protocol: str | None = None
    max_inset: int | None = None

    def graphics(self, *, kitty_ack: bool) -> str | None:
        """Select output; a host name alone never grants Kitty graphics."""
        return self.raster_protocol or ("kitty" if kitty_ack else None)

    def kernel_inset(
        self,
        kernel: tuple[int, int, int, int],
        cell: tuple[int, int],
        pixels: tuple[int, int],
    ) -> tuple[int, int] | None:
        """Measure the verified fixed-border quirk from same-grid reports.

        XTerm 407 includes borders in TIOCGWINSZ but reports text pixels through
        CSI 14/16. Remove this correction when its kernel dimensions agree.
        The broker rejects stale reports; mutable scrollbar insets are excluded.
        """
        if self.max_inset is None:
            return None
        cols, rows, width, height = kernel
        cell_width, cell_height = cell
        text_width, text_height = pixels
        if pixels != (cols * cell_width, rows * cell_height):
            return None
        dx, dy = width - text_width, height - text_height
        if not (dx == dy and 0 <= dx <= self.max_inset and dx % 2 == 0):
            return None
        return dx, dy


UNKNOWN = HostAdapter("unknown")
KITTY = HostAdapter(
    "kitty",
    hyperlinks=True,
    enhanced_keyboard=True,
    extended_styles=True,
    pixel_mouse=True,
    true_color=True,
    title_stack=True,
    cursor_color=True,
    # OSC 12 cannot distinguish dynamic color from fixed black. Retire this
    # requirement only when another query preserves that distinction.
    extended_cursor=True,
    overline=True,
)
XTERM = HostAdapter(
    "xterm", title_stack=True, cursor_color=True, max_inset=XTERM_MAX_INSET
)
WEZTERM = HostAdapter(
    "wezterm",
    true_color=True,
    cursor_color=True,
    overline=True,
    # This verified build has stubbed title push/pop and a working iTerm image
    # path. Other builds need independent tests before changing these policies.
    raster_protocol="iterm",
)
VERIFIED_HOSTS = {XTERM_VERSION: XTERM, WEZTERM_VERSION: WEZTERM}


def identify_host(sequence: str) -> HostAdapter:
    """Interpret a live XTVERSION reply without broadening verified versions."""
    if KITTY_VERSION.fullmatch(sequence) is not None:
        return KITTY
    return VERIFIED_HOSTS.get(sequence, UNKNOWN)
