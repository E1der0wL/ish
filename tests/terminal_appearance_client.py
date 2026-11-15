"""Literal appearance protocol client inside the real child controlling PTY."""

import json
import os
import re
import select
import sys
import termios
import time
import tty
from pathlib import Path

ST = b"\x1b\\"
DEADLINE = 20


def main():
    """Query actual virtual colors and coordinate observable rendering stages."""
    directory = Path(sys.argv[1])
    fd = os.open("/dev/tty", os.O_RDWR)
    previous = termios.tcgetattr(fd)
    tty.setraw(fd)

    def save(name, value):
        (directory / name).write_text(json.dumps(value))

    def receive(marker):
        wire = bytearray()
        deadline = time.monotonic() + DEADLINE
        while marker not in wire:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError(bytes(wire))
            wire.extend(os.read(fd, 65536))
        return bytes(wire)

    def colors(wire):
        values = {}
        for key, red, green, blue in re.findall(
            rb"\x1b\](10|11|12|4;1);rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)",
            wire,
        ):
            values[key.decode()] = [
                round(int(channel, 16) * 255 / (16 ** len(channel) - 1))
                for channel in (red, green, blue)
            ]
        return values

    try:
        os.write(
            fd,
            b"\x1b]10;?"
            + ST
            + b"\x1b]11;?"
            + ST
            + b"\x1b]12;?"
            + ST
            + b"\x1b]4;1;?"
            + ST
            + b"\x1b[?996n",
        )
        wire = receive(b"\x1b[?997;1n")
        save("appearance-query.json", {"colors": colors(wire), "wire": wire.hex()})
        os.write(
            fd,
            b"\x1b[2J\x1b[H\x1b[41m    \x1b[0m"
            b"\x1b[2;1HMMMM\x1b[3;1H\x1b[53mMMMM\x1b[55m"
            b"\x1b]12;rgb:46/b4/d2" + ST + b"\x1b[2 q\x1b[5;1H\x1b[?25h",
        )
        save("appearance-ready.json", {"ok": True})
        receive(b"r")
        os.write(fd, b"\x1b]112" + ST + b"\x1b[6;1HRESET\x1b[5;1H")
        save("appearance-reset.json", {"ok": True})
        receive(b"o")
        os.write(fd, b"\x1b]10;rgb:ab/cd/ef" + ST + b"\x1b[?2031h")
        save("appearance-override.json", {"ok": True})
        # Only a real notification makes this stage succeed, never elapsed time.
        wire = receive(b"\x1b[?997;2n")
        os.write(fd, b"\x1b]10;?" + ST + b"\x1b[?996n")
        queried = receive(b"\x1b[?997;2n")
        save(
            "appearance-theme.json",
            {
                "colors": colors(queried),
                "notification": wire.hex(),
                "wire": queried.hex(),
            },
        )
        receive(b"x")
        os.write(fd, b"\x1b]110" + ST + b"\x1b[?2031l\x1b[0m")
    except Exception as error:
        save("appearance-error.json", {"error": repr(error)})
        raise
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        os.close(fd)


if __name__ == "__main__":
    main()
