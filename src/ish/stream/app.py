"""Serialize direct terminal output and optional primary-prompt PTK panels."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import signal
import tempfile
import termios
import tty
from dataclasses import dataclass, field
from pathlib import Path

from ish.runtime.fdio import FDWriter
from ish.terminal.session import Session

from .protocol import Keys, Stream

READ_BYTES = 65536
PANEL_ROWS = 5
MIN_ROWS = PANEL_ROWS + 3
MIN_COLUMNS = 20
ESCAPE_DELAY = 0.05
REQUEST_BYTES = 1024
PENDING_READS = 65536
SAVE = b"\x1b7"
RESTORE = b"\x1b8"
RESET_MARGIN = b"\x1b[r"


@dataclass
class Request:
    """Correlate a forwarded shortcut, native widget boundary and private ACK."""

    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    pending: bytearray = field(default_factory=bytearray)
    serial: int | None = None
    stage: str = "waiting"
    claimed: bool = False


def position(row: int, column: int = 1) -> bytes:
    """Address a host cell using one-based coordinates."""
    return f"\x1b[{row};{column}H".encode("ascii")


class App:
    """Own one byte relay; PTK never writes to the external terminal itself."""

    def __init__(self, argv, cwd, *, demo=False):
        """Keep unadorned streaming independent of shell integration and PTK UI."""
        self.argv, self.cwd, self.demo = argv, cwd, demo
        self.session = self.writer = self.panel = self.integration = None
        self.stream = self.keys = None
        self.loop = self.done = None
        self.active = self.popup = self.eligible = False
        self.cols = self.rows = 0
        self.frame = None
        self.pauses = set()
        self.input_pauses = set()
        self.key_timer = None
        self.error = None
        self.stopping = False
        self.pending_frame = False
        self.request = None
        self.nonce = None
        self.server = self.socket_dir = None
        self.clients = set()

    def _write(self, data):
        if data:
            self.writer.write(data)

    def _pause(self, reason, paused):
        if paused:
            self.pauses.add(reason)
        else:
            self.pauses.discard(reason)
        if self.session:
            self.session.set_output_enabled(not self.pauses)

    def _failed(self, error):
        self.error = error
        self.stop(1)

    def _pause_input(self, reason, paused):
        if paused:
            self.input_pauses.add(reason)
        else:
            self.input_pauses.discard(reason)
        if self.input_pauses or self.stopping:
            self.loop.remove_reader(0)
        else:
            self.loop.add_reader(0, self._input)

    def stop(self, status=0):
        """Request normal asynchronous teardown without dropping the PTY owner."""
        if self.done is not None and not self.done.done():
            self.done.set_result(status)

    def _event(self, kind, serial):
        if self.stopping:
            return
        if kind == "H":
            self._hide()
            self._release_request()
        elif kind == "B":
            if self.request and self.request.stage == "waiting":
                self.request.serial = serial
                self.request.ready.set()
        elif kind == "P":
            self._hide()
            self.eligible = True
            if self.rows < MIN_ROWS or self.cols < MIN_COLUMNS:
                self._release_request()
                return
            bottom = self.rows - PANEL_ROWS
            # Make fresh blank rows by normal host scrolling before reserving
            # them. Existing output/images are never painted over by the panel.
            self._write(
                SAVE
                + RESET_MARGIN
                + position(self.rows)
                + b"\n" * PANEL_ROWS
                + f"\x1b[1;{bottom}r".encode("ascii")
                + RESTORE
                + f"\x1b[{PANEL_ROWS}A".encode("ascii")
            )
            self.active = True
            self.session.resize(self.cols, bottom)
            self.panel.show()
            self._release_request()

    def _release_request(self):
        request, self.request = self.request, None
        if request:
            if request.serial is not None and request.stage == "waiting":
                # A rejected native popup must not turn its text into a shell
                # command when the native editor redraws after cancellation.
                request.pending.clear()
            request.stage = "cancelled"
            request.ready.set()
            request.finished.set()
            if not self.stopping:
                self._pause_input("request", False)
                # These bytes were never offered to the native shell before.
                buffered = bytes(request.pending)
                if self.keys and self.active:
                    self._route_keys(buffered)
                else:
                    self._native_input(buffered)

    async def _popup_client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        answer = b"cancel\n"
        disconnected = None
        try:
            payload = json.loads(await reader.readline())
            if not isinstance(payload, dict):
                return
            request = self.request
            if not request or payload.get("nonce") != self.nonce or request.claimed:
                return
            disconnected = asyncio.create_task(reader.read(1))
            if not await self._while_connected(request.ready, disconnected):
                return
            if (
                request is not self.request
                or request.stage != "waiting"
                or payload.get("serial") != request.serial
                or not self.active
                or not self.stream.boundary
            ):
                return
            if request.claimed or type(payload.get("serial")) is not int:
                return
            request.claimed = True
            request.stage = "popup"
            if not self.panel.open_popup():
                return
            self._pause_input("request", False)
            buffered = bytes(request.pending)
            request.pending.clear()
            if buffered:
                self.panel.feed(buffered)
            if await self._while_connected(request.finished, disconnected):
                answer = b"done\n"
            elif request is self.request and self.popup:
                self.panel.close_popup()
        except (ValueError, OSError):
            pass
        finally:
            if disconnected:
                disconnected.cancel()
            try:
                writer.write(answer)
                await writer.drain()
            except (OSError, ConnectionError):
                pass
            writer.close()
            self.clients.discard(task)

    @staticmethod
    async def _while_connected(event, disconnected):
        ready = asyncio.create_task(event.wait())
        try:
            await asyncio.wait(
                (ready, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            return ready.done() and not disconnected.done()
        finally:
            ready.cancel()

    def _hide(self):
        self.eligible = False
        if not self.active:
            return
        self.panel.hide()
        self.active = False
        clear = b"".join(
            position(row) + b"\x1b[0m\x1b[2K"
            for row in range(self.rows - PANEL_ROWS + 1, self.rows + 1)
        )
        self._write(SAVE + clear + RESET_MARGIN + RESTORE)
        self.session.resize(self.cols, self.rows)
        self.pending_frame = False

    def _paint(self, frame):
        self.frame = frame
        if not self.active or self.stopping:
            return
        if self.stream and not self.stream.boundary:
            self.pending_frame = True
            return
        self.pending_frame = False
        data = frame.render(self.rows - PANEL_ROWS + 1)
        # While a popup owns input, SAVE contains the shell cursor. Do not
        # replace that slot with the popup cursor during subsequent redraws.
        self._write(data if self.popup else SAVE + data + RESTORE)

    def _focus(self, popup):
        if popup:
            self._pause("popup", True)
            self._write(SAVE)
            self.popup = True
        else:
            self.popup = False
            self._write(RESTORE)
            self._pause("popup", False)
            if self.request and self.request.stage == "popup":
                self.request.stage = "returning"
                self.request.finished.set()

    def _open_popup(self, raw):
        if (
            not self.active
            or not self.eligible
            or not self.stream.boundary
            or self.request
        ):
            return False
        # F2 still goes to tcsh. Only its native editor's B + socket request can
        # grant popup focus; a stale primary observation never grants it.
        self.request = Request()
        self.session.send(raw)
        return True

    def _native_input(self, data):
        if self.popup:
            self.panel.feed(data)
        elif self.request:
            self.request.pending.extend(data)
            if len(self.request.pending) >= PENDING_READS:
                self._pause_input("request", True)
        else:
            # This early hide is cosmetic. The ordered H hook is authoritative
            # for other bindings, queued paste and commands already in tcsh.
            if any(byte in data for byte in (3, 4, 10, 13)):
                self._hide()
            self.session.send(data)

    def _input(self):
        if self.input_pauses:
            return
        try:
            data = os.read(0, READ_BYTES)
            if not data:
                self.stop(0)
                return
            if self.popup:
                self.panel.feed(data)
            elif self.request:
                if data == b"\x03":
                    # Cancelling a widget wait discards only its unsubmitted
                    # input, then lets the native shell handle the interrupt.
                    self.request.pending.clear()
                    self._hide()
                    self._release_request()
                    self.session.send(data)
                else:
                    self._native_input(data)
            elif self.demo:
                self._route_keys(data)
            else:
                self.session.send(data)
        except BlockingIOError:
            pass
        except Exception as exc:
            self._failed(exc)

    def _route_keys(self, data):
        if self.key_timer:
            self.key_timer.cancel()
        self.keys.feed(data)
        # This resolves the native Escape/F2 ambiguity only, never readiness.
        if self.keys.pending:
            self.key_timer = self.loop.call_later(ESCAPE_DELAY, self.keys.flush)

    def _output(self, data):
        if self.stream:
            self.stream.feed(data)
            if self.pending_frame and self.stream.boundary:
                self._paint(self.frame)
        else:
            self._write(data)

    def _resize(self):
        self._hide()
        size = os.get_terminal_size(1)
        self.cols, self.rows = size.columns, size.lines
        self.session.resize(self.cols, self.rows)
        if self.panel:
            self.panel.resize(max(1, self.cols), PANEL_ROWS)
        # A resize revokes the current decoration. It returns at the next
        # confirmed primary rather than guessing when native redisplay finished.

    async def run(self):
        """Relay a controlling PTY and restore all outer modes changed by ish."""
        self.loop = asyncio.get_running_loop()
        self.done = self.loop.create_future()
        saved_tty = termios.tcgetattr(0)
        saved_flags = {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in (0, 1)}
        size = os.get_terminal_size(1)
        self.cols, self.rows = size.columns, size.lines
        argv, env = self.argv, dict(os.environ)
        previous_signals = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            if self.demo:
                from .panel import Panel
                from .shells import Integration

                nonce = self.nonce = secrets.token_hex(16)
                self.socket_dir = tempfile.TemporaryDirectory(prefix="ish-popup-")
                socket_path = str(Path(self.socket_dir.name, "control"))
                self.server = await asyncio.start_unix_server(
                    self._popup_client, socket_path, limit=REQUEST_BYTES
                )
                self.integration = Integration(
                    argv, env, nonce=nonce, popup_socket=socket_path
                )
                argv, env = self.integration.argv, self.integration.env
                self.panel = Panel(
                    self._paint,
                    on_focus=self._focus,
                    on_remainder=self._native_input,
                )
                await self.panel.start(max(1, self.cols), PANEL_ROWS)
                self.stream = Stream(nonce, self._write, self._event, self._hide)
                self.keys = Keys(self._native_input, self._open_popup)
            self.session = Session(
                argv,
                self.cwd,
                self._output,
                self.stop,
                self._failed,
                env=env,
                input_flow=lambda paused: self._pause_input("native", paused),
            )
            self.session.start(self.cols, self.rows)
            tty.setraw(0, termios.TCSANOW)
            os.set_blocking(0, False)
            os.set_blocking(1, False)
            self.writer = FDWriter(
                self.loop,
                1,
                self._failed,
                on_flow=lambda paused: self._pause("host", paused),
            )
            self.loop.add_reader(0, self._input)
            for signum in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
                callback = (
                    self._resize
                    if signum == signal.SIGWINCH
                    else lambda signum=signum: self.stop(128 + signum)
                )
                self.loop.add_signal_handler(signum, callback)
            result = await self.done
            return result
        finally:
            try:
                await self._close()
            finally:
                for signum, handler in previous_signals.items():
                    self.loop.remove_signal_handler(signum)
                    signal.signal(signum, handler)
                for fd, flags in saved_flags.items():
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                termios.tcsetattr(0, termios.TCSANOW, saved_tty)
                if self.integration:
                    self.integration.close()
                if self.socket_dir:
                    self.socket_dir.cleanup()

    async def _close(self):
        self.stopping = True
        self._release_request()
        self.loop.remove_reader(0)
        if self.key_timer:
            self.key_timer.cancel()
        try:
            if self.writer:
                self._hide()
        finally:
            try:
                if self.panel:
                    await self.panel.close()
            finally:
                if self.server:
                    self.server.close()
                    await self.server.wait_closed()
                clients = tuple(self.clients)
                for task in clients:
                    task.cancel()
                if clients:
                    await asyncio.gather(*clients, return_exceptions=True)
                if self.session:
                    await self.session.close()
        if self.stream and self.writer:
            self.stream.finish()
        if self.writer:
            try:
                await self.writer.drain()
            finally:
                self.writer.close()
