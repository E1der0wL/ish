"""Test-only persistent tcsh with FIFO syntax and a controlling PTY for stdin.

This harness observes stock-shell behavior. It grants no ish input ownership;
prompt strings are test witnesses and deadlines only fail missing outcomes.
"""

import contextlib
import errno
import fcntl
import os
import pty
import select
import shlex
import signal
import struct
import termios
import time
from pathlib import Path

PRIMARY = b"__ISH_CHANNEL_PRIMARY__> "
CONTINUATION = b"__ISH_CHANNEL_CONTINUATION__> "
DEADLINE = 8


class NativeChannel:
    """Keep one interactive tcsh alive across zero or more sourced FIFOs."""

    def __init__(self, executable, root, *, env=None, pass_fds=()):
        """Start tcsh on its PTY; optional environment/FDs support test shims.

        tcsh itself may close inherited FDs during startup. A shim that needs a
        lasting channel should reconnect using an injected socket path.
        """
        self.root = Path(root).resolve()
        self.output = bytearray()
        self.syntax_fd = None
        self.syntax_path = None
        self._source_count = 0
        self._closed = False
        environment = {
            "HOME": str(self.root),
            "TERM": "xterm",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        environment.update(env or {})
        tty_reader, tty_writer = os.pipe()
        try:
            self.pid, self.fd = pty.fork()
        except BaseException:
            os.close(tty_reader)
            os.close(tty_writer)
            raise
        if self.pid == 0:
            os.close(tty_reader)
            try:
                os.write(tty_writer, os.fsencode(os.ttyname(0)))
            finally:
                os.close(tty_writer)
            os.chdir(self.root)
            for descriptor in pass_fds:
                os.set_inheritable(descriptor, True)
            os.execve(executable, [executable, "-f"], environment)
        os.close(tty_writer)
        try:
            slave = os.read(tty_reader, 4096)
            if not slave:
                raise RuntimeError(
                    "The PTY child did not report its controlling terminal"
                )
            self.tty_path = os.fsdecode(slave)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(tty_reader)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        self.write_stdin(
            "set prompt = "
            + shlex.quote(PRIMARY.decode())
            + "; set prompt2 = "
            + shlex.quote(CONTINUATION.decode())
            + "\n"
        )
        try:
            self.require(lambda: self.output.endswith(PRIMARY))
        except BaseException:
            self.close()
            raise
        self.output.clear()

    def receive(self):
        """Collect output without interpreting silence as permission or readiness."""
        if select.select([self.fd], [], [], 0.02)[0]:
            try:
                self.output.extend(os.read(self.fd, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise

    def require(self, predicate, timeout=DEADLINE):
        """Fail if a concrete file/output witness does not arrive by its deadline."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.receive()
        if not predicate():
            raise AssertionError(repr(bytes(self.output[-5000:])))

    def _write(self, descriptor, data):
        data = data.encode() if isinstance(data, str) else data
        deadline = time.monotonic() + DEADLINE
        while data:
            try:
                data = data[os.write(descriptor, data) :]
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "Test input writer did not make progress"
                    ) from error
                self.receive()

    def write_stdin(self, data):
        """Write native terminal bytes, including control keys, to the PTY."""
        self._write(self.fd, data)

    def write_syntax(self, data):
        """Write exact syntax bytes to the current FIFO, without newline rewriting."""
        if self.syntax_fd is None:
            raise RuntimeError("No syntax FIFO is open")
        self._write(self.syntax_fd, data)

    def start_source(self, name=None):
        """Source a fresh FIFO and return its path after the kernel reader opens.

        No probe command is injected: an optional shim can gate the very first
        syntax read. Opening the FIFO is a transport witness, not PTK permission.
        Callers must observe recovery before starting another source after cancel.
        """
        if self.syntax_fd is not None:
            raise RuntimeError("Close the previous syntax writer first")
        self._source_count += 1
        if name is None:
            name = (
                "syntax.fifo"
                if self._source_count == 1
                else f"syntax-{self._source_count}.fifo"
            )
        path = self.root / name
        os.mkfifo(path)
        self.syntax_path = path
        self.output.clear()
        self.write_stdin("source " + shlex.quote(str(path)) + "\n")
        deadline = time.monotonic() + DEADLINE
        while True:
            try:
                self.syntax_fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                return path
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                if time.monotonic() >= deadline:
                    raise AssertionError("tcsh did not open the syntax FIFO") from error
                self.receive()

    def close_syntax(self):
        """Deliver actual FIFO EOF; this is different from a PTY Ctrl+D byte."""
        if self.syntax_fd is not None:
            os.close(self.syntax_fd)
            self.syntax_fd = None

    def close(self):
        """Reap this shell and its current foreground job, including failed tests."""
        if self._closed:
            return
        self._closed = True
        self.close_syntax()
        with contextlib.suppress(OSError):
            foreground = os.tcgetpgrp(self.fd)
            if foreground > 0:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(foreground, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)
        os.close(self.fd)
