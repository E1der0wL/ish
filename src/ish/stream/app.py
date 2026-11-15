"""Serialize direct terminal output and optional primary-prompt PTK panels."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import secrets
import signal
import tempfile
import termios
import tty
from pathlib import Path

from ish.lang import Message, i18n
from ish.log import DEFAULT_LOGGER_NAME
from ish.runtime.fdio import FDWriter
from ish.terminal.session import Session

from .handoff import Request, Stage
from .protocol import Event, Keys, Reply, Stream
from .settings import ESCAPE_DELAY, validate_escape_delay

READ_BYTES = 65536
PANEL_ROWS = 5
MIN_ROWS = PANEL_ROWS + 3
MIN_COLUMNS = 20
REQUEST_BYTES = 1024
PENDING_BYTES = 65536
NONCE_BYTES = 16
SAVE = b"\x1b7"
RESTORE = b"\x1b8"
RESET_MARGIN = b"\x1b[r"
INDEX = b"\x1bD"


def position(row: int, column: int = 1) -> bytes:
    """Address a host cell using one-based coordinates."""
    return f"\x1b[{row};{column}H".encode("ascii")


class App:
    """Own one byte relay; PTK never writes to the external terminal itself."""

    def __init__(
        self,
        argv,
        cwd,
        *,
        integration_dir=None,
        plugins=(),
        plugin_dir=None,
        library_dir=None,
        logger=None,
        report=None,
        escape_delay=ESCAPE_DELAY,
    ):
        """Keep unadorned streaming independent of shell integration and PTK UI."""
        self.argv, self.cwd = argv, cwd
        self.escape_delay = validate_escape_delay(escape_delay)
        self.logger = logger or logging.getLogger(DEFAULT_LOGGER_NAME)
        self.report = report
        self.integration_dir = integration_dir
        self.plugin_names = tuple(plugins)
        self.plugin_dir, self.library_dir = plugin_dir, library_dir
        self.extensions = None
        self.plugin_errors = []
        self.ui_enabled = True
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
        self.reusable = False

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

    def _child_exited(self, status):
        """Retire input and popup focus while Session drains the child's final output."""
        self.logger.debug("Child exit status=%s; draining output", status)
        self.stopping = True
        self.loop.remove_reader(0)
        if self.key_timer:
            self.key_timer.cancel()
            self.key_timer = None
        if self.request:
            self.request.pending.clear()
            self.request.cancel()
        # Closing the popup releases only its own output pause. A slow host's
        # writer keeps backpressure until it can accept the remaining PTY bytes.
        self._hide()
        self._release_request()

    def _event(self, kind, serial):
        self.logger.debug("Shell event kind=%s serial=%s", kind, serial)
        if self.stopping:
            return
        if kind == Event.HIDE:
            self._hide()
            self._release_request()
        elif kind == Event.BOUNDARY:
            if self.request:
                self.request.bind(serial)
        elif kind == Event.PRIMARY:
            self._hide()
            if not self.ui_enabled:
                self._release_request(primary=True)
                return
            self.eligible = True
            if self.rows < MIN_ROWS or self.cols < MIN_COLUMNS:
                self._release_request(primary=True)
                return
            self._reserve()
            self._release_request(primary=True)

    def _reserve(self):
        bottom = self.rows - PANEL_ROWS
        up = f"\x1b[{PANEL_ROWS}A".encode("ascii")
        if self.reusable:
            # The previous panel was cleared and subsequent output has not
            # moved back above existing content. Advance from the real cursor:
            # the host scrolls only when these rows do not fit below it.
            data = SAVE + RESET_MARGIN + RESTORE + INDEX * PANEL_ROWS + up
            data += SAVE + f"\x1b[1;{bottom}r".encode("ascii") + RESTORE
        else:
            # Unknown layout/images require fresh blank rows, not an overlay
            # over content whose geometry this byte relay does not own.
            data = SAVE + RESET_MARGIN + position(self.rows) + INDEX * PANEL_ROWS
            data += RESTORE + up + SAVE
            data += f"\x1b[1;{bottom}r".encode("ascii") + RESTORE
        self._write(data)
        self.active = True
        self.panel.show()

    def _rewind(self):
        if not self.active:
            self.reusable = False

    def _unsafe(self):
        self._hide()
        self.reusable = False

    def _cleared(self):
        self.reusable = True

    def _forward(self, data):
        # BS can cross a row under a persistent host reverse-wrap mode.
        if not self.active and any(byte in data for byte in (0x08, 0x0B, 0x0C)):
            self.reusable = False
        self._write(data)

    def _release_request(self, *, primary=False):
        request, self.request = self.request, None
        if request:
            if request.serial is not None and request.stage == Stage.WAITING:
                # A rejected native popup must not turn its text into a shell
                # command when the native editor redraws after cancellation.
                request.pending.clear()
            if not primary or not request.complete():
                request.cancel()
            self.logger.debug(
                "Popup request stage=%s serial=%s", request.stage, request.serial
            )
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
        answer = Reply.CANCEL
        owned = None
        disconnected = None
        try:
            payload = json.loads(await reader.readline())
            if not isinstance(payload, dict):
                return
            request = self.request
            if (
                not request
                or payload.get("nonce") != self.nonce
                or request.stage != Stage.WAITING
            ):
                return
            disconnected = asyncio.create_task(reader.read(1))
            if not await self._while_connected(request.ready, disconnected):
                return
            if (
                request is not self.request
                or not self.active
                or not self.stream.boundary
            ):
                return
            if not request.claim(payload.get("serial")):
                return
            owned = request
            try:
                opened = self.panel.open_popup()
            except Exception as error:
                self._ui_error(error)
                opened = False
            if not opened:
                request.pending.clear()
                self._hide()
                self._release_request()
                return
            self._pause_input("request", False)
            buffered = bytes(request.pending)
            request.pending.clear()
            if buffered:
                self.panel.feed(buffered)
            if (
                await self._while_connected(request.finished, disconnected)
                and request.stage == Stage.RETURNING
                and self.ui_enabled
            ):
                answer = Reply.DONE
            else:
                request.cancel()
                if request is self.request and self.popup:
                    self.panel.close_popup()
        except (ValueError, OSError):
            if owned:
                owned.cancel()
        finally:
            if disconnected:
                disconnected.cancel()
            try:
                writer.write(answer)
                if answer == Reply.DONE and owned:
                    # write() queued the response; drain() may yield while the
                    # native helper consumes it and emits the next primary.
                    owned.reply_queued()
                await writer.drain()
            except (OSError, ConnectionError):
                if owned:
                    owned.cancel()
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
        self.reusable = True
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
        self.logger.debug("Popup focus=%s", popup)
        if popup:
            self._pause("popup", True)
            self._write(SAVE)
            self.popup = True
        else:
            self.popup = False
            self._write(RESTORE)
            self._pause("popup", False)
            if self.request:
                self.request.begin_return()

    def _ui_error(self, error):
        """Retire faulty UI without giving popup text to the native shell."""
        if not self.ui_enabled:
            return
        self.ui_enabled = False
        self.plugin_errors.append(str(error))
        self.logger.error(i18n.get(Message.STREAM_UI_ERROR, error=error))
        self.loop.call_soon(self._disable_ui)

    def _disable_ui(self):
        if self.stopping:
            return
        if self.request:
            self.request.pending.clear()
        self._hide()
        self._release_request()
        if self.keys:
            # A partial Escape/F2 sequence still belongs to native input; it is
            # separate from text held for a granted or pending popup request.
            self.keys.flush()
        self.keys = None
        if self.key_timer:
            self.key_timer.cancel()
            self.key_timer = None

    def _open_popup(self, raw):
        if (
            not self.ui_enabled
            or not self.active
            or not self.eligible
            or not self.stream.boundary
            or self.request
        ):
            return False
        # F2 still goes to the shell. Only its native B + socket request can
        # grant popup focus; a stale primary observation never grants it.
        self.request = Request()
        self.session.send(raw)
        return True

    def _native_input(self, data):
        if self.popup:
            self.panel.feed(data)
        elif self.request:
            self.request.pending.extend(data)
            if len(self.request.pending) >= PENDING_BYTES:
                self._pause_input("request", True)
        else:
            # This early hide is cosmetic. The ordered H hook is authoritative
            # for other bindings, queued paste and commands already in the shell.
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
            elif self.keys:
                self._route_keys(data)
            elif self.stream:
                self._native_input(data)
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
            self.key_timer = self.loop.call_later(self.escape_delay, self.keys.flush)

    def _output(self, data):
        if self.stream:
            self.stream.feed(data)
            if self.pending_frame and self.stream.boundary:
                self._paint(self.frame)
        else:
            self._write(data)

    def _resize(self):
        self._hide()
        self.reusable = False
        size = os.get_terminal_size(1)
        self.cols, self.rows = size.columns, size.lines
        self.session.resize(self.cols, self.rows)
        self.logger.debug("Terminal resize columns=%s rows=%s", self.cols, self.rows)
        if self.panel:
            self.panel.resize(max(1, self.cols), PANEL_ROWS)
        # A resize revokes the current decoration. It returns at the next
        # confirmed primary rather than guessing when native redisplay finished.

    async def _prepare(self, argv, env):
        """Prepare optional plugins before starting the one native shell session."""
        if not self.plugin_names:
            return argv, env
        from .extensions import Extensions
        from .panel import Panel
        from .shells import Integration

        self.extensions = Extensions()
        try:
            self.extensions.load(
                self.plugin_names,
                directory=self.plugin_dir,
                library_dir=self.library_dir,
            )
            if not self.extensions.has_ui:
                return argv, env
            build = self.extensions.select_shell(argv)
            nonce = secrets.token_hex(NONCE_BYTES)
            socket_path = None
            if self.extensions.popup is not None:
                self.socket_dir = tempfile.TemporaryDirectory(prefix="ish-popup-")
                socket_path = str(Path(self.socket_dir.name, "control"))
            self.integration = Integration(
                argv,
                env,
                nonce=nonce,
                popup_socket=socket_path,
                script_dir=self.integration_dir,
                build=build,
            )
            self.panel = Panel(
                self._paint,
                on_focus=self._focus,
                on_remainder=self._native_input,
                bar=self.extensions.bar,
                popup=self.extensions.popup,
                on_error=self._ui_error,
                escape_delay=self.escape_delay,
            )
            await self.panel.start(max(1, self.cols), PANEL_ROWS)
            if self.panel.error is not None:
                raise self.panel.error
            if socket_path is not None:
                self.server = await asyncio.start_unix_server(
                    self._popup_client, socket_path, limit=REQUEST_BYTES
                )
                self.keys = Keys(self._native_input, self._open_popup)
            self.nonce = nonce
            self.stream = Stream(
                nonce,
                self._forward,
                self._event,
                self._unsafe,
                rewind=self._rewind,
                cleared=self._cleared,
            )
            return self.integration.argv, self.integration.env
        except (Exception, SystemExit) as error:
            # No shell has started, so fallback cannot repeat a user command or
            # leave a partially sourced hook installed in a running shell.
            if self.panel:
                await self.panel.close()
                self.panel = None
            if self.server:
                self.server.close()
                await self.server.wait_closed()
                self.server = None
            if self.integration:
                self.integration.close()
                self.integration = None
            if self.socket_dir:
                self.socket_dir.cleanup()
                self.socket_dir = None
            for cleanup_error in await self.extensions.close():
                self.logger.error(
                    i18n.get(Message.STREAM_CLEANUP_ERROR, error=cleanup_error)
                )
            self.stream = self.keys = None
            self.logger.warning(i18n.get(Message.STREAM_FALLBACK, error=error))
            return argv, env

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
            argv, env = await self._prepare(argv, env)
            self.logger.info(
                "Session start shell=%s plugins=%s",
                Path(argv[0]).name,
                len(self.plugin_names),
            )
            if self.report:
                self.report()
            self.session = Session(
                argv,
                self.cwd,
                self._output,
                self.stop,
                self._failed,
                env=env,
                input_flow=lambda paused: self._pause_input("native", paused),
                child_exited=self._child_exited,
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
            self.logger.info("Session exit status=%s", result)
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
                if self.extensions:
                    for error in await self.extensions.close():
                        self.logger.error(
                            i18n.get(Message.STREAM_CLEANUP_ERROR, error=error)
                        )

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
