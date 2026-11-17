"""Hub's isolated X11 ownership lifecycle, adapted to the text provider contract.

Only CLIPBOARD is owned. The child retains its selection until replacement or
close; Xlib and its failure handlers never run in the terminal's UI process.
"""

import asyncio
import errno
import sys
from pathlib import Path

from .clipboard_wire import MAX_BYTES, READ_CHUNK, TIMEOUT_SECONDS, X11Wire


async def read_bounded(stream):
    """Bound memory before a subprocess can supply an oversized clipboard."""
    data = bytearray()
    while chunk := await stream.read(min(READ_CHUNK, MAX_BYTES + 1 - len(data))):
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise OSError(errno.EFBIG, "")
    return bytes(data)


class X11Clipboard:
    """Own the native selection worker independently of the UI process."""

    def __init__(self, env):
        """Capture this session's DISPLAY and XAUTHORITY without connecting yet."""
        self.env = dict(env)
        self.owner = None
        self.tasks = set()
        self.closed = False

    async def _reap(self, process):
        try:
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def transfer(self, text=None):
        """Read fresh selection text or retain an acknowledged owner for a write."""
        if self.closed or not self.env.get("DISPLAY"):
            raise OSError(errno.ENOSYS, "")
        process = None
        retained = False
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).with_name(X11Wire.WORKER)),
                    X11Wire.READ if text is None else X11Wire.COPY,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self.env,
                )
                if text is None:
                    process.stdin.close()
                    data = await read_bounded(process.stdout)
                    if await process.wait() != X11Wire.SUCCESS:
                        raise OSError(errno.EIO, "")
                    return data.decode("utf-8")
                payload = text.encode("utf-8")
                if len(payload) > MAX_BYTES:
                    raise OSError(errno.EFBIG, "")
                process.stdin.write(X11Wire.header(len(payload)) + payload)
                await process.stdin.drain()
                if await process.stdout.readline() != X11Wire.OWNED:
                    raise OSError(errno.EIO, "")
                if self.closed:
                    raise OSError(errno.ECANCELED, "")
                previous, self.owner = self.owner, process
                if previous is not None and previous.returncode is None:
                    previous.stdin.close()
                task = asyncio.create_task(self._reap(process))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                retained = True
        finally:
            if process is not None and not retained:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    def close(self):
        """Release the current selection owner without blocking the UI thread."""
        self.closed = True
        if self.owner is not None and self.owner.returncode is None:
            self.owner.stdin.close()
            try:
                self.owner.terminate()
            except ProcessLookupError:
                pass

    async def aclose(self):
        """Reap retained and previously replaced owner processes."""
        self.close()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
