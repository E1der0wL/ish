"""Exercise a relocated Nuitka executable through real PTYs, without host Python/GCC.

Run with the development environment: `uv run python tools/smoke_distribution.py PATH`.
Test homes and TMPDIRs are private; the real user's shell files are never changed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import stat
import struct
import tempfile
import termios
import time
from pathlib import Path

import psutil


class Terminal:
    """Manage a PTY child with bounded waits and cleanup on failed assertions."""

    def __init__(self, argv: list[str], root: Path, env: dict[str, str]):
        """Start a controlling terminal and keep its original settings for exit checks."""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            os.execve(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        self.attrs = termios.tcgetattr(self.fd)
        self.output = bytearray()
        self.status = None

    def send(self, data: bytes) -> None:
        """Write raw keys to the PTY."""
        os.write(self.fd, data)

    def until(self, predicate, timeout: float = 15) -> None:
        """Wait for output and a predicate, answering cursor-position queries."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if select.select([self.fd], [], [], 0.02)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.output.extend(chunk)
                if b"\x1b[6n" in chunk:
                    self.send(b"\x1b[1;1R")
        if not predicate():
            raise AssertionError(repr(bytes(self.output[-3500:])))

    def ready(self) -> bool:
        """Recognize the test prompt and prompt-toolkit input ownership."""
        return b"READY>" in self.output and b"\x1b[?2004h" in self.output

    def submit(self, command: str) -> None:
        """Submit text as a single bracketed paste."""
        self.output.clear()
        self.send(b"\x1b[200~" + command.encode() + b"\x1b[201~\r")

    def exited(self) -> bool:
        """Reap the outer process once it exits."""
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status is not None

    def finish(self) -> None:
        """Exit normally and verify terminal restoration before closing the test PTY."""
        self.submit("ish_exit")
        # A PTY may signal EIO before waitpid observes the exit.
        deadline = time.monotonic() + 15
        while not self.exited() and time.monotonic() < deadline:
            with contextlib.suppress(AssertionError):
                self.until(self.exited, timeout=0.1)
            time.sleep(0.02)
        assert self.status == 0, (self.status, bytes(self.output[-3500:]))
        assert termios.tcgetattr(self.fd) == self.attrs, (
            "terminal settings not restored"
        )

    def close(self) -> None:
        """Kill only this test process and its descendants if normal exit did not complete."""
        if self.status is None:
            with contextlib.suppress(psutil.NoSuchProcess):
                process = psutil.Process(self.pid)
                for child in reversed(process.children(recursive=True)):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
                process.kill()
            os.waitpid(self.pid, 0)
        os.close(self.fd)


def prepare(root: Path) -> dict[str, str]:
    """Write isolated shell rc files, a Python plugin, and forbidden-tool sentinels."""
    root.mkdir()
    posix = "PS1='READY> '\nPS2='CONT> '\n"
    for name in (".bashrc", ".zshrc", ".shrc"):
        (root / name).write_text(posix)
    for name in (".tcshrc", ".cshrc"):
        (root / name).write_text("set prompt='READY> '\nset history=100\n")
    plugin = root / "ish" / "plugin" / "script" / "smoke_plugin"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        'PLUGIN_META = {"name": "smoke_plugin"}\n'
        "import sys\n"
        "from pathlib import Path\n"
        "def report(path):\n"
        "    Path(path).write_text('WORKER_OK')\n"
        "    Path(path + '.exe').write_text(sys.executable)\n"
        "    print('WORKER_OK', flush=True)\n"
    )
    (root / "ish" / ".ishrc.py").write_text(
        "from pathlib import Path\n"
        "import ish.shell.integration as integration\n"
        "from smoke_plugin import report\n"
        f"config.ISH_HOME = Path({str(root / 'custom state')!r})\n"
        "prompt.set_tool('worker', function=report)\n"
        "prompt.set_tool('say', function=print)\n"
        f"Path({str(root / 'module-location')!r}).write_text(integration.__file__)\n"
    )
    blocked = root / "blocked-bin"
    blocked.mkdir()
    for name in ("gcc", "python", "python3", "python3.12"):
        path = blocked / name
        path.write_text(
            f"#!/bin/sh\necho {name} >> '{root / 'forbidden-tools'}'\nexit 127\n"
        )
        path.chmod(0o700)
    scratch = root / "tmp"
    scratch.mkdir()
    return {
        "HOME": str(root),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "PATH": f"{blocked}:/usr/bin:/bin",
        "TMPDIR": str(scratch),
        "ENV": str(root / ".shrc"),
        "ZDOTDIR": str(root),
        "XDG_DATA_HOME": str(root / "data"),
        "ISH_PLUGIN_PYTHON": str(blocked / "python3.12"),
    }


def exercise(
    binary: Path, shell: str, root: Path, idle_seconds: float, *, onefile: bool
) -> dict:
    """Check shell I/O, worker spawn, concurrent caches, tmp cleanup, and TUI return."""
    env = prepare(root)
    executable = os.environ.get("ISH_TEST_" + shell.upper()) or shutil.which(shell)
    if not executable:
        raise RuntimeError(f"Shell not installed: {shell}")
    terminals = []
    started = time.monotonic()
    try:
        first = Terminal([str(binary), executable], root, env)
        terminals.append(first)
        first.until(first.ready, 60)
        startup = time.monotonic() - started
        bundle_helper = (
            Path((root / "module-location").read_text()).parents[2]
            / "libexec"
            / "ish_forward"
        )
        bundle_mtime = bundle_helper.stat().st_mtime_ns
        if onefile:
            assert bundle_helper.is_relative_to(root / "ish" / ".cache" / "nuitka")
        cache = root / "custom state" / ".cache"
        sessions = list(cache.glob("session-*"))
        assert len(sessions) == 1, sessions
        original = sessions[0]
        for fifo in ("shell.fifo", "tty.fifo"):
            info = (original / fifo).stat()
            assert stat.S_ISFIFO(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert stat.S_IMODE(original.stat().st_mode) == 0o700
        assert list((original / "integration").glob("shellIntegraion.*"))
        assert not list((root / "tmp").iterdir()), "runtime depends on TMPDIR"
        second = Terminal([str(binary), executable], root, env)
        terminals.append(second)
        second.until(second.ready, 60)
        second_helper = (
            Path((root / "module-location").read_text()).parents[2]
            / "libexec"
            / "ish_forward"
        )
        if onefile:
            assert second_helper != bundle_helper, (
                "concurrent launches share extracted files"
            )
        assert len(list(cache.glob("session-*"))) == 2
        shutil.rmtree(root / "tmp")
        (root / "tmp").mkdir()
        if idle_seconds:
            time.sleep(idle_seconds)
        assert bundle_helper.is_file() and second_helper.is_file()
        for index, terminal in enumerate(terminals):
            terminal.submit("sh -c 'echo INTERRUPT_READY; sleep 30'")
            terminal.until(lambda t=terminal: b"INTERRUPT_READY\r\n" in t.output)
            terminal.output.clear()
            terminal.send(b"\x03")
            terminal.until(terminal.ready, 3)
            terminal.submit(f"echo CACHE_OK > shell-{index}")
            terminal.until(
                lambda t=terminal, i=index: (root / f"shell-{i}").exists() and t.ready()
            )
            assert (root / f"shell-{index}").read_text().strip() == "CACHE_OK"
            terminal.submit("ish_recover")
            terminal.until(terminal.ready)
            terminal.submit(f"worker worker-{index}")
            terminal.until(
                lambda t=terminal, i=index: (
                    (root / f"worker-{i}").exists() and t.ready()
                ),
                30,
            )
            assert (root / f"worker-{index}").read_text() == "WORKER_OK"
            worker_exe = Path((root / f"worker-{index}.exe").read_text())
            assert worker_exe == binary or worker_exe.is_relative_to(
                root / "ish" / ".cache" / "nuitka"
            ), worker_exe
            assert b"WORKER_OK\r\n" in terminal.output
        if shell == "tcsh":
            for command, quit_keys, marker in (
                (
                    "vim -Nu NONE -n -i NONE "
                    '--cmd "set t_u7= t_RV= t_RF= t_RB=" '
                    '-c \'call writefile(["ready"], "vim-ready")\'',
                    b":q!\r",
                    b"\x1b[?1049h",
                ),
                ("env MANPAGER=less PAGER=less man top", b"q", b"Manual page top(1)"),
            ):
                first.submit(command)
                first.until(
                    lambda m=marker, q=quit_keys: (
                        m in first.output
                        and (q == b"q" or (root / "vim-ready").exists())
                    )
                )
                first.output.clear()
                first.send(quit_keys)
                first.until(first.ready, 3)
                assert b"^M" not in first.output and b"Manual page" not in first.output
                first.submit("echo TUI_OK > tui-result")
                first.until(lambda: (root / "tui-result").exists() and first.ready(), 3)
                (root / "tui-result").unlink()
        first.finish()
        assert not original.exists(), "ended session was not cleaned"
        assert len(list(cache.glob("session-*"))) == 1, "peer session was removed"
        assert second_helper.is_file(), "peer extraction was removed"
        if onefile:
            assert not bundle_helper.exists(), "ended extraction leaked"
        second.submit("say SURVIVOR_OK")
        second.until(lambda: b"SURVIVOR_OK\r\n" in second.output and second.ready(), 30)
        second.finish()
        assert not list(cache.glob("session-*")), (
            "session files leaked after normal exit"
        )
        assert not (root / "forbidden-tools").exists(), "host Python/GCC was invoked"
        extractions = list((root / "ish" / ".cache" / "nuitka").glob("*/launch-*"))
        assert not extractions, "ended extractions leaked"
        # A third launch checks restart after both concurrent instances have exited.
        reused = Terminal([str(binary), executable], root, env)
        terminals.append(reused)
        reused.until(reused.ready, 60)
        reused.finish()
        if not onefile:
            assert bundle_helper.stat().st_mtime_ns == bundle_mtime, (
                "installed helper was rewritten"
            )
        return {
            "shell": shell,
            "startup_seconds": round(startup, 3),
            "idle_seconds": idle_seconds,
            "active_extractions_after_exit": len(extractions),
            "result": "passed",
        }
    finally:
        for terminal in reversed(terminals):
            terminal.close()


def exercise_paused_extraction(binary: Path, parent: Path) -> dict:
    """Pause one executable write and require another cold launch to remain independent."""
    for attempt in range(3):
        root = parent / f"cold-start-{attempt}"
        env = prepare(root)
        first = Terminal([str(binary), "/bin/bash"], root, env)
        second = None
        paused = False
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                files = psutil.Process(first.pid).open_files()
                if any(
                    Path(f.path).name == "ish.bin" and f.mode.startswith("w")
                    for f in files
                ):
                    os.kill(first.pid, signal.SIGSTOP)
                    paused = True
                    # Confirm that the writer did not close between inspection and stop.
                    files = psutil.Process(first.pid).open_files()
                    if any(
                        Path(f.path).name == "ish.bin" and f.mode.startswith("w")
                        for f in files
                    ):
                        break
                    os.kill(first.pid, signal.SIGCONT)
                    paused = False
                time.sleep(0.001)
            if not paused:
                continue
            second = Terminal([str(binary), "/bin/bash"], root, env)
            second.until(second.ready, 60)
            second.submit("say INDEPENDENT_OK")
            second.until(
                lambda t=second: b"INDEPENDENT_OK\r\n" in t.output and t.ready(), 30
            )
            os.kill(first.pid, signal.SIGCONT)
            paused = False
            first.until(first.ready, 60)
            first.finish()
            second.finish()
            assert not list((root / "ish" / ".cache" / "nuitka").glob("*/launch-*"))
            assert not list((root / "custom state" / ".cache").glob("session-*"))
            return {"scenario": "paused_extraction", "result": "passed"}
        finally:
            if paused:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(first.pid, signal.SIGCONT)
            for terminal in (second, first):
                if terminal is not None:
                    terminal.close()
    raise AssertionError("Could not intercept extraction in three attempts")


def main() -> None:
    """Relocate the distribution away from the source tree and run shell scenarios."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--shell", action="append", dest="shells")
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.executable.resolve(strict=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="ish-distribution-test-") as directory:
        root = Path(directory)
        relocated = root / "deployment"
        if source.parent.name.endswith(".dist"):
            shutil.copytree(source.parent, relocated)
        else:
            relocated.mkdir()
            shutil.copy2(source, relocated / "ish")
        for shell in args.shells or ["bash", "zsh", "sh", "csh", "tcsh"]:
            result = exercise(
                relocated / "ish",
                shell,
                root / shell,
                args.idle_seconds,
                onefile=not source.parent.name.endswith(".dist"),
            )
            results.append(result)
            print(json.dumps(result), flush=True)
        if not source.parent.name.endswith(".dist"):
            result = exercise_paused_extraction(relocated / "ish", root)
            results.append(result)
            print(json.dumps(result), flush=True)
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
