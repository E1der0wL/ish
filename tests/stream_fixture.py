"""Independent native programs used by prompt-stream PTY and host tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import signal
import termios
import time
import tty
from pathlib import Path

READY = b"STREAM_NATIVE_READY\r\n"
DONE = b"STREAM_NATIVE_DONE\r\n"
HOST_QUERIES = b"\x1b[6n\x1bP$qr\x1b\\\x1b[5n"
QUERY_SECONDS = 20
INPUT_GATE_SECONDS = 30
INPUT_READ_BYTES = 65536
RAW_OUTPUT = (
    b"\x1b]2;STREAM_NATIVE_TITLE\x07"
    b"\x1b[38;2;17;35;51mTRUECOLOR\x1b[0m\r\n"
    b"\x1b]8;;https://example.invalid/stream\x1b\\LINK\x1b]8;;\x1b\\\r\n"
    b'\x1bP0;0;0q"1;1;6;6#1;2;100;0;0#1~~~~~~\x1b\\'
    b"\x1b]52;c;c3RyZWFtLXRlc3Q=\x07"
    + b"\x1b]777;foreign;"
    + b"x" * (128 * 1024)
    + b"\x1b\\"
    + b"STREAM_RAW_DONE\r\n"
)


def raw_input(path: Path) -> None:
    """Record exact stdin bytes until the independent END delimiter arrives."""
    saved = termios.tcgetattr(0)
    received = bytearray()
    try:
        tty.setraw(0, termios.TCSANOW)
        os.write(1, READY)
        while not received.endswith(b"END"):
            data = os.read(0, 4096)
            if not data:
                break
            received.extend(data)
        path.write_bytes(received)
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        os.write(1, DONE)


def gated_input(directory: Path) -> None:
    """Stop reading until a file gate opens, then persist and hash exact stdin bytes."""
    count = json.loads((directory / "input-config.json").read_text())["bytes"]
    saved = termios.tcgetattr(0)
    try:
        tty.setraw(0, termios.TCSANOW)
        (directory / "input-blocked").write_text("ready")
        os.write(1, READY)
        gate = directory / "read-gate"
        deadline = time.monotonic() + INPUT_GATE_SECONDS
        while not gate.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Explicit native stdin gate was not opened")
            time.sleep(0.01)
        digest = hashlib.sha256()
        received = 0
        with (directory / "native-input.bin").open("wb") as output:
            while received < count:
                data = os.read(0, min(INPUT_READ_BYTES, count - received))
                if not data:
                    raise EOFError(
                        f"Native stdin closed after {received} of {count} bytes"
                    )
                output.write(data)
                digest.update(data)
                received += len(data)
        (directory / "input-result.json").write_text(
            json.dumps({"bytes": received, "sha256": digest.hexdigest()})
        )
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        os.write(1, DONE)


def watch_size(path: Path) -> None:
    """Record the kernel PTY size initially and after actual SIGWINCH delivery."""
    sizes = []

    def record(*_):
        size = os.get_terminal_size(0)
        sizes.append([size.columns, size.lines])
        path.write_text(json.dumps(sizes))

    signal.signal(signal.SIGWINCH, record)
    saved = termios.tcgetattr(0)
    try:
        tty.setraw(0, termios.TCSANOW)
        record()
        os.write(1, READY)
        while os.read(0, 1) != b"q":
            pass
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        os.write(1, DONE)


def emit_raw() -> None:
    """Emit protocols as exact bytes without importing ish parsing or encoding."""
    saved = termios.tcgetattr(1)
    try:
        tty.setraw(1, termios.TCSANOW)
        os.write(1, RAW_OUTPUT)
    finally:
        termios.tcsetattr(1, termios.TCSANOW, saved)


def image(path: Path) -> None:
    """Hold a deterministic SIXEL frame in the real host until native input arrives."""
    saved = termios.tcgetattr(0)
    try:
        tty.setraw(0, termios.TCSANOW)
        output = b"\x1b[2J\x1b[HSTREAM_SIXEL_HOST_FRAME\x1b[4;4H"
        output += b'\x1bP0;0;0q"1;1;180;60'
        output += b"#1;2;100;0;0#2;2;0;100;0#3;2;0;0;100"
        for band in range(10):
            output += b"#1!60~#2!60~#3!60~"
            if band != 9:
                output += b"-"
        output += b"\x1b\\\x1b[14;1HNative image program: press q to return\r\n"
        os.write(1, output)
        os.write(1, HOST_QUERIES)
        replies = bytearray()
        deadline = time.monotonic() + QUERY_SECONDS
        while not replies.endswith(b"\x1b[0n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise TimeoutError(
                    f"Actual host did not answer DSR: {bytes(replies)!r}"
                )
            replies.extend(os.read(0, 4096))
        cpr = re.search(rb"\x1b\[(\d+);(\d+)R", replies)
        margins = re.search(rb"\x1bP1\$r(\d+);(\d+)r\x1b\\", replies)
        result = {
            "hex": replies.hex(),
            "cpr": [int(value) for value in cpr.groups()] if cpr else None,
            "dsr": replies.endswith(b"\x1b[0n"),
            "margins": [int(value) for value in margins.groups()] if margins else None,
            "size": list(os.get_terminal_size(0)),
        }
        path.with_name("host-replies.json").write_text(json.dumps(result, indent=2))
        if result["cpr"] != [15, 1]:
            raise AssertionError(f"Unexpected real host cursor reply: {result}")
        path.write_text("ready")
        while os.read(0, 1) != b"q":
            pass
    finally:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        os.write(1, DONE)


def main() -> None:
    """Select a tiny independent executable by its observable contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kind", choices=("input", "gated-input", "size", "output", "image")
    )
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.kind == "output":
        emit_raw()
    elif args.kind == "input":
        raw_input(args.path)
    elif args.kind == "gated-input":
        gated_input(args.path)
    elif args.kind == "image":
        image(args.path)
    else:
        watch_size(args.path)


if __name__ == "__main__":
    main()
