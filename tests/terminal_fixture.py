"""Emit deterministic native graphics while independently recording PTY input."""

import base64
import json
import os
import select
import struct
import sys
import termios
import time
import tty
import zlib
from pathlib import Path

WIDTH, HEIGHT = 96, 64
COLORS = {"red": (255, 0, 0, 255), "blue": (0, 0, 255, 255)}


def emit(color, protocol="kitty", width=WIDTH, height=HEIGHT):
    """Exercise both native RGBA and PNG input with a fixed cell placement."""
    pixels = bytes(COLORS[color]) * width * height
    if color == "blue" or protocol == "iterm":

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        rows = b"".join(
            b"\0" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height)
        )
        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
        payload = base64.b64encode(png).decode()
        format_ = "f=100"
    else:
        payload = base64.b64encode(zlib.compress(pixels)).decode()
        format_ = "f=32,o=z"
    if protocol == "iterm":
        output = f"\x1b[5;5H\x1b]1337;File=inline=1;width={width}px;height={height}px;preserveAspectRatio=0:{payload}\x1b\\"
    else:
        output = f"\x1b[5;5H\x1b_Ga=T,{format_},s={width},v={height},i=333,p=1,c=12,r=6,z=1,C=1,q=2;{payload}\x1b\\"
    sys.stdout.write(output)
    sys.stdout.flush()


def main():
    """Use test control files solely as a deterministic fixture coordination channel."""
    directory = Path(sys.argv[1])
    protocol = sys.argv[2] if len(sys.argv) > 2 else "kitty"
    saved = termios.tcgetattr(0)
    received = bytearray()
    sequence = -1
    try:
        tty.setraw(0)
        sys.stdout.write("\x1b[2J\x1b[H\x1b[2;1HGUI_FIXTURE_READY")
        emit("red", protocol)
        (directory / "fixture-ready").write_text("ready")
        while True:
            try:
                command = json.loads((directory / "fixture-control").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                command = {}
            if command.get("sequence", -1) != sequence:
                sequence = command["sequence"]
                if command.get("exit"):
                    break
                if command.get("erase"):
                    sys.stdout.write("\x1b[2J\x1b[H")
                    sys.stdout.flush()
                elif "scroll" in command:
                    sys.stdout.write("\x1b[999;1H")
                    for number in range(command["scroll"]):
                        sys.stdout.write(f"GUI_HISTORY_{number:03d}\r\n")
                    sys.stdout.flush()
                else:
                    emit(
                        command["color"],
                        protocol,
                        width=300 if command.get("wide") else WIDTH,
                    )
                (directory / "fixture-ack").write_text(str(sequence))
            ready, _, _ = select.select([0], [], [], 0.02)
            if ready:
                chunk = os.read(0, 4096)
                if not chunk:
                    break
                received.extend(chunk)
                (directory / "native-input").write_bytes(received)
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        (directory / "native-input").write_bytes(received)
        (directory / "fixture-done").write_text(str(time.monotonic()))


if __name__ == "__main__":
    main()
