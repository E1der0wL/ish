"""Emit the same transparent SIXEL followed by independent overwrite operations."""

from __future__ import annotations

import os
import signal
import termios
import tty

from comparison_child import receive

SIXEL = b'\x1bP0;1q"1;1;4;6#1;2;100;0;0~?~?\x1b\\'


def main():
    """Keep every stage alive until explicit input acknowledges its observed frame."""
    previous = termios.tcgetattr(0)
    stage = "START"

    def resized(*_args):
        os.write(1, f"\x1b[7;1HRESIZED_{stage}".encode())

    signal.signal(signal.SIGWINCH, resized)
    try:
        tty.setraw(0)
        for name, operation in (
            ("ech", b"\x1b[H\x1b[2X"),
            ("text", b"\x1b[HAA"),
            ("ed2", b"\x1b[2J"),
        ):
            os.write(
                1,
                b"\x1b[2J\x1b[HABCD\x1b[H"
                + SIXEL
                + f"\x1b[4;1HIMAGE_{name}_READY".encode(),
            )
            receive(b"n")
            stage = name
            os.write(1, operation + f"\x1b[5;1HIMAGE_{name}_DONE".encode())
            receive(b"n")
        os.write(1, b"\x1b[6;1HIMAGES_DONE")
        receive(b"q")
    finally:
        termios.tcsetattr(0, termios.TCSANOW, previous)


if __name__ == "__main__":
    main()
