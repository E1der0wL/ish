"""Observe outer-terminal input only after ish exits in a private GUI test."""

import json
import os
import select
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path


def main():
    """Wait for child exit, then delimit leftover protocol bytes with a real DSR."""
    result = Path(sys.argv[1])
    returncode = subprocess.call(sys.argv[2:])
    descriptor = os.open("/dev/tty", os.O_RDWR)
    previous = termios.tcgetattr(descriptor)
    captured = bytearray()
    try:
        tty.setraw(descriptor)
        os.write(descriptor, b"\x1b[5n")
        deadline = time.monotonic() + 20
        while b"\x1b[0n" not in captured:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                raise TimeoutError(bytes(captured))
            captured.extend(os.read(descriptor, 65536))
        result.write_text(
            json.dumps({"returncode": returncode, "wire": captured.hex()})
        )
        # Retain the real clipboard owner's window until the test examines X11.
        os.read(descriptor, 1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, previous)
        os.close(descriptor)


if __name__ == "__main__":
    main()
