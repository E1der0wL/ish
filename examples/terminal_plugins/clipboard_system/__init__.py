"""Opt-in Windows/forwarded-X11 text clipboard, using Hub's native transports.

Choose this package through config.plugins in the configured plugin_dir.
TARGET can be edited before the next session: auto, windows, or x11. Selecting
one target never falls back to another desktop or to terminal escape output.
"""

import asyncio
import errno
import os

from .clipboard_wire import MAX_BYTES, TIMEOUT_SECONDS
from .x11_clipboard import X11Clipboard, read_bounded

PLUGIN_META = {"version": "1.0.0", "description": "Windows and X11 text clipboard"}
TERMINAL_API = 1
TARGET = "auto"


class SystemClipboard:
    """Fix one destination at session start; read fresh data without a text cache."""

    REMOTE_ENV = ("SSH_CONNECTION", "SSH_TTY")
    WINDOWS_ENV = ("WSL_DISTRO_NAME", "WSL_INTEROP")
    WINDOWS_READ = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        "[Console]::Write((Get-Clipboard -Raw))",
    )
    WINDOWS_WRITE = ("clip.exe",)

    def __init__(self, *, target=TARGET, env=None):
        """Select one explicit or environment-scoped target without performing I/O."""
        self.env = dict(os.environ if env is None else env)
        remote = any(self.env.get(key) for key in self.REMOTE_ENV)
        windows = not remote and any(self.env.get(key) for key in self.WINDOWS_ENV)
        if target == "auto":
            target = (
                "windows" if windows else "x11" if self.env.get("DISPLAY") else None
            )
        if target == "windows" and not windows:
            target = None
        if target == "x11" and not self.env.get("DISPLAY"):
            target = None
        self.target = target
        self.x11 = X11Clipboard(self.env) if target == "x11" else None
        self.closed = False
        self.processes = set()

    async def _windows(self, text=None):
        if text is not None and "\0" in text:
            raise OSError(errno.EINVAL, "")
        process = None
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                process = await asyncio.create_subprocess_exec(
                    *(self.WINDOWS_READ if text is None else self.WINDOWS_WRITE),
                    stdin=asyncio.subprocess.DEVNULL
                    if text is None
                    else asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE
                    if text is None
                    else asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self.env,
                )
                self.processes.add(process)
                if text is None:
                    data = await read_bounded(process.stdout)
                else:
                    process.stdin.write(text.encode("utf-16-le"))
                    await process.stdin.drain()
                    process.stdin.close()
                if await process.wait() != 0:
                    raise OSError(errno.EIO, "")
                if text is None:
                    return data.decode("utf-8").replace("\r\n", "\n")
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                self.processes.discard(process)

    async def read(self):
        """Read fresh system text from the selected target, or fail explicitly."""
        if self.closed:
            raise OSError(errno.ECANCELED, "")
        if self.target == "windows":
            return await self._windows()
        if self.x11 is not None:
            return await self.x11.transfer()
        raise OSError(errno.ENOSYS, "")

    async def write(self, text):
        """Publish bounded Unicode text without choosing another clipboard on failure."""
        if self.closed:
            raise OSError(errno.ECANCELED, "")
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise OSError(errno.EFBIG, "")
        if self.target == "windows":
            await self._windows(text)
        elif self.x11 is not None:
            await self.x11.transfer(text)
        else:
            raise OSError(errno.ENOSYS, "")

    def close(self):
        """Stop accepting requests and release any owned OS processes."""
        self.closed = True
        for process in self.processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        if self.x11 is not None:
            self.x11.close()

    async def aclose(self):
        """Wait until all retained native owners and Windows helpers have exited."""
        self.close()
        if self.processes:
            await asyncio.gather(*(process.wait() for process in self.processes))
        if self.x11 is not None:
            await self.x11.aclose()


def setup(api):
    """Register transport only; the broker retains permission and request ownership."""
    api.register_clipboard("system", factory=SystemClipboard)
