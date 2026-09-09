from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import multiprocessing as mp
from multiprocessing.reduction import recv_handle, send_handle
import os
import pickle
import pty
import sys
import termios
import traceback
from typing import Any, Callable

from ish.fdio import FDWriter

__all__ = ['ProcessHandler']


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
    sys.stdin = os.fdopen(0, 'r', encoding=encoder, errors='replace', closefd=False)
    sys.stdout = os.fdopen(1, 'w', encoding=encoder, errors='replace', buffering=1, closefd=False)
    sys.stderr = os.fdopen(2, 'w', encoding=encoder, errors='replace', buffering=1, closefd=False)
    try:
        func(*args, **kwargs)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


class ProcessHandler:
    """Run an importable, pickleable callable in a fresh interpreter with a PTY.

    Local functions and lambdas are rejected before acquiring resources. Define
    tools in an importable module (including a plugin module), not in a closure.
    No global multiprocessing start method is modified.
    """

    def __init__(self, encoder: str = 'utf-8', *, stdin: int | None = None, stdout: int | None = None):
        self.encoder = encoder
        self.stdin_fd = sys.stdin.fileno() if stdin is None else stdin
        self.stdout_fd = sys.stdout.fileno() if stdout is None else stdout

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> int:
        if not callable(func):
            raise TypeError('Python tool must be callable')
        try:
            pickle.dumps((func, args, kwargs))
        except (pickle.PickleError, TypeError, AttributeError) as exc:
            raise TypeError('Python tools and arguments must be pickleable; define the tool in an importable module') from exc

        loop = asyncio.get_running_loop()
        failed = loop.create_future()
        exited = loop.create_future()
        eof = loop.create_future()
        process = None
        started = False
        sentinel = None

        def fail(exc):
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
                slave_owner = resources.enter_context(os.fdopen(slave_fd, 'rb', buffering=0))
                os.set_blocking(master_fd, False)
                input_writer = FDWriter(loop, master_fd, fail)
                output_writer = FDWriter(loop, self.stdout_fd, fail)
                resources.callback(input_writer.close)
                resources.callback(output_writer.close)

                context = mp.get_context('spawn')
                parent_connection, child_connection = context.Pipe(duplex=True)
                resources.callback(parent_connection.close)
                resources.callback(child_connection.close)
                process = context.Process(target=_run_worker, args=(func, args, kwargs, child_connection, self.encoder))
                process.start()
                started = True
                child_connection.close()
                send_handle(parent_connection, slave_fd, process.pid)
                parent_connection.close()
                slave_owner.close()
                sentinel = process.sentinel

                def on_exit():
                    loop.remove_reader(sentinel)
                    if not exited.done():
                        exited.set_result(None)

                def on_input():
                    try:
                        data = os.read(self.stdin_fd, 4096)
                        if not data:
                            loop.remove_reader(self.stdin_fd)
                            data = b'\x04'
                        input_writer.write(data)
                    except (BlockingIOError, InterruptedError):
                        pass
                    except Exception as exc:
                        fail(exc)

                def on_output():
                    try:
                        try:
                            data = os.read(master_fd, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            data = b''
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
                    if paused:
                        loop.remove_reader(master_fd)
                    elif not eof.done():
                        loop.add_reader(master_fd, on_output)

                output_writer.on_flow = output_flow

                for fd, callback in [(master_fd, on_output), (self.stdin_fd, on_input), (sentinel, on_exit)]:
                    loop.add_reader(fd, callback)
                    resources.callback(loop.remove_reader, fd)
                done, _ = await asyncio.wait([exited, failed], return_when=asyncio.FIRST_COMPLETED)
                if failed in done:
                    raise failed.result()
                # A slow consumer may have paused the PTY reader. Resume it
                # before starting the EOF timeout, so queued output is not lost.
                await output_writer.drain()
                # Process death can precede the final readable PTY bytes.
                done, _ = await asyncio.wait([eof, failed], timeout=1, return_when=asyncio.FIRST_COMPLETED)
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
