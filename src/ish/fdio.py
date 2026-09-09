"""Ordered writes to a nonblocking POSIX file descriptor."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from collections import deque
from typing import Callable


class FDWriter:
    """Queue partial writes and resume on readiness without blocking the loop.

    The owner must set O_NONBLOCK and close this writer before closing its fd.
    A bounded amount of work per callback also makes repeated EINTR yield.
    """

    def __init__(self, loop, fd: int, on_error: Callable[[Exception], None] | None = None,
                 *, max_pending_bytes: int = 4 * 1024 * 1024, on_flow=None):
        self.loop = loop
        self.fd = fd
        self.on_error = on_error
        self.pending = deque()
        self.error: Exception | None = None
        self.closed = False
        self._watching = False
        self._scheduled = None
        self._waiters = set()
        self.max_pending_bytes = max_pending_bytes
        self.pending_bytes = 0
        self.on_flow = on_flow
        self._paused = False

    def write(self, data: bytes | bytearray) -> None:
        if self.closed:
            raise RuntimeError("Writer is closed")
        if self.error:
            raise self.error
        if data:
            if self.pending_bytes + len(data) > self.max_pending_bytes:
                raise BufferError('Pending terminal writes exceed the byte limit')
            self.pending.append(memoryview(bytes(data)))
            self.pending_bytes += len(data)
            self._flush()
        if self.error:
            raise self.error

    def _unwatch(self) -> None:
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None
        if self._watching:
            self.loop.remove_writer(self.fd)
            self._watching = False

    def _resume(self) -> None:
        self._scheduled = None
        self._flush()

    def _finish_waiters(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                # Errors are raised by drain(), avoiding unobserved exceptions.
                waiter.set_result(None)
        self._waiters.clear()

    def _flush(self) -> None:
        if self.closed or self.error:
            return
        budget = 65536
        try:
            for _ in range(16):
                if not self.pending or budget <= 0:
                    break
                data = self.pending[0]
                try:
                    written = os.write(self.fd, data[:budget])
                except InterruptedError:
                    continue
                except BlockingIOError:
                    break
                if written == 0:
                    raise OSError(errno.EIO, "write returned zero bytes")
                budget -= written
                self.pending_bytes -= written
                if written == len(data):
                    self.pending.popleft()
                else:
                    self.pending[0] = data[written:]
            if self.pending:
                if not self._watching and self._scheduled is None:
                    try:
                        self.loop.add_writer(self.fd, self._flush)
                        self._watching = True
                    except PermissionError:
                        # epoll cannot watch regular files (redirected stdout).
                        if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                            raise
                        self._scheduled = self.loop.call_soon(self._resume)
            else:
                self._unwatch()
                self._finish_waiters()
            if self.on_flow:
                if not self._paused and self.pending_bytes >= self.max_pending_bytes // 2:
                    self._paused = True
                    self.on_flow(True)
                elif self._paused and self.pending_bytes <= self.max_pending_bytes // 4:
                    self._paused = False
                    self.on_flow(False)
        except Exception as exc:
            self.error = exc
            self.pending.clear()
            self.pending_bytes = 0
            self._unwatch()
            self._finish_waiters()
            if self.on_error:
                self.on_error(exc)

    async def drain(self) -> None:
        if self.pending and not self.error and not self.closed:
            waiter = self.loop.create_future()
            self._waiters.add(waiter)
            try:
                await waiter
            finally:
                self._waiters.discard(waiter)
        if self.error:
            raise self.error
        if self.closed:
            raise RuntimeError("Writer is closed")

    def close(self) -> None:
        self.closed = True
        self._unwatch()
        self.pending.clear()
        self.pending_bytes = 0
        self._finish_waiters()
