"""Check management windows through real PTYs or actual host terminal replies."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import os
import select
import signal
import sys
import termios
import traceback
import tty
from pathlib import Path

from terminal_host import record
from test_terminal_cli import ROOT, OuterScreen, OuterStream, Terminal, pty_open


class ManagementTerminal(Terminal):
    """Reuse the native fixture's I/O and cleanup for the public management CLI."""

    READ_BYTES = 65536

    def __init__(
        self,
        root,
        option,
        *,
        environment=None,
        language="en",
        host=False,
        rows=28,
        cols=110,
    ):
        """Run only -h or -c with an isolated settings path and optional host relay."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = self.root / "ish"
        self.host = host
        self.saved_host = termios.tcgetattr(0) if host else None
        self.resize_handler = signal.getsignal(signal.SIGWINCH) if host else None
        self.resize_pending = False
        self.host_input = bytearray()
        if host:
            rows, cols = termios.tcgetwinsize(0)
        self.rows, self.cols = rows, cols
        self.sizes = [[cols, rows]]
        self.output, self.query_tail, self.status = bytearray(), b"", None
        self.prompt_before = None
        self.screen = OuterScreen(cols, rows)
        self.stream = OuterStream(self.screen)
        self.state_path = self.root / "unused-state.json"
        child_environment = {
            "HOME": str(self.root),
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Management must work even when loading the engine would fail.
            "ISH_GHOSTTY_LIB": str(self.root / "must-not-load.so"),
        }
        if host:
            child_environment.update(
                {
                    name: os.environ[name]
                    for name in ("TERM", "COLORTERM", "WT_SESSION", "WSL_DISTRO_NAME")
                    if name in os.environ
                }
            )
        child_environment.update(environment or {})
        self.fd, slave = pty_open(rows, cols)
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
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "ish",
                    option,
                    "--home",
                    str(self.home),
                    *(["--lang", language] if language is not None else []),
                ],
                child_environment,
            )
        os.close(slave)
        if host:
            tty.setraw(0)
            signal.signal(signal.SIGWINCH, self._resized)

    def _resized(self, *_):
        """Handle host resize in ordinary fixture flow instead of the signal handler."""
        self.resize_pending = True

    def receive(self):
        """Use the existing simulated-host fixture or relay only actual host input."""
        if not self.host:
            return super().receive()
        if self.resize_pending:
            self.resize_pending = False
            rows, cols = termios.tcgetwinsize(0)
            if (rows, cols) != (self.rows, self.cols):
                self.resize(rows, cols)
                self.sizes.append([cols, rows])
        ready, _, _ = select.select([0, self.fd], [], [], 0.02)
        if self.fd in ready:
            try:
                data = os.read(self.fd, self.READ_BYTES)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            if data:
                self.output.extend(data)
                self.stream.feed(data)
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[os.write(1, remaining) :]
        if 0 in ready:
            self.host_input.extend(os.read(0, self.READ_BYTES))
        if self.host_input and self.status is None:
            attrs = termios.tcgetattr(self.fd)
            if not attrs[3] & (termios.ECHO | termios.ICANON):
                self.send(self.host_input)
                self.host_input.clear()

    def finish(self, key=None):
        """Close the management UI and require the actual outer PTY's restoration."""
        if key is None:
            key = self.escape_key
        if key:
            self.send(key)
        self.require(self.exited)
        self.receive()
        if self.status != 0:
            raise AssertionError((self.status, bytes(self.output[-3000:])))
        if termios.tcgetattr(self.fd) != self.attrs:
            raise AssertionError("Management did not restore outer termios")

    def close_settings(self):
        """Click the close control in the actual settings title bar."""

        def close_column():
            row = self.screen.buffer[0]
            return next(
                (col + 1 for col in range(self.cols) if row[col].data == "×"), None
            )

        self.require(close_column)
        column = close_column()
        self.send(f"\x1b[<0;{column};1M\x1b[<0;{column};1m")
        self.finish(key=b"")

    def close(self):
        """Clean fixture processes and restore the actual host even after failure."""
        try:
            super().close()
        finally:
            if self.host:
                signal.signal(signal.SIGWINCH, self.resize_handler)
                termios.tcsetattr(0, termios.TCSANOW, self.saved_host)
                (self.root / "output.bin").write_bytes(self.output)


def host_case(directory, case, *, require_resize=False):
    """Record public CLI outcomes separately from physical-key and pixel evidence."""
    directory.mkdir(parents=True)
    home = directory / "ish"
    home.mkdir()
    rc = home / ".ishrc.py"
    original = b"# Host management fixture\nconfig.plugins = []\n"
    rc.write_bytes(original)
    terminal = None
    result = {
        "case": case,
        "status": "running",
        "input_method": "scripted outer PTY bytes; actual host replies",
    }
    try:
        terminal = ManagementTerminal(
            directory, "-h" if case == "help" else "-c", host=True
        )
        terminal.require(
            lambda: terminal.frame_has(
                "ish — Help" if case == "help" else "⚙  Settings"
            ),
            timeout=45,
        )
        if case == "help":
            terminal.send(b"\x1b[B\x1b[B")
            terminal.require(lambda: terminal.frame_has(".ishrc.py"))
            if require_resize:
                print("\r\nResize this Windows Terminal window once.\r\n", flush=True)
                terminal.require(lambda: len(terminal.sizes) > 1, timeout=180)
            terminal.finish()
            assert rc.read_bytes() == original
        elif case == "save":
            # Choose Korean, then Tab to Cancel and move right to activate Save.
            terminal.send(b"\r\r\x1b[B\x1b[B\r\t\x1b[C\r")
            from ish.terminal.settings import load

            terminal.require(lambda: load(rc).lang == "ko")
            terminal.close_settings()
            assert load(rc).lang == "ko"
        else:
            # Navigate after the control key to prove the same window still accepts
            # input. Escape from the sidebar must be its first actual close.
            key = b"\x03" if case == "interrupt" else b"\x04"
            terminal.send(key + b"\x1b[B")
            terminal.require(
                lambda: (
                    terminal.frame_has("▸ UI palette") and terminal.frame_has("Accent")
                )
            )
            terminal.finish()
            assert rc.read_bytes() == original
        result.update(
            status="passed",
            returncode=terminal.status,
            sizes=terminal.sizes,
            outer_termios_restored=True,
        )
    except BaseException:
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            try:
                terminal.close()
                result["host_termios_restored"] = (
                    termios.tcgetattr(0) == terminal.saved_host
                )
                if not result["host_termios_restored"]:
                    raise AssertionError(
                        "Management did not restore actual host termios"
                    )
            except BaseException:
                result.update(status="failed", cleanup_error=traceback.format_exc())
        record(directory, "result.json", result)
    return result


def restore_case(directory, case):
    """Compare actual host replies before and after non-default management sessions."""
    from terminal_restore import apply, observe

    directory.mkdir(parents=True)
    saved = termios.tcgetattr(0)
    initial = None
    terminal = None
    result = {
        "case": case,
        "status": "running",
        "input_method": "scripted outer PTY bytes; actual host state replies",
    }
    try:
        tty.setraw(0)
        initial = observe()
        required = {6, 7, 25, 1000, 1004, 1006, 2004}
        if not required.issubset(initial["modes"]):
            raise AssertionError("Host did not answer the required restoration queries")
        if any(initial["modes"][mode] not in (1, 2) for mode in required):
            raise AssertionError("Host did not report mutable starting modes")
        selected = copy.deepcopy(initial)
        selected["modes"].update(
            {6: 1, 7: 2, 12: 1, 25: 2, 1000: 1, 1004: 1, 1006: 1, 2004: 1}
        )
        selected.update(cursor=3, position=[3, 9], attributes="1;3;31;44")
        apply(selected)
        before = observe()
        if any(
            before[field] != selected[field]
            for field in ("modes", "cursor", "position")
        ) or before["attributes"] not in ("1;3;31;44", "0;1;3;31;44"):
            raise AssertionError(
                ("Host rejected selected starting state", before, selected)
            )
        help_case = case in ("restore-help", "restore-interrupt", "restore-eof")
        terminal = ManagementTerminal(
            directory / "session", "-h" if help_case else "-c", host=True
        )
        terminal.require(
            lambda: terminal.frame_has("ish — Help" if help_case else "⚙  Settings"),
            timeout=45,
        )
        key = (
            b"\x03"
            if case == "restore-interrupt"
            else b"\x04"
            if case == "restore-eof"
            else terminal.escape_key
        )
        terminal.finish(key=key)
        outer_restored = termios.tcgetattr(terminal.fd) == terminal.attrs
        terminal.close()
        terminal = None
        after = observe()
        result.update(before=before, after=after, outer_termios_restored=outer_restored)
        if before != after:
            raise AssertionError(
                ("Management changed actual starting host state", before, after)
            )
        result["status"] = "passed"
    except BaseException:
        result.update(status="failed", error=traceback.format_exc())
    finally:
        try:
            if terminal is not None:
                terminal.close()
        except BaseException:
            result.update(status="failed", cleanup_error=traceback.format_exc())
        try:
            if initial is not None:
                apply(initial)
                restored = observe()
                result.update(
                    initial=initial,
                    final=restored,
                    initial_host_state_restored=restored == initial,
                )
                if restored != initial:
                    raise AssertionError(
                        "Fixture did not restore the original host state"
                    )
        except BaseException:
            result.update(status="failed", restoration_error=traceback.format_exc())
        finally:
            termios.tcsetattr(0, termios.TCSANOW, saved)
            result["host_termios_restored"] = termios.tcgetattr(0) == saved
            record(directory, "result.json", result)
    return result


def main():
    """Check help, saving and cancellation in an actual Windows Terminal/WSL host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--require-resize", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("An actual interactive host terminal is required")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Choose a new evidence directory; prior results are retained")
    directory.mkdir(parents=True)
    results = []
    cases = ["help", "save", "interrupt", "eof"]
    if args.restore:
        cases.extend(
            ("restore-help", "restore-settings", "restore-interrupt", "restore-eof")
        )
    for case in cases:
        if case.startswith("restore-"):
            result = restore_case(directory / case, case)
        else:
            result = host_case(
                directory / case,
                case,
                require_resize=args.require_resize and case == "help",
            )
        results.append(result)
        record(directory, "summary.json", {"status": "running", "cases": results})
        if results[-1]["status"] != "passed":
            break
    passed = len(results) == len(cases) and all(
        item["status"] == "passed" for item in results
    )
    summary = {"status": "passed" if passed else "failed", "cases": results}
    record(directory, "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
