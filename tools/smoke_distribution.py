"""Exercise a relocated CPython distribution through real PTYs, without host Python/GCC.

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
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import termios
import time
import zipfile
from pathlib import Path

import psutil

from ish.shell.adapter import get_adapter


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
        self.wait_exit(0)

    def wait_exit(self, expected: int) -> None:
        """Observe a requested exit without confusing PTY closure with process exit."""
        # A PTY may signal EIO before waitpid observes the exit.
        deadline = time.monotonic() + 15
        while not self.exited() and time.monotonic() < deadline:
            with contextlib.suppress(AssertionError):
                self.until(self.exited, timeout=0.1)
            time.sleep(0.02)
        assert self.status == expected, (self.status, bytes(self.output[-3500:]))
        assert termios.tcgetattr(self.fd) == self.attrs, (
            "terminal settings not restored"
        )

    def resume(self, reconnect: bool, timeout: float = 3) -> None:
        """Explicitly reconnect BSD csh only after its real native prompt returns."""
        if reconnect:
            self.until(lambda: self.output.endswith(b"READY> "), timeout)
            self.output.clear()
            self.send(b"ish_recover\r")
        self.until(self.ready, timeout)

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
    wheels = root / "wheels"
    wheels.mkdir()
    # A local wheel exercises real pip installation without network access or
    # changing the bundled interpreter's read-only site-packages directory.
    files = {
        "runtime_dependency/__init__.py": "VALUE = 42\n",
        "runtime_dependency/data.txt": "package-data\n",
        "runtime_dependency-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: runtime-dependency\nVersion: 1.0\n",
        "runtime_dependency-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: ish-smoke\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    with zipfile.ZipFile(
        wheels / "runtime_dependency-1.0-py3-none-any.whl", "w"
    ) as wheel:
        for name, contents in files.items():
            wheel.writestr(name, contents)
        record = "runtime_dependency-1.0.dist-info/RECORD"
        wheel.writestr(record, "".join(f"{name},,\n" for name in [*files, record]))
    plugin = root / "ish" / "plugin" / "script" / "smoke_plugin"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        'PLUGIN_META = {"name": "smoke_plugin", "dependencies": ["runtime-dependency==1.0|runtime_dependency"]}\n'
        "import sys, subprocess, sqlite3, lzma, xml.etree.ElementTree\n"
        "import importlib.resources, runtime_dependency\n"
        "from pathlib import Path\n"
        "def report(path):\n"
        "    assert runtime_dependency.VALUE == 42\n"
        "    assert importlib.resources.files(runtime_dependency).joinpath('data.txt').read_text() == 'package-data\\n'\n"
        "    assert sqlite3.connect(':memory:').execute('select 42').fetchone() == (42,)\n"
        "    assert lzma.decompress(lzma.compress(b'worker')) == b'worker'\n"
        "    assert subprocess.check_output([sys.executable, '-I', '-c', 'print(42)']).strip() == b'42'\n"
        "    Path(path).write_text('WORKER_OK')\n"
        "    Path(path + '.exe').write_text(sys.executable)\n"
        "    print('WORKER_OK', flush=True)\n"
    )
    (root / "ish" / ".ishrc.py").write_text(
        "from pathlib import Path\n"
        "import ish.shell.integration as integration\n"
        "from ish.runtime.distribution import bundled_forward_binary\n"
        "from smoke_plugin import report\n"
        f"config.ISH_HOME = Path({str(root / 'custom state')!r})\n"
        "prompt.set_tool('worker', function=report)\n"
        "prompt.set_tool('say', function=print)\n"
        f"Path({str(root / 'module-location')!r}).write_text(integration.__file__)\n"
        f"Path({str(root / 'helper-location')!r}).write_text(str(bundled_forward_binary()))\n"
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
        # The launcher must isolate Python startup without removing these from
        # the shell environment or making worker spawn depend on host settings.
        "PYTHONHOME": str(root / "nonexistent-python"),
        "PYTHONPATH": str(root / "unrelated-python"),
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheels),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def exercise(binary: Path, shell: str, root: Path, idle_seconds: float) -> dict:
    """Check shell I/O, worker spawn, concurrent caches, tmp cleanup, and TUI return."""
    bundle = binary.resolve(strict=True).parent
    env = prepare(root)
    executable = os.environ.get("ISH_TEST_" + shell.upper()) or shutil.which(shell)
    if not executable:
        raise RuntimeError(f"Shell not installed: {shell}")
    reconnect = get_adapter(shell, executable).refresh
    suffix = "; ish_recover" if reconnect else ""
    terminals = []
    started = time.monotonic()
    try:
        first = Terminal([str(binary), executable], root, env)
        terminals.append(first)
        first.until(first.ready, 60)
        startup = time.monotonic() - started
        bundle_helper = Path((root / "helper-location").read_text())
        assert bundle_helper == bundle / "libexec/ish_forward"
        bundle_mtime = bundle_helper.stat().st_mtime_ns
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
        second_helper = Path((root / "helper-location").read_text())
        assert second_helper == bundle_helper
        assert len(list(cache.glob("session-*"))) == 2
        shutil.rmtree(root / "tmp")
        (root / "tmp").mkdir()
        if idle_seconds:
            time.sleep(idle_seconds)
        assert bundle_helper.is_file() and second_helper.is_file()
        # A shell that prints a marker before spawning sleep has a SIGINT race
        # even in native csh. Announce readiness from the process that will handle
        # the signal, after its handler is installed and without another fork.
        interrupt_command = shlex.join(
            [
                str(bundle / "python/bin/python3"),
                "-I",
                "-c",
                "import signal, sys, time; signal.signal(signal.SIGINT, lambda *_: sys.exit(130)); print('INTERRUPT_READY', flush=True); time.sleep(30)",
            ]
        )
        for index, terminal in enumerate(terminals):
            terminal.submit(interrupt_command)
            terminal.until(lambda t=terminal: b"INTERRUPT_READY\r\n" in t.output)
            terminal.output.clear()
            terminal.send(b"\x03")
            terminal.resume(reconnect)
            terminal.submit(f"echo CACHE_OK > shell-{index}" + suffix)
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
            assert worker_exe.resolve() == (bundle / "python/bin/python3").resolve(), (
                worker_exe
            )
            assert b"WORKER_OK\r\n" in terminal.output
        # The reader must receive only its value, even after an input wait.
        read_command = (
            'set answer = "$<"; printf "%s" "$answer" > read-result'
            if get_adapter(shell, executable).family == "csh"
            else 'read -r answer; printf "%s" "$answer" > read-result'
        )
        first.submit('printf "INPUT_WAIT> "; ' + read_command + suffix)
        first.until(lambda: first.output.endswith(b"INPUT_WAIT> "))
        time.sleep(0.2)
        assert not (root / "read-result").exists()
        first.send(b"reader-value\r")
        first.until(lambda: (root / "read-result").exists() and first.ready())
        assert (root / "read-result").read_text() == "reader-value"

        first.submit("sleep 0.2; echo FIRST >> ordered" + suffix)
        first.send(("echo SECOND >> ordered" + suffix + "\r").encode())
        first.until(
            lambda: (
                (root / "ordered").exists()
                and (root / "ordered").read_text() == "FIRST\nSECOND\n"
                and first.ready()
            )
        )
        first.submit("echo BOUNDARY > boundary" + suffix)
        first.until(lambda: (root / "boundary").exists() and first.ready())
        assert (root / "ordered").read_text() == "FIRST\nSECOND\n"

        if all(shutil.which(tool) for tool in ("vim", "man", "less")):
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
                first.resume(reconnect)
                assert b"^M" not in first.output and b"Manual page" not in first.output
                first.submit("echo TUI_OK > tui-result" + suffix)
                first.until(lambda: (root / "tui-result").exists() and first.ready(), 3)
                (root / "tui-result").unlink()
        first.finish()
        assert not original.exists(), "ended session was not cleaned"
        assert len(list(cache.glob("session-*"))) == 1, "peer session was removed"
        assert second_helper.is_file(), "installed helper was removed"
        second.submit("say SURVIVOR_OK")
        second.until(lambda: b"SURVIVOR_OK\r\n" in second.output and second.ready(), 30)
        second.finish()
        assert not list(cache.glob("session-*")), (
            "session files leaked after normal exit"
        )
        assert not (root / "forbidden-tools").exists(), "host Python/GCC was invoked"
        # A third launch checks restart after both concurrent instances have exited.
        reused = Terminal([str(binary), executable], root, env)
        terminals.append(reused)
        reused.until(reused.ready, 60)
        reused.finish()
        assert not list(cache.glob("session-*"))
        assert bundle_helper.stat().st_mtime_ns == bundle_mtime, (
            "installed helper was rewritten"
        )
        # The launcher must forward handled termination to the actual session
        # owner, including its terminal restoration and session cleanup paths.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            ending = Terminal([str(binary), executable], root, env)
            terminals.append(ending)
            ending.until(ending.ready, 60)
            os.kill(ending.pid, signum)
            ending.wait_exit(128 + signum)
            assert not list(cache.glob("session-*"))
        return {
            "shell": shell,
            "startup_seconds": round(startup, 3),
            "idle_seconds": idle_seconds,
            "explicit_reconnect": reconnect,
            "native_read": "passed",
            "ordered_typeahead": "passed",
            "vim_man": "passed"
            if all(shutil.which(tool) for tool in ("vim", "man", "less"))
            else "not installed",
            "term_hup_cleanup": "passed",
            "result": "passed",
        }
    finally:
        for terminal in reversed(terminals):
            terminal.close()


def main() -> None:
    """Relocate the distribution away from the source tree and run shell scenarios."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--shell", action="append", dest="shells")
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.executable.resolve(strict=True)
    installation = source.parent.parent
    assert source.parent.name == "bin", source
    assert (installation / "ish").is_symlink(), installation
    assert os.readlink(installation / "ish") == "./bin/ish"
    results = []
    with tempfile.TemporaryDirectory(prefix="ish-distribution-test-") as directory:
        root = Path(directory)
        relocated = root / "deployment with spaces"
        shutil.copytree(installation, relocated, symlinks=True)
        assert (relocated / "ish").is_symlink()
        assert os.readlink(relocated / "ish") == "./bin/ish"
        link = root / "ish-link"
        link.symlink_to(relocated / "ish")
        # Verify the direct interpreter and launcher after moving the installation.
        subprocess.run(
            [
                str(relocated / "bin/python/bin/python3"),
                "-I",
                str(Path(__file__).with_name("check_runtime.py")),
            ],
            cwd=root,
            check=True,
        )
        subprocess.run([str(link), "--version"], cwd=root, check=True)
        subprocess.run(
            ["ish-link", "--version"],
            cwd=root,
            env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
            check=True,
        )
        modes = {}
        try:
            for path in [relocated, *relocated.rglob("*")]:
                if path.is_symlink():
                    continue
                modes[path] = stat.S_IMODE(path.stat().st_mode)
                path.chmod(modes[path] & ~0o222)
            for shell in args.shells or ["bash", "zsh", "sh", "csh", "tcsh"]:
                result = exercise(
                    relocated / "ish", shell, root / shell, args.idle_seconds
                )
                results.append(result)
                print(json.dumps(result), flush=True)
        finally:
            for path, mode in modes.items():
                path.chmod(mode)
    if args.report:
        args.report.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
