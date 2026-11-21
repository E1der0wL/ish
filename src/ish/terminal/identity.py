"""Own the child terminal advertisement and its native query identity."""

from ish import __version__

TERM = "xterm-256color"
PROGRAM = "ish"
IDENTITY = f"{PROGRAM} {__version__}"

# Conventional editing-key text shared by the child encoder and PTK widgets.
NAMED_TEXT = {"escape": "\x1b", "enter": "\r", "tab": "\t", "backspace": "\x7f"}
CONTROL_TEXT = {
    " ": "\x00",
    "@": "\x00",
    "[": "\x1b",
    "\\": "\x1c",
    "]": "\x1d",
    "^": "\x1e",
    "_": "\x1f",
    "?": "\x7f",
}

# libghostty deliberately avoids F3/CPR ambiguity using CSI 13;modifier~.
# Our child advertises xterm-256color, whose modified F3 entries use CSI
# 1;modifierR. Apply this mapping only when the child uses legacy F3 encoding.
LEGACY_F3 = {
    f"\x1b[13;{modifier}~".encode(): f"\x1b[1;{modifier}R".encode()
    for modifier in range(2, 9)
}


def legacy_control(key, *, alt, ctrl):
    """Fold a modified editing key to the bytes a conventional PTY can represent."""
    text = NAMED_TEXT.get(key)
    if text is None and ctrl:
        text = CONTROL_TEXT.get(key)
    if text is None:
        return b""
    return (b"\x1b" if alt else b"") + text.encode()
