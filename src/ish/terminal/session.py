"""Own one controlling PTY without interpreting shell prompts or command syntax."""

from __future__ import annotations

import asyncio
import errno
import os
import pty
import signal
import termios
from collections.abc import Callable

from ish.lang import Message, i18n
from ish.runtime.fdio import FDWriter

READ_BYTES = 65536
READ_BUDGET = 256 * 1024
SHUTDOWN_SECONDS = 2
SHUTDOWN_INTERVAL = 0.05
TERM = "xterm-256color"
MAX_PENDING_INPUT = 4 * 1024 * 1024


class Session:
    """Forward native input and terminal output through a single ordered PTY."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        output: Callable[[bytes], None],
        exited: Callable[[int], None],
        failed: Callable[[Exception], None],
        *,
        env: dict[str, str] | None = None,
        input_flow: Callable[[bool], None] | None = None,
        child_exited: Callable[[int], None] | None = None,
    ):
        """Keep process exit observation separate from final output completion.

        child_exited observes native process termination; exited follows final
        delivery to output. Neither callback claims a host writer has drained.
        """
        self.argv, self.cwd = argv, cwd
        self.output, self.exited, self.failed = output, exited, failed
        self.env = env
        self.input_flow = input_flow
        self.child_exited = child_exited
        self.output_enabled = True
        self.pid = self.fd = None
        self.writer = self.waiter = self.loop = None
        self.returncode = None
        self.eof = False
        self.closed = False
        self.size = (0, 0)
        self.pending_input = bytearray()
        self._output_done = None
        self._final_remaining = None

    def start(self, cols: int, rows: int) -> None:
        """Create the shell's controlling terminal before starting background work."""
        if self.pid is not None:
            raise RuntimeError(i18n.get(Message.SESSION_STARTED))
        self.loop = asyncio.get_running_loop()
        self._output_done = self.loop.create_future()
        if self.env is None:
            from .host import scrub_environment

            env = scrub_environment(os.environ)
            env.update(TERM=TERM, COLORTERM="truecolor", TERM_PROGRAM="ish-vt")
        else:
            env = dict(self.env)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(self.cwd)
                termios.tcsetwinsize(0, (rows, cols))
                os.execvpe(self.argv[0], self.argv, env)
            except BaseException as exc:
                os.write(2, f"ish: {exc}\r\n".encode(errors="replace"))
                os._exit(127)
        self.size = (cols, rows)
        os.set_blocking(self.fd, False)
        self.writer = FDWriter(self.loop, self.fd, self.failed, on_flow=self.input_flow)
        if self.pending_input:
            self.writer.write(self.pending_input)
            self.pending_input.clear()
        self.loop.add_reader(self.fd, self._read)
        self.waiter = self.loop.create_task(self._wait())

    def _read(self) -> None:
        """Deliver bounded output batches, retaining final work across pauses."""
        if self.closed or not self.output_enabled or self._output_done.done():
            return
        budget = READ_BUDGET
        if self._final_remaining is not None:
            budget = min(budget, self._final_remaining)
        try:
            while budget > 0:
                try:
                    data = os.read(self.fd, min(READ_BYTES, budget))
                except BlockingIOError:
                    if self._final_remaining is not None:
                        self._finish_output()
                    break
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    self.eof = True
                    self._finish_output()
                    break
                budget -= len(data)
                if self._final_remaining is not None:
                    self._final_remaining -= len(data)
                self.output(data)
                if self._final_remaining == 0:
                    self._finish_output()
                    break
                if not self.output_enabled:
                    break
        except Exception as exc:
            self._finish_output(delivered=False)
            self.failed(exc)

    def _finish_output(self, *, delivered=True) -> None:
        """Finish the final delivery or release its waiter during forced close."""
        self.loop.remove_reader(self.fd)
        if not self._output_done.done():
            self._output_done.set_result(delivered)

    async def _wait(self) -> None:
        """Observe child exit before reporting completion of its final output."""
        _, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        self.returncode = os.waitstatus_to_exitcode(status)
        if not self.closed:
            # Fresh and previously queued input must not reach a retained slave
            # while the host finishes consuming this child's output.
            self.writer.close()
            try:
                if self.child_exited:
                    self.child_exited(self.returncode)
            except Exception as exc:
                self._finish_output(delivered=False)
                self.failed(exc)
                return
            # A background process may keep the slave open after the shell has
            # exited, or keep producing output. Preserve the existing one-pass
            # final budget, carrying its remainder across host backpressure.
            # EAGAIN ends that pass without waiting for a retained slave's EOF.
            if not self._output_done.done():
                self._final_remaining = READ_BUDGET
                self._read()
            delivered = await self._output_done
            if delivered and not self.closed:
                self.exited(self.returncode)

    def send(self, data: bytes) -> None:
        """Queue bytes from the focused pane or a terminal protocol reply."""
        if not self.closed and not self.eof and self.returncode is None:
            try:
                if self.writer is None:
                    if len(self.pending_input) + len(data) > MAX_PENDING_INPUT:
                        raise BufferError(i18n.get(Message.INPUT_LIMIT))
                    self.pending_input.extend(data)
                else:
                    self.writer.write(data)
            except Exception as exc:
                self.failed(exc)

    def resize(self, cols: int, rows: int) -> None:
        """Advertise only the pane's dimensions to the child terminal."""
        if self.fd is not None and not self.closed and self.size != (cols, rows):
            termios.tcsetwinsize(self.fd, (rows, cols))
            self.size = (cols, rows)

    def set_output_enabled(self, enabled: bool) -> None:
        """Apply backpressure without changing the child or consuming its output."""
        self.output_enabled = enabled
        if self.fd is None or self.closed or self._output_done.done():
            return
        if enabled:
            self.loop.add_reader(self.fd, self._read)
            if self._final_remaining is not None:
                # A retained slave with no pending bytes is not readable. Probe
                # once after resuming so EAGAIN can finish the final pass.
                self.loop.call_soon(self._read)
        else:
            self.loop.remove_reader(self.fd)

    async def close(self) -> None:
        """Release the PTY and terminate remaining groups belonging to this session."""
        if self.closed:
            return
        self.closed = True
        self.pending_input.clear()
        if self.fd is None:
            return
        self._finish_output(delivered=False)
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
