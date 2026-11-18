"""Emit fixed terminal contracts and record bytes at the final native consumer."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import termios
import time
import tty
from pathlib import Path

DEADLINE = 15


def record(root, name, data):
    """Publish fixture observations atomically without consulting product state."""
    path = root / name
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.replace(path)


def receive(suffix):
    """Require an explicit protocol or user-input boundary; timeout only fails."""
    data = bytearray()
    deadline = time.monotonic() + DEADLINE
    while not data.endswith(suffix):
        if time.monotonic() > deadline:
            raise RuntimeError(("Missing native input", bytes(data)))
        if select.select([0], [], [], 0.05)[0]:
            chunk = os.read(0, 4096)
            if not chunk:
                raise EOFError
            data.extend(chunk)
    return bytes(data)


def wait(root, name):
    """Use explicit fixture files to advance output-only observation stages."""
    deadline = time.monotonic() + DEADLINE
    while not (root / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError(("Missing test controller", name))
        time.sleep(0.005)


def contracts(root):
    """Cover cells, modes, queries and exact final-consumer input independently."""
    os.write(
        1,
        b"\x1b[2J\x1b[HCORE_READY\x1b[3;5HABCDEF\x1b[3;7H\x1b[2@xy\x1b[4;1H\x1b[1;38;2;123;45;67mRED\x1b[0m\x1b[6;10H\x1b[6n",
    )
    record(root, "cursor.json", {"reply": receive(b"R").hex()})
    record(
        root, "core.json", {"pid": os.getpid(), "size": list(os.get_terminal_size(0))}
    )
    wait(root, "core-next")
    os.write(1, "\x1b[2J\x1b[H한글 café é 🚀\r\nUNICODE_READY".encode())
    wait(root, "unicode-next")
    os.write(1, b"\x1b[?1049h\x1b[2J\x1b[HALTERNATE_READY")
    wait(root, "alternate-next")
    os.write(1, b"\x1b[?1049l\x1b[4;1HRETURN_READY")
    wait(root, "return-next")
    os.write(1, b"\x1b[?2004h\x1b[?1h\x1b[5;1HINPUT_READY")
    data = receive(b"|done|")
    record(root, "input.json", {"hex": data.hex()})
    os.write(1, b"\x1b[?2004l\x1b[?1l\x1b[6;1HRESIZE_READY")
    wait(root, "resize-next")
    record(root, "size.json", {"size": list(os.get_terminal_size(0))})
    os.write(1, b"\x1b[7;1HDONE_READY")
    receive(b"q")


def benchmark(root):
    """Generate numbered text and echo exact probes for bounded comparisons."""
    record(root, "benchmark.json", {"pid": os.getpid()})
    os.write(1, b"BENCH_READY")
    receive(b"s")
    start = time.monotonic()
    total = 0
    for block in range(200):
        data = b"".join(
            f"ROW_{block * 100 + line:06d}:".encode() + b"x" * 100 + b"\r\n"
            for line in range(100)
        )
        offset = 0
        while offset < len(data):
            offset += os.write(1, data[offset:])
        total += len(data)
    os.write(1, b"BENCH_OUTPUT_DONE\r\n")
    record(
        root,
        "produced.json",
        {"seconds": time.monotonic() - start, "bytes": total, "lines": 20000},
    )
    for _index in range(30):
        data = receive(b"|")
        os.write(1, b"\r\nECHO_" + data + b"\r\n")
    os.write(1, b"BENCH_DONE")
    receive(b"q")


def stress(root):
    """Fill bounded history repeatedly with explicit observer acknowledgements."""
    os.write(1, b"STRESS_READY")
    receive(b"s")
    total = 0
    start = time.monotonic()
    for batch in range(10):
        for block in range(200):
            data = b"".join(
                f"LONG_{batch:02d}_{block * 100 + line:06d}:".encode()
                + b"x" * 100
                + b"\r\n"
                for line in range(100)
            )
            offset = 0
            while offset < len(data):
                offset += os.write(1, data[offset:])
            total += len(data)
        os.write(1, f"\r\nSTRESS_BATCH_{batch:02d}\r\n".encode())
        receive(b"n")
    record(
        root,
        "stress.json",
        {"bytes": total, "lines": 200000, "seconds": time.monotonic() - start},
    )
    os.write(1, b"STRESS_DONE")
    receive(b"q")


def main():
    """Keep termios responsibility explicit in this independent native fixture."""
    root, mode = Path(sys.argv[1]), sys.argv[2]
    previous = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        signal.signal(
            signal.SIGWINCH,
            lambda *_: record(
                root, "resize-signal.json", {"size": list(os.get_terminal_size(0))}
            ),
        )
        {"contracts": contracts, "benchmark": benchmark, "stress": stress}[mode](root)
    finally:
        termios.tcsetattr(0, termios.TCSANOW, previous)


if __name__ == "__main__":
    main()
