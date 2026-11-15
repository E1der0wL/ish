"""A literal-wire clipboard client for isolated real-terminal regression tests."""

import base64
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


def main():
    """Write/read through a real controlling PTY and save observable results."""
    directory, action = Path(sys.argv[1]), sys.argv[2]
    fd = os.open("/dev/tty", os.O_RDWR)
    previous = termios.tcgetattr(fd)
    tty.setraw(fd)
    captured = bytearray()

    def send(metadata, payload=None):
        wire = b"\x1b]5522;" + metadata.encode()
        if payload is not None:
            wire += b";" + base64.b64encode(payload)
        os.write(fd, wire + ST)

    def receive(marker):
        start = len(captured)
        deadline = time.monotonic() + 20
        while marker not in captured[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError(bytes(captured[start:]))
            captured.extend(os.read(fd, 65536))
        return bytes(captured[start:])

    try:
        if action == "legacy":
            os.write(fd, b"\x1b]52;c;" + base64.b64encode("OSC52 한글".encode()) + ST)
        elif action == "iterm":
            os.write(
                fd, b"\x1b]1337;Copy=:" + base64.b64encode("iTerm 복사".encode()) + ST
            )
        elif action == "mime":
            send("type=write:id=gui")
            for mime, name in (
                ("image/png", "source.png"),
                ("text/plain", "source.txt"),
            ):
                data = (directory / name).read_bytes()
                encoded = base64.b64encode(mime.encode()).decode()
                for offset in range(0, len(data), 4096):
                    send("type=wdata:mime=" + encoded, data[offset : offset + 4096])
            send("type=wdata")
            receive(b"type=write:status=DONE:id=gui")
        elif action == "fire":
            send("type=write:id=fire")
            send("type=wdata:mime=dGV4dC9wbGFpbg==", "fire and forget 한글".encode())
            send("type=wdata")
        elif action == "read":
            send("type=read:id=gui", b"image/png text/plain")
            wire = receive(b"type=read:status=DONE:id=gui")
            values = {}
            for body in re.findall(rb"\x1b\]5522;(.*?)\x1b\\", wire):
                metadata, _, payload = body.partition(b";")
                fields = dict(item.split(b"=", 1) for item in metadata.split(b":"))
                if fields.get(b"status") == b"DATA":
                    mime = base64.b64decode(fields[b"mime"], validate=True).decode()
                    values.setdefault(mime, bytearray()).extend(
                        base64.b64decode(payload, validate=True)
                    )
            (directory / "read.png").write_bytes(values["image/png"])
            (directory / "read.txt").write_bytes(values["text/plain"])
        elif action == "query":
            os.write(fd, b"\x1b[?5522$p")
            receive(b"\x1b[?5522;4$y")
        else:
            raise ValueError(action)
        (directory / (action + ".json")).write_text(
            json.dumps({"ok": True, "wire": captured.hex()})
        )
    except Exception as error:
        (directory / (action + ".json")).write_text(
            json.dumps({"ok": False, "error": repr(error), "wire": captured.hex()})
        )
        raise
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        os.close(fd)


if __name__ == "__main__":
    main()
