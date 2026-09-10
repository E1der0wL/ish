"""End-to-end Linux PTY test with isolated HOME, runtime files and environment."""

import fcntl
import os
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

import psutil


class ShellPTYTests(unittest.TestCase):
    """Verify real CLI initialization, command execution, exit, and runtime-file cleanup."""

    def test_large_environment_start_command_and_exit(self):
        """Start the CLI with a large environment, run a command, exit normally, and clean
        up files.
        """
        self.run_cli("bash")

    def test_zsh_cli(self):
        """Pass prompt, command, and exit boundaries in a zsh CLI session."""
        self.run_cli("zsh")

    def test_csh_cli(self):
        """Exit a BSD csh CLI session normally, including its separate state refresh."""
        self.run_cli("csh")

    def test_tcsh_cli(self):
        """Return to the primary prompt and clean up resources in a tcsh CLI session."""
        self.run_cli("tcsh")

    def test_sh_cli(self):
        """Load integration and refresh state in a POSIX sh CLI session."""
        self.run_cli("sh")

    def run_cli(self, name):
        """Run the CLI with an isolated shell rc and verify prompt, command, and exit
        behavior.
        """
        shell = os.environ.get("ISH_TEST_" + name.upper()) or shutil.which(name)
        if not shell:
            self.skipTest(name + " not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ish-p1-pty-") as directory:
            scratch = Path(directory)
            (scratch / "tmp").mkdir()
            if name == "zsh":
                modules = list(
                    Path(shell).parent.parent.glob("lib/*/zsh/*/zsh/parameter.so")
                )
                if modules:
                    (scratch / ".zshrc").write_text(
                        'module_path=("'
                        + str(modules[0].parent.parent)
                        + '" $module_path)\n'
                    )
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(root)
                env = {
                    "HOME": directory,
                    "XDG_DATA_HOME": str(scratch / "data"),
                    "TMPDIR": str(scratch / "tmp"),
                    "TERM": "xterm-256color",
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "PYTHONPATH": str(root / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    # Larger than a FIFO buffer, including formerly ambiguous bytes.
                    "P1_LARGE": "x" * 65536 + "\nline\x01\x04\x1e\x1f",
                }
                os.execve(
                    sys.executable, [sys.executable, "-B", "-m", "ish.main", shell], env
                )
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            received = bytearray()
            reaped = False

            def until(predicate, timeout=8):
                """Read PTY output until the predicate succeeds or the timeout expires."""
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    if predicate():
                        return True
                    if not select.select([fd], [], [], 0.05)[0]:
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    received.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(fd, b"\x1b[1;1R")
                return predicate()

            try:
                self.assertTrue(
                    until(lambda: b"\x1b[?2004h" in received),
                    repr(bytes(received[-1000:])),
                )
                received.clear()
                os.write(fd, b"printf 'P1_%s\\n' OK\r")
                self.assertTrue(
                    until(lambda: b"P1_OK\r\n" in received),
                    repr(bytes(received[-1000:])),
                )
                self.assertTrue(
                    until(
                        lambda: (
                            received.find(b"\x1b[?2004h", received.find(b"P1_OK\r\n"))
                            >= 0
                        )
                    )
                )
                os.write(fd, b"ish_exit\r")
                until(lambda: False, 2)
                # PTY EOF may precede waitpid readiness, and the engine allows
                # two seconds for shell hangup before forceful shutdown.
                deadline = time.monotonic() + 3
                while not reaped and time.monotonic() < deadline:
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    reaped = bool(waited)
                    if not reaped:
                        time.sleep(0.01)
                self.assertTrue(
                    reaped, "ish did not exit: " + repr(bytes(received[-1000:]))
                )
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertEqual(list((scratch / "tmp").iterdir()), [])
            finally:
                if not reaped:
                    try:
                        proc = psutil.Process(pid)
                        for child in reversed(proc.children(recursive=True)):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        proc.kill()
                    except psutil.NoSuchProcess:
                        pass
                    os.waitpid(pid, 0)
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
