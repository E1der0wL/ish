"""Run Python tools in separate processes with controlling PTYs.

The parent event loop relays I/O, transfers file descriptors and terminal sizes, and
drains output after process exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import multiprocessing as mp
import os
import pickle
import pty
import sys
import termios
import traceback
from multiprocessing.reduction import recv_handle, send_handle
from typing import Any, Callable

from ish.fdio import FDWriter

__all__ = ["ProcessHandler"]


def _run_worker(func, args, kwargs, connection, encoder):
    """Spawn entry point; receive a PTY through SCM_RIGHTS, not an inherited fd."""
    try:
        slave_fd = recv_handle(connection)
    finally:
        connection.close()
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    for fd in (0, 1, 2):
        os.dup2(slave_fd, fd)
    if slave_fd > 2:
        os.close(slave_fd)
    sys.stdin = os.fdopen(0, "r", encoding=encoder, errors="replace", closefd=False)
    sys.stdout = os.fdopen(
        1, "w", encoding=encoder, errors="replace", buffering=1, closefd=False
    )
    sys.stderr = os.fdopen(
        2, "w", encoding=encoder, errors="replace", buffering=1, closefd=False
    )
    try:
        func(*args, **kwargs)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        traceback.print_exc()
        raise SystemExit(1) from None
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


class ProcessHandler:
    """Run an importable, pickleable callable in a fresh interpreter with a PTY.

    Local functions and lambdas are rejected before acquiring resources. Define
    tools in an importable module (including a plugin module), not in a closure.
    No global multiprocessing start method is modified.
    """

    def __init__(
        self,
        encoder: str = "utf-8",
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        get_terminal_fd: Callable[[], int | None] | None = None,
    ):
        """Store I/O descriptors and a lazy getter for the shell's current PTY.

        Without a shell PTY, use stdin's size, falling back to 24 rows and 80 columns
        for non-terminal input. The caller forwards size changes through resize().
        """
        self.encoder = encoder
        self.stdin_fd = sys.stdin.fileno() if stdin is None else stdin
        self.stdout_fd = sys.stdout.fileno() if stdout is None else stdout
        self.get_terminal_fd = get_terminal_fd
        self._master_fds: set[int] = set()

    def _terminal_size(self) -> tuple[int, int]:
        """Read the current source PTY size as rows and columns at the time of use."""
        fd = self.get_terminal_fd() if self.get_terminal_fd is not None else None
        try:
            rows, columns = termios.tcgetwinsize(self.stdin_fd if fd is None else fd)
        except (OSError, termios.error):
            rows, columns = 0, 0
        return rows or 24, columns or 80

    def resize(self) -> None:
        """Copy the shell's size to active workers without changing signal handlers.

        Updating a controlling PTY also notifies its foreground process group of
        SIGWINCH. Completed workers are removed before their descriptors are closed.
        """
        if self._master_fds:
            size = self._terminal_size()
            for fd in self._master_fds:
                termios.tcsetwinsize(fd, size)

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> int:
        """Run a tool and return its exit code after draining output.

        The callable and arguments must be pickleable. Reject local functions before
        acquiring resources. On cancellation, stop the worker and restore the parent's
        descriptor flags.
        """
        if not callable(func):
            raise TypeError("Python tool must be callable")
        try:
            pickle.dumps((func, args, kwargs))
        except (pickle.PickleError, TypeError, AttributeError) as exc:
            raise TypeError(
                "Python tools and arguments must be pickleable; define the tool in an importable module"
            ) from exc

        loop = asyncio.get_running_loop()
        failed = loop.create_future()
        exited = loop.create_future()
        eof = loop.create_future()
        process = None
        started = False
        sentinel = None

        def fail(exc):
            """Report the first I/O error to the main waiting task."""
            if not failed.done():
                failed.set_result(exc)

        with contextlib.ExitStack() as resources:
            try:
                for fd in dict.fromkeys([self.stdin_fd, self.stdout_fd]):
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    resources.callback(fcntl.fcntl, fd, fcntl.F_SETFL, flags)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                master_fd, slave_fd = pty.openpty()
                resources.callback(os.close, master_fd)
                # File.close() is idempotent, including on the failure path.
                slave_owner = resources.enter_context(
                    os.fdopen(slave_fd, "rb", buffering=0)
                )
                self._master_fds.add(master_fd)
                resources.callback(self._master_fds.discard, master_fd)
                # Set dimensions before the child can initialize a terminal UI.
                termios.tcsetwinsize(master_fd, self._terminal_size())
                os.set_blocking(master_fd, False)
                input_writer = FDWriter(loop, master_fd, fail)
                output_writer = FDWriter(loop, self.stdout_fd, fail)
                resources.callback(input_writer.close)
                resources.callback(output_writer.close)

                context = mp.get_context("spawn")
                parent_connection, child_connection = context.Pipe(duplex=True)
                resources.callback(parent_connection.close)
                resources.callback(child_connection.close)
                process = context.Process(
                    target=_run_worker,
                    args=(func, args, kwargs, child_connection, self.encoder),
                )
                process.start()
                started = True
                child_connection.close()
                send_handle(parent_connection, slave_fd, process.pid)
                parent_connection.close()
                slave_owner.close()
                sentinel = process.sentinel

                def on_exit():
                    """Stop watching the process sentinel and wake the exit waiter."""
                    loop.remove_reader(sentinel)
                    if not exited.done():
                        exited.set_result(None)

                def on_input():
                    """Forward user input to the worker PTY and translate EOF to EOT."""
                    try:
                        data = os.read(self.stdin_fd, 4096)
                        if not data:
                            loop.remove_reader(self.stdin_fd)
                            data = b"\x04"
                        input_writer.write(data)
                    except (BlockingIOError, InterruptedError):
                        pass
                    except Exception as exc:
                        fail(exc)

                def on_output():
                    """Queue worker output and record PTY EOF."""
                    try:
                        try:
                            data = os.read(master_fd, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            data = b""
                        if data:
                            output_writer.write(data)
                        else:
                            loop.remove_reader(master_fd)
                            if not eof.done():
                                eof.set_result(None)
                    except (BlockingIOError, InterruptedError):
                        pass
                    except Exception as exc:
                        fail(exc)

                def output_flow(paused):
                    """Pause PTY reads when the output queue fills and resume when space is
                    available.
                    """
                    if paused:
                        loop.remove_reader(master_fd)
                    elif not eof.done():
                        loop.add_reader(master_fd, on_output)

                output_writer.on_flow = output_flow

                for fd, callback in [
                    (master_fd, on_output),
                    (self.stdin_fd, on_input),
                    (sentinel, on_exit),
                ]:
                    loop.add_reader(fd, callback)
                    resources.callback(loop.remove_reader, fd)
                done, _ = await asyncio.wait(
                    [exited, failed], return_when=asyncio.FIRST_COMPLETED
                )
                if failed in done:
                    raise failed.result()
                # A slow consumer may have paused the PTY reader. Resume it
                # before starting the EOF timeout, so queued output is not lost.
                await output_writer.drain()
                # Process death can precede the final readable PTY bytes.
                done, _ = await asyncio.wait(
                    [eof, failed], timeout=1, return_when=asyncio.FIRST_COMPLETED
                )
                if failed in done:
                    raise failed.result()
                await output_writer.drain()
                process.join()
                return process.exitcode
            finally:
                if process is not None and started:
                    if process.is_alive():
                        process.terminate()
                        # Do not block the event loop waiting for a running child.
                        await asyncio.to_thread(process.join, 2)
                        if process.is_alive():
                            process.kill()
                            await asyncio.to_thread(process.join)
                    else:
                        process.join()
                    if sentinel is not None:
                        loop.remove_reader(sentinel)
                    process.close()
                elif process is not None:
                    process.close()
