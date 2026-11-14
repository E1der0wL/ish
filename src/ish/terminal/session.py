"""Own one controlling PTY without interpreting shell prompts or command syntax."""

from __future__ import annotations

import asyncio
import errno
import os
import pty
import signal
import termios
from collections.abc import Callable

from ish.runtime.fdio import FDWriter

READ_BYTES = 65536
READ_BUDGET = 256 * 1024
SHUTDOWN_SECONDS = 2
SHUTDOWN_INTERVAL = 0.05
TERM = "xterm-256color"


class Session:
    """Forward native input and terminal output through a single ordered PTY."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        output: Callable[[bytes], None],
        exited: Callable[[int], None],
        failed: Callable[[Exception], None],
    ):
        """Keep process configuration separate from UI focus and VT state."""
        self.argv, self.cwd = argv, cwd
        self.output, self.exited, self.failed = output, exited, failed
        self.pid = self.fd = None
        self.writer = self.waiter = self.loop = None
        self.returncode = None
        self.eof = False
        self.closed = False
        self.size = (0, 0)

    def start(self, cols: int, rows: int) -> None:
        """Create the shell's controlling terminal before starting background work."""
        if self.pid is not None:
            raise RuntimeError("The terminal session has already started")
        self.loop = asyncio.get_running_loop()
        env = os.environ.copy()
        env.update(TERM=TERM, COLORTERM="truecolor", TERM_PROGRAM="ish-vt")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(self.cwd)
                termios.tcsetwinsize(0, (rows, cols))
                os.execvpe(self.argv[0], self.argv, env)
            except BaseException as exc:
                os.write(2, f"ish-vt: {exc}\r\n".encode(errors="replace"))
                os._exit(127)
        self.size = (cols, rows)
        os.set_blocking(self.fd, False)
        self.writer = FDWriter(self.loop, self.fd, self.failed)
        self.loop.add_reader(self.fd, self._read)
        self.waiter = self.loop.create_task(self._wait())

    def _read(self) -> None:
        """Feed bounded output batches to the VT engine; never write to stdout."""
        budget = READ_BUDGET
        try:
            while budget > 0:
                try:
                    data = os.read(self.fd, min(READ_BYTES, budget))
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    self.eof = True
                    self.loop.remove_reader(self.fd)
                    break
                budget -= len(data)
                self.output(data)
        except Exception as exc:
            self.loop.remove_reader(self.fd)
            self.failed(exc)

    async def _wait(self) -> None:
        """Reap the actual child without foreground polling or /proc inspection."""
        _, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        self.returncode = os.waitstatus_to_exitcode(status)
        if not self.closed:
            # A background process may keep the slave open after the shell has
            # exited. Drain already available output without tying UI lifetime
            # to that process or guessing from a quiet-output timeout.
            if not self.eof:
                self._read()
            self.exited(self.returncode)

    def send(self, data: bytes) -> None:
        """Queue bytes from the focused pane or a terminal protocol reply."""
        if not self.closed and not self.eof and self.writer is not None:
            try:
                self.writer.write(data)
            except Exception as exc:
                self.failed(exc)

    def resize(self, cols: int, rows: int) -> None:
        """Advertise only the pane's dimensions to the child terminal."""
        if self.fd is not None and not self.closed and self.size != (cols, rows):
            termios.tcsetwinsize(self.fd, (rows, cols))
            self.size = (cols, rows)

    async def close(self) -> None:
        """Release the PTY and terminate remaining groups belonging to this session."""
        if self.closed:
            return
        self.closed = True
        if self.fd is None:
            return
        groups = {self.pid} if self.returncode is None else set()
        try:
            groups.add(os.tcgetpgrp(self.fd))
        except OSError:
            pass
        groups.discard(os.getpgrp())
        groups = {group for group in groups if group is not None and group > 0}
        self.loop.remove_reader(self.fd)
        self.writer.close()
        # Closing a controlling PTY also notifies its foreground group. Explicit
        # signals cover a popup/forced close while that group is running a TUI.
        for group in groups:
            try:
                os.killpg(group, signal.SIGHUP)
            except ProcessLookupError:
                pass
        os.close(self.fd)
        deadline = self.loop.time() + SHUTDOWN_SECONDS
        while groups and self.loop.time() < deadline:
            for group in tuple(groups):
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    groups.remove(group)
            if groups:
                await asyncio.sleep(SHUTDOWN_INTERVAL)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await self.waiter
