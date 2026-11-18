"""Drive ish and a pinned Zellij through the same independent outer PTY observer."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
from pathlib import Path

import psutil

from test_terminal_cli import ROOT, OuterScreen, OuterStream, Terminal, pty_open


class ComparedTerminal(Terminal):
    """Reuse PTY driving and pyte observation without inspecting either engine state."""

    ZELLIJ_VERSION = "zellij 0.45.0"
    CELL_WIDTH, CELL_HEIGHT = 8, 16
    HOST_QUERY = re.compile(rb"\x1b\[(14|16)t|\x1b\[c|\x1b\[\?80\$p")
    HOST_QUERIES = (b"\x1b[14t", b"\x1b[16t", b"\x1b[c", b"\x1b[?80$p")
    ZELLIJ_CONFIG = """default_mode "locked"
keybinds clear-defaults=true {}
pane_frames false
mouse_mode false
on_force_close "quit"
session_serialization false
show_startup_tips false
show_release_notes false
web_server false
web_sharing "disabled"
scroll_buffer_size 10000
"""

    def __init__(self, directory, engine, native, *, zellij, rows=24, cols=100):
        """Start one UI-free screen with an isolated HOME and identical native argv."""
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.rows, self.cols = rows, cols
        self.engine, self.zellij = engine, Path(zellij)
        self.output = bytearray()
        self.query_tail = b""
        self.host_tail = b""
        self.status = None
        self.prompt_before = None
        self.state_path = self.root / "unused-state.json"
        self.screen = OuterScreen(cols, rows)
        self.stream = OuterStream(self.screen)
        self.session_name = uuid.uuid4().hex[:12]
        self.runtime = tempfile.TemporaryDirectory(prefix="ish-compare-")
        self.started = time.monotonic()
        self.environment = {
            "HOME": str(self.root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": "C.UTF-8",
            "PYTHONPATH": os.environ.get("ISH_COMPARISON_SOURCE", str(ROOT / "src")),
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": self.runtime.name,
            "ZELLIJ_SOCKET_DIR": self.runtime.name,
        }
        for name, value in os.environ.items():
            if name.startswith("ISH_GHOSTTY_"):
                self.environment[name] = value
        if engine == "ish":
            invocation = [
                sys.executable,
                "-B",
                "-m",
                "ish",
                "--no-rc",
                "--no-plugins",
                "--color-depth=24",
                "--shell",
                str(native[0]),
                "--cwd",
                str(self.root),
                *[f"--shell-arg={argument}" for argument in native[1:]],
            ]
        elif engine == "zellij":
            version = subprocess.check_output(
                [str(zellij), "--version"], text=True
            ).strip()
            if version != self.ZELLIJ_VERSION:
                raise RuntimeError(f"Expected {self.ZELLIJ_VERSION}, found {version}")
            config = self.root / "zellij.kdl"
            layout = self.root / "layout.kdl"
            arguments = " ".join(json.dumps(str(value)) for value in native[1:])
            layout.write_text(
                "layout {\n pane command="
                + json.dumps(str(native[0]))
                + " close_on_exit=true {\n  args "
                + arguments
                + ";\n }\n}\n"
            )
            config.write_text(
                self.ZELLIJ_CONFIG
                + "layout_dir "
                + json.dumps(str(self.root))
                + "\ndefault_layout "
                + json.dumps(str(layout))
                + "\n"
            )
            invocation = [
                str(zellij),
                "--config",
                str(config),
                "--session",
                self.session_name,
            ]
        else:
            raise ValueError(engine)
        self.invocation = invocation
        self.fd, slave = pty_open(rows, cols)
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack(
                "HHHH", rows, cols, cols * self.CELL_WIDTH, rows * self.CELL_HEIGHT
            ),
        )
        self.attrs = termios.tcgetattr(slave)
        self.pid = os.fork()
        if self.pid == 0:
            os.close(self.fd)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for target in (0, 1, 2):
                os.dup2(slave, target)
            if slave > 2:
                os.close(slave)
            os.chdir(self.root)
            os.execve(invocation[0], invocation, self.environment)
        os.close(slave)

    def receive(self):
        """Extend the shared basic host with explicitly declared 8×16 pixel cells."""
        before = len(self.output)
        super().receive()
        data = self.host_tail + bytes(self.output[before:])
        for match in self.HOST_QUERY.finditer(data):
            if match[0] == b"\x1b[?80$p":
                response = "\x1b[?80;2$y"
            elif match[1] is None:
                response = "\x1b[?62;4;6;22c"
            elif match[1] == b"14":
                response = f"\x1b[4;{self.rows * self.CELL_HEIGHT};{self.cols * self.CELL_WIDTH}t"
            else:
                response = f"\x1b[6;{self.CELL_HEIGHT};{self.CELL_WIDTH}t"
            self.send(response)
        # Preserve incomplete queries only, never reply to a completed query twice.
        start = data.rfind(b"\x1b")
        tail = data[start:] if start >= 0 else b""
        self.host_tail = (
            tail
            if any(
                query.startswith(tail) and query != tail for query in self.HOST_QUERIES
            )
            else b""
        )

    def submit(self, text):
        """Submit a line without depending on engine diagnostic files."""
        self.send(text + "\r")

    def snapshot(self):
        """Record independently observed characters, simple styles and cursor."""
        return {
            "lines": self.screen.display,
            "cursor": [self.screen.cursor.x, self.screen.cursor.y],
            "cells": {
                f"{y},{x}": {
                    "text": cell.data,
                    "fg": cell.fg,
                    "bg": cell.bg,
                    "bold": cell.bold,
                    "italic": cell.italics,
                    "underline": cell.underscore,
                }
                for y, row in self.screen.buffer.items()
                for x, cell in row.items()
                if cell.data.strip() or cell.fg != "default" or cell.bg != "default"
            },
        }

    def finish(self, key=b"q"):
        """Let the common native child exit and verify outer termios restoration."""
        self.send(key)
        self.require(self.exited, timeout=30)
        self.receive()
        if self.status != 0:
            raise AssertionError((self.status, repr(bytes(self.output[-3000:]))))
        if termios.tcgetattr(self.fd) != self.attrs:
            raise AssertionError("Outer termios was not restored")

    def close(self):
        """Preserve raw evidence and stop only the uniquely named comparison session."""
        try:
            if self.engine == "zellij" and self.status is None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        [str(self.zellij), "kill-session", self.session_name],
                        env=self.environment,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
            super().close()
        finally:
            (self.root / "output.bin").write_bytes(self.output)
            self.runtime.cleanup()


def identity(zellij):
    """Record the actual comparator executable, not only its requested version."""
    executable = Path(zellij).resolve()
    source = Path(os.environ.get("ISH_COMPARISON_SOURCE", str(ROOT / "src")))
    library = Path(
        os.environ.get(
            "ISH_GHOSTTY_LIB", ROOT / ".cache/ghostty-vt/install/lib/libish_vt.so"
        )
    )
    digest = hashlib.sha256()
    for path in sorted((source / "ish").rglob("*.py")):
        digest.update(str(path.relative_to(source)).encode())
        digest.update(path.read_bytes())
    return {
        "path": str(executable),
        "version": subprocess.check_output(
            [str(executable), "--version"], text=True
        ).strip(),
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python": sys.version,
        "source": str(source),
        "source_sha256": digest.hexdigest(),
        "native_library": str(library),
        "native_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        "platform": list(os.uname()),
        "host_profile": {
            "term": "xterm-256color",
            "color_depth": 24,
            "cell_pixels": [8, 16],
            "sixel": True,
            "sixel_mode_start": "reset",
        },
    }


class ResourceSampler:
    """Sample both foreground clients and Zellij's uniquely owned background server."""

    INTERVAL = 0.05

    def __init__(self, terminal):
        """Keep samples and CPU high-water observations local to one session."""
        self.terminal = terminal
        self.samples = []
        self.cpu = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            owned = {}
            with contextlib.suppress(psutil.Error):
                process = psutil.Process(self.terminal.pid)
                owned[process.pid] = process
                owned.update(
                    (child.pid, child) for child in process.children(recursive=True)
                )
            if self.terminal.engine == "zellij":
                for process in psutil.process_iter(["cmdline"]):
                    arguments = process.info["cmdline"] or []
                    if "--server" in arguments and any(
                        self.terminal.session_name in arg for arg in arguments
                    ):
                        owned[process.pid] = process
                        with contextlib.suppress(psutil.Error):
                            owned.update(
                                (child.pid, child)
                                for child in process.children(recursive=True)
                            )
            rss = 0
            for process in owned.values():
                with contextlib.suppress(psutil.Error):
                    rss += process.memory_info().rss
                    times = process.cpu_times()
                    self.cpu[process.pid] = times.user + times.system
            self.samples.append(
                {"seconds": time.monotonic() - self.terminal.started, "rss": rss}
            )
            self.stop.wait(self.INTERVAL)

    def __enter__(self):
        """Begin bounded sampling while the workload is active."""
        self.thread.start()
        return self

    def __exit__(self, *exc):
        """Stop the sampler before reporting or tearing down the PTY."""
        self.stop.set()
        self.thread.join()

    def result(self):
        """Report measured process totals, with raw samples for later analysis."""
        return {
            "peak_rss_bytes": max(sample["rss"] for sample in self.samples),
            "process_cpu_seconds": sum(self.cpu.values()),
            "interpretation": "RSS sums process RSS including shared pages; CPU is the sum of sampled process high-water times and can omit short-lived children. Controller/pyte costs are outside these process totals but included in observed latency.",
            "sample_count": len(self.samples),
            "samples": self.samples,
        }
