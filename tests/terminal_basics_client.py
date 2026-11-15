"""Set child-owned presentation state and wait on native stdin for GUI checks."""

import os
import sys
import termios
import tty
from pathlib import Path


def main():
    """Expose final output before waiting; the fixture never parses shell text."""
    directory = Path(sys.argv[1])
    saved = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        os.write(
            1,
            b"\x1b]2;ISH_CHILD_TITLE\x1b\\\x1b]7;file://fixture.invalid/child\x1b\\\x1b]12;#ee2200\x1b\\\x1b[2J\x1b[HLIFECYCLE_READY",
        )
        (directory / "lifecycle-ready").write_text("ready")
        os.read(0, 1)
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)


if __name__ == "__main__":
    main()
