"""Own the child terminal advertisement and its native query identity."""

from ish import __version__

TERM = "xterm-256color"
PROGRAM = "ish"
IDENTITY = f"{PROGRAM} {__version__}"

# libghostty deliberately avoids F3/CPR ambiguity using CSI 13;modifier~.
# Our child advertises xterm-256color, whose modified F3 entries use CSI
# 1;modifierR. Normalize only this legacy native result; negotiated keyboard
# protocols and other keys retain the encoder's exact output.
LEGACY_F3 = {
    f"\x1b[13;{modifier}~".encode(): f"\x1b[1;{modifier}R".encode()
    for modifier in range(2, 9)
}


def key_bytes(key, encoded):
    """Keep legacy function-key encoding consistent with the advertised TERM."""
    return LEGACY_F3.get(encoded, encoded) if key == "f3" else encoded
