"""Measure outer terminal state before and after an actual ish CLI lifetime."""

import json
import os
import select
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

TITLE = "ISH_PARENT_TITLE"
QUERY = b"\x1b]12;?\x1b\\\x1b[?1049$p\x1b[?1000$p\x1b[?1004$p\x1b[?2004$p\x1b[?u\x1b[5n"


def query(fd):
    """Delimit real host replies with a DSR, using time only as a failure deadline."""
    previous = termios.tcgetattr(fd)
    data = bytearray()
    try:
        tty.setraw(fd)
        os.write(fd, QUERY)
        deadline = time.monotonic() + 15
        while not data.endswith(b"\x1b[0n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError(bytes(data))
            data.extend(os.read(fd, 65536))
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
    return bytes(data).hex()


def main():
    """Keep the real host open for independent GUI inspection after child exit."""
    directory = Path(sys.argv[1])
    fd = os.open("/dev/tty", os.O_RDWR)
    before_attrs = termios.tcgetattr(fd)
    os.write(fd, f"\x1b]2;{TITLE}\x1b\\\x1b]12;#123456\x1b\\".encode())
    before = query(fd)
    child = subprocess.Popen(sys.argv[2:])
    (directory / "lifecycle-parent.json").write_text(
        json.dumps({"pid": child.pid, "cwd": str(Path.cwd())})
    )
    result = child.wait()
    restored = termios.tcgetattr(fd) == before_attrs
    after = query(fd)
    (directory / "lifecycle-result.json").write_text(
        json.dumps(
            {
                "returncode": result,
                "before": before,
                "after": after,
                "termios_restored": restored,
            }
        )
    )
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        os.close(fd)


if __name__ == "__main__":
    main()
