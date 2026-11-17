"""Run the shared native VT scenarios in an actual host terminal.

Only the host answers terminal queries. Scripted outer-PTY input and native file
results are recorded separately from GUI keyboard, IME and pixel verification.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import select
import signal
import sys
import termios
import time
import traceback
import tty
from pathlib import Path

from terminal_scenarios import SHELLS, HooklessTerminal, Scenario

GATE_SECONDS = 600
READ_BYTES = 65536


def record(directory, name, value):
    """Publish complete JSON evidence using the stream host harness convention."""
    temporary = directory / (name + ".next")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(directory / name)


class HostTerminal(HooklessTerminal):
    """Reuse the controlling-PTY fixture, relaying actual host replies instead of CPR."""

    def __init__(self, directory, *, shell, plugins, extra_args=(), environment=None):
        """Retain the real host's geometry and identity without modifying its settings."""
        self.directory = directory
        self.saved_host = termios.tcgetattr(0)
        self.resize_handler = signal.getsignal(signal.SIGWINCH)
        self.resize_pending = False
        self.resize_gate = None
        self.host_input = bytearray()
        self.sizes = []
        rows, cols = termios.tcgetwinsize(0)
        host_environment = {
            name: os.environ[name]
            for name in ("TERM", "COLORTERM", "WT_SESSION", "WSL_DISTRO_NAME")
            if name in os.environ
        }
        child_environment = {**host_environment, **(environment or {})}
        super().__init__(
            directory / "home",
            shell=shell,
            plugins=plugins,
            rows=rows,
            cols=cols,
            environment=child_environment,
            extra_args=extra_args,
        )
        try:
            tty.setraw(0)
            signal.signal(signal.SIGWINCH, self._resized)
            self.sizes.append([cols, rows])
            record(
                directory,
                "environment.json",
                {
                    **host_environment,
                    "python": sys.version,
                    "pid": self.pid,
                    "shell": shell,
                    "plugins": plugins,
                    "initial_size": [cols, rows],
                    "input_method": "scripted bytes into outer PTY; real host responses relayed",
                },
            )
        except BaseException:
            self.close()
            raise

    def _resized(self, *_):
        """Defer ioctl and evidence writes until ordinary fixture processing."""
        self.resize_pending = True

    def sync_size(self):
        """Propagate actual host geometry to the real outer PTY and PTK application."""
        if not self.resize_pending:
            return
        self.resize_pending = False
        rows, cols = termios.tcgetwinsize(0)
        if (rows, cols) != (self.rows, self.cols):
            self.resize(rows, cols)
            self.sizes.append([cols, rows])
            record(self.directory, "host-size.json", {"sizes": self.sizes})

    def receive(self):
        """Forward every rendered byte and use only actual host protocol responses."""
        self.sync_size()
        ready, _, _ = select.select([0, self.fd], [], [], 0.05)
        if self.fd in ready:
            try:
                data = os.read(self.fd, READ_BYTES)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            if data:
                self.output.extend(data)
                self.stream.feed(data)
                view = memoryview(data)
                while view:
                    view = view[os.write(1, view) :]
        if 0 in ready:
            data = os.read(0, READ_BYTES)
            if self.resize_gate is not None and (b"\r" in data or b"\n" in data):
                self.resize_gate.touch()
                data = data.replace(b"\r", b"").replace(b"\n", b"")
            if data:
                self.host_input.extend(data)
        self.flush_host_input()

    def flush_host_input(self):
        """Relay host input only while the fixture child owns its raw outer PTY."""
        if not self.host_input or self.status is not None:
            return
        attributes = termios.tcgetattr(self.fd)
        if attributes[3] & (termios.ECHO | termios.ICANON):
            return
        # The extra test PTY starts in canonical/echo mode. Forwarding a focus
        # report before ish enters raw mode would echo ^[[I onto the real host
        # and change its saved cursor before the application even starts.
        self.send(self.host_input)
        self.host_input.clear()

    def close(self):
        """Always restore the real host and retain emitted bytes, including failures."""
        try:
            super().close()
        finally:
            signal.signal(signal.SIGWINCH, self.resize_handler)
            termios.tcsetattr(0, termios.TCSANOW, self.saved_host)
            (self.directory / "output.bin").write_bytes(self.output)


class HostRun:
    """Record shared scenario outcomes and an optional physical host resize gate."""

    def __init__(self, directory, shell, plugins, *, require_resize=False):
        """Keep each case's native files, snapshots and restoration evidence separate."""
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.shell = shell
        self.plugins = plugins
        self.require_resize = require_resize
        self.terminal = None
        self.result = {
            "status": "running",
            "shell": shell,
            "plugins": plugins,
            "scope": "actual host protocol responses, rendered output and native files",
            "pixel_verification": "not performed",
            "gui_keyboard_verification": "not performed",
            "stages": [],
        }

    def stage(self, name):
        """Expose completed boundaries without treating screen text as product readiness."""
        entry = {"stage": len(self.result["stages"]) + 1, "name": name}
        self.result["stages"].append(entry)
        record(self.directory, "stage.json", entry)
        record(self.directory, "result.json", self.result)

    def resize(self, kind):
        """Confirm one real host size change after foreground raw stdin is ready."""
        terminal = self.terminal
        if not self.require_resize:
            return False
        before = [terminal.cols, terminal.rows]
        self.stage("resize-" + kind)
        number = len(self.result["stages"])
        gate = self.directory / f"continue-{number}"
        terminal.resize_gate = gate
        started = time.monotonic()
        try:
            terminal.require(gate.exists, GATE_SECONDS)
            terminal.resize_pending = True
            terminal.sync_size()
            terminal.require(lambda: [terminal.cols, terminal.rows] != before)
        finally:
            terminal.resize_gate = None
        record(
            self.directory,
            f"resize-{kind}.json",
            {
                "initial": before,
                "final": [terminal.cols, terminal.rows],
                "seconds": time.monotonic() - started,
                "confirmation": gate.name,
            },
        )
        return True

    def run(self):
        """Return an explicit failure unless every native scenario and restoration passes."""
        try:
            self.terminal = HostTerminal(
                self.directory, shell=self.shell, plugins=self.plugins
            )
            scenario = Scenario(
                self.terminal,
                self.shell,
                self.plugins,
                stage=self.stage,
                resize=self.resize,
            )
            scenario.run()
            self.result.update(
                status="passed",
                returncode=self.terminal.status,
                native_files=sorted(set(scenario.files)),
                sizes=self.terminal.sizes,
                foreground_sizes=json.loads(
                    (self.terminal.root / "raw-sizes").read_text()
                ),
                outer_termios_restored=termios.tcgetattr(self.terminal.fd)
                == self.terminal.attrs,
            )
        except Exception:
            self.result.update(status="failed", error=traceback.format_exc())
        finally:
            if self.terminal is not None:
                try:
                    self.terminal.close()
                    restored = termios.tcgetattr(0) == self.terminal.saved_host
                    self.result["host_termios_restored"] = restored
                    if not restored:
                        raise AssertionError(
                            "The actual host's termios was not restored"
                        )
                except Exception:
                    self.result.update(
                        status="failed", cleanup_error=traceback.format_exc()
                    )
            record(self.directory, "result.json", self.result)
        return self.result


def main():
    """Run one actual-terminal case without opening or altering a terminal profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shell", choices=SHELLS, default="tcsh")
    parser.add_argument("--plugins", action="store_true")
    parser.add_argument("--require-resize", action="store_true")
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("This host check requires an actual interactive terminal")
    result = HostRun(
        args.directory, args.shell, args.plugins, require_resize=args.require_resize
    ).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
