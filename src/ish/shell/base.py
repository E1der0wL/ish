"""Manage input ownership and output ordering between a PTY shell and prompt-toolkit.

Forward input to the running command, then return it to the editor after receiving the
primary prompt and state frame. Tie acquired FDs, terminal settings, and temporary files
to the session lifetime.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import itertools
import os
import platform
import pty
import shutil
import signal
import struct
import sys
import tempfile
import termios
import tty
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from ish.fdio import FDWriter

from .adapters import get_adapter
from .constants import FORWARD_BINARY, PROMPT_ID, SHELL_FIFO, TTY_FIFO, SessionSignals
from .integration import build_binary, install_scripts
from .output_line import OutputLine
from .protocol import FrameDecoder
from .sequencer import Sequencer
from .signal import ShellExitRequest, ShellPassRequest

if TYPE_CHECKING:
    import types
    from asyncio import AbstractEventLoop
    from ish.ui.prompt import Prompt

__all__ = ["InteractiveShell"]


class ScrollBack:
    """Split the payload byte budget equally between history and last output.

    Discard the oldest bytes first. UTF-8 cuts display replacement characters;
    truncation flags let consumers distinguish a retained tail from full output.
    """

    def __init__(self, max_lines=10000, max_bytes=10 * 1024 * 1024, encoder="utf-8"):
        """Validate line and byte budgets and prepare history and last-output buffers."""
        if max_lines < 0 or max_bytes < 0:
            raise ValueError("Scrollback limits must be nonnegative")
        self.max_lines, self.max_bytes, self.encoder = max_lines, max_bytes, encoder
        self._buffer = deque()
        self._current_bytes = 0
        self._partial_line = bytearray()
        self._last_output = bytearray()
        self.history_truncated = self.last_output_truncated = False

    def __len__(self):
        """Return the number of complete history lines currently retained."""
        return len(self._buffer)

    @property
    def retained_bytes(self):
        """Return payload bytes retained in history, the partial line, and last output."""
        return self._current_bytes + len(self._partial_line) + len(self._last_output)

    @property
    def last_output(self):
        """Decode the last command output, replacing characters cut at byte boundaries."""
        return self._last_output.decode(self.encoder, errors="replace")

    @last_output.setter
    def last_output(self, value):
        """Replace the last output or clear it when given None."""
        self._last_output.clear()
        self.last_output_truncated = False
        if value is not None:
            self.append_ld(
                value.encode(self.encoder) if isinstance(value, str) else value
            )

    def _trim_history(self):
        """Discard the oldest history to meet line and history-byte budgets."""
        limit = self.max_bytes // 2
        while self._buffer and (
            len(self._buffer) > self.max_lines
            or self._current_bytes + len(self._partial_line) > limit
        ):
            self._current_bytes -= len(self._buffer.popleft())
            self.history_truncated = True
        if len(self._partial_line) > limit:
            del self._partial_line[: len(self._partial_line) - limit]
            self.history_truncated = True

    def _add_line(self, line):
        """Store a complete line within the budget and trim existing history as needed."""
        limit = self.max_bytes // 2
        if not limit or not self.max_lines:
            self.history_truncated |= bool(line)
            return
        if len(line) > limit:
            line = line[-limit:]
            self.history_truncated = True
        self._buffer.append(bytes(line))
        self._current_bytes += len(line)
        self._trim_history()

    def append(self, data):
        """Accumulate output by line and retain an incomplete tail for the next read."""
        if not data:
            return
        limit = self.max_bytes // 2
        if not limit or not self.max_lines:
            self.history_truncated = True
            return
        if len(data) > limit:
            data = data[-limit:]
            self._partial_line.clear()
            self.history_truncated = True
        parts = (self._partial_line + data).split(b"\n")
        self._partial_line.clear()
        for part in parts[:-1]:
            self._add_line(part + b"\n")
        self._partial_line.extend(parts[-1])
        self._trim_history()

    def append_ld(self, data):
        """Retain the tail of the last command output within its byte budget."""
        limit = self.max_bytes - self.max_bytes // 2
        if len(self._last_output) + len(data) > limit:
            self.last_output_truncated = True
        if not limit:
            return
        self._last_output.extend(data[-limit:])
        del self._last_output[: max(0, len(self._last_output) - limit)]

    def get_lines(self, count=None):
        """Return the last count complete lines, or all complete lines, as strings."""
        start = 0 if count is None else max(0, len(self._buffer) - max(0, count))
        return [
            line.decode(self.encoder, errors="replace")
            for line in itertools.islice(self._buffer, start, None)
        ]

    def clear(self):
        """Clear history, last output, the partial line, and truncation flags."""
        self._buffer.clear()
        self._partial_line.clear()
        self._last_output.clear()
        self._current_bytes = 0
        self.history_truncated = self.last_output_truncated = False


class InteractiveShell:
    """Asynchronous session coordinating a real shell process, PTY, FIFOs, and editor.

    Adapters define shell policies. Forward raw output to the terminal and transfer
    input ownership or repair integration only at a confirmed primary prompt boundary.
    """

    __slots__ = (
        "encoder",
        "chunk_size",
        "stdin_fd",
        "stdout_fd",
        "stderr_fd",
        "master_fd",
        "slave_fd",
        "dupin_fd",
        "exec_attrs",
        "shell_pipe_path",
        "shell_pipe",
        "tty_pipe_path",
        "tty_pipe",
        "shell_path",
        "shell",
        "shell_args",
        "shell_pid",
        "_environ",
        "_variable",
        "_alias",
        "xdg_home",
        "shell_integration",
        "init_command",
        "last_command",
        "prompt_event",
        "command_start_event",
        "command_done_event",
        "continuation_active",
        "loop",
        "proc",
        "tasks",
        "cleanup_handlers",
        "message",
        "shell_pipe_buffer",
        "shell_temp_buffer",
        "dupin_buffer",
        "scroll_back",
        "sequencer",
        "session",
        "_writers",
        "_fatal_error",
        "_initializing",
        "_init_output",
        "_init_event",
        "_frame_decoder",
        "adapter",
        "_context_id",
        "_prompt_id",
        "_context_event",
        "refresh_command",
        "_native_output_line",
        "_prompt_prefix",
        "_unhooked_prompt",
        "signals",
        "context_timeout",
        "_output_buffer",
        "_output_task",
        "_output_paused",
        "_closing_output",
    )

    def __init__(
        self,
        shell: str = "bash",
        encoder: str = sys.stdout.encoding or "utf-8",
        stdin: int = sys.stdin.fileno(),
        stdout: int = sys.stdout.fileno(),
        stderr: int = sys.stderr.fileno(),
        prompt: Prompt = None,
        chunk: int = 1024 * 1024,
    ):
        """Prepare the shell adapter and buffers; main opens descriptors and starts
        processes.
        """
        if chunk <= 0:
            raise ValueError("PTY read size must be positive")
        self.encoder: str = encoder
        self.chunk_size: int = chunk

        self.stdin_fd: int = stdin
        self.stdout_fd: int = stdout
        self.stderr_fd: int = stderr

        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.dupin_fd: Optional[int] = None

        self.exec_attrs: Optional[List[Union[int, List[int]]]] = None

        self.shell_pipe_path: Optional[str] = None
        self.shell_pipe: Optional[int] = None
        self.tty_pipe_path: Optional[str] = None
        self.tty_pipe: Optional[int] = None

        if os.path.sep in shell:
            self.shell_path = os.path.abspath(shell)
        else:
            self.shell_path = shutil.which(shell)
        shell_name: str = ""
        if self.shell_path:
            shell_name = os.path.basename(self.shell_path)

        self.adapter = get_adapter(shell_name or shell, self.shell_path)
        self.shell = self.adapter.name
        self.shell_args = self.adapter.args
        self.refresh_command = b""
        self.signals = SessionSignals.create()
        self.context_timeout = 5.0
        self._output_buffer = bytearray()
        self._output_task = None
        self._output_paused = False
        self._closing_output = False
        self._context_id = self._prompt_id = 0
        self._context_event = asyncio.Event()
        self._native_output_line = OutputLine()
        self._prompt_prefix = b""
        self._unhooked_prompt = False

        self.shell_pid: int = -1

        self._environ: Dict[str, str] = os.environ.copy()
        self._variable: Dict[str, List[str]] = {}
        self._alias: Dict[str, str] = {}

        self.xdg_home: Path = Path()
        self.shell_integration: str = ""

        self.init_command: bytes = b""
        self.last_command: str = ""

        self.prompt_event: Optional[asyncio.Event] = None
        self.command_start_event: Optional[asyncio.Event] = None
        self.command_done_event: Optional[asyncio.Event] = None
        self.continuation_active: Optional[asyncio.Event] = None

        self.loop: Optional[AbstractEventLoop] = None
        self.proc: Optional[asyncio.subprocess.Process] = None

        self.tasks: Optional[List[asyncio.Task]] = None
        self.cleanup_handlers: List[Callable[[], Any]] = []
        self.message: Union[bytes, str] = b""
        self._frame_decoder = FrameDecoder()
        self.shell_pipe_buffer = self._frame_decoder.buffer
        self.shell_temp_buffer: bytearray = bytearray()
        self.dupin_buffer: bytearray = bytearray()

        self.scroll_back: ScrollBack = ScrollBack(
            max_lines=10000, max_bytes=10 * 1024 * 1024, encoder=self.encoder
        )

        self.sequencer: Optional[Sequencer] = None

        self.session: Prompt = prompt
        self._writers = {}
        self._fatal_error = None
        self._initializing = False
        self._init_output = bytearray()
        self._init_event = None

    @staticmethod
    def _sigwinch(fd, col, row, xpix=0, ypix=0) -> None:
        """Set the PTY window size in rows and columns."""
        try:
            win_size = struct.pack("HHHH", row, col, xpix, ypix)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, win_size)
        except OSError:
            return

    def _signal_handler(self, signum: int, frame: Optional[types.FrameType]) -> None:
        """Handle SIGWINCH by matching the internal PTY size to the current terminal."""
        if signum == signal.SIGWINCH:
            self._sigwinch(self.master_fd, *shutil.get_terminal_size())

    def _set_prompt(self, prompt: bytes) -> None:
        # Stop forwarding as soon as the primary boundary is parsed, before
        # awaiting context or running any integration maintenance commands.
        """Store the primary prompt and stop forwarding raw input.

        Freeze the output prefix at this boundary; state FIFO synchronization uses a
        separate wait.
        """
        if self.loop is not None and self.dupin_fd is not None:
            self.loop.remove_reader(self.dupin_fd)
        self.command_done_event.set()
        self.continuation_active.clear()
        self.message = prompt
        if not self._initializing:
            self._prompt_prefix = self._native_output_line.prefix
        self.session.set_prompt(self._prompt_prefix + prompt, self.encoder)
        self.prompt_event.set()
        if self._initializing:
            self._init_event.set()

    def _set_prompt_id(self, data: bytes) -> bool:
        """Accept only valid, increasing session IDs and pass invalid signals through."""
        if not data or len(data) > 19 or not data.isdigit():
            return False
        value = int(data)
        if not self._prompt_id < value < 2**63:
            return False
        self._prompt_id = value
        return True

    def _set_unhooked_prompt(self, prompt: bytes) -> None:
        # After an interrupt tcsh can skip precmd and show the primary
        # markers as caret notation with its secondary editor still enabled.
        """Record a caret-form primary prompt and schedule recovery at that boundary."""
        self._unhooked_prompt = True
        self._set_prompt(prompt)

    async def _wait_context(self) -> None:
        """Wait within a deadline for the state FIFO to reach the current prompt ID."""
        while self._context_id < self._prompt_id:
            self._context_event.clear()
            try:
                await asyncio.wait_for(self._context_event.wait(), self.context_timeout)
            except TimeoutError:
                raise RuntimeError(
                    "Shell context was not received after a confirmed prompt"
                ) from None

    def _set_continuation(self, prompt: bytes) -> Optional[bytes]:
        """Keep continuation input with the shell or pass it to the editor per adapter
        policy.
        """
        self.continuation_active.set()
        if self.adapter.behavior.native_continuation:
            # The shell may already have read the complete foreach body into
            # its own buffer. Keep one input owner until the primary prompt.
            # Returning bytes preserves the prompt's position in the PTY output.
            return prompt
        self.session.set_prompt(prompt, self.encoder)
        # A custom UI continuation policy must not flush or replay shell input.
        self.prompt_event.set()

    def _pre_input(self, fd: int) -> Optional[bool]:
        """Collect forwarded typeahead in a bounded buffer and report whether data was
        read.
        """
        try:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            if len(self.dupin_buffer) + len(chunk) > 1024 * 1024:
                self._io_failed(BufferError("Pending typeahead exceeds 1 MiB"))
                return
            self.dupin_buffer.extend(chunk)
            return True
        except (BlockingIOError, OSError):
            return

    def _read(self, fd: int) -> bytes:
        """Read one chunk from an FD, returning empty bytes if nothing is currently
        readable.
        """
        try:
            data = os.read(fd, self.chunk_size)
        except (BlockingIOError, OSError):
            data = b""
        return data

    def _io_failed(self, exc: Exception) -> None:
        """Wake the main session waiter with the first I/O error."""
        if self._fatal_error is not None and not self._fatal_error.done():
            self._fatal_error.set_result(exc)

    def _writer(self, fd: int) -> FDWriter:
        """Create or reuse a writer that preserves output order for each FD."""
        if fd not in self._writers:
            self._writers[fd] = FDWriter(self.loop, fd, self._io_failed)
        return self._writers[fd]

    def _resume_output(self):
        """Register PTY reads again when the output queue reaches its low watermark."""
        if (
            self._output_paused
            and not self._closing_output
            and len(self._output_buffer) <= 1024 * 1024
        ):
            self._output_paused = False
            self.loop.add_reader(self.master_fd, self._display, self.master_fd)

    async def _flush_output(self):
        """Drain queued output in order and coordinate writes with the active editor
        renderer.
        """
        try:
            while self._output_buffer:
                data = bytes(self._output_buffer[:65536])
                del self._output_buffer[: len(data)]

                async def write(data=data):
                    """Write the chunk captured for this iteration and wait for partial
                    writes to finish.
                    """
                    writer = self._writer(self.stdout_fd)
                    writer.write(data)
                    await writer.drain()

                app = getattr(self.session, "app", None)
                if getattr(app, "is_running", False) is True:
                    await self.session.write_output(write)
                else:
                    await write()
                self._resume_output()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._io_failed(exc)

    async def _drain_output(self):
        """Wait until both output tasks and FD writer queues are empty."""
        if self._output_task is not None:
            await self._output_task
        await self._writer(self.stdout_fd).drain()

    def _write(self, fd: int, data: Union[bytes, bytearray]) -> None:
        """Send output to an FD writer or enqueue it for the UI and apply backpressure."""
        if fd != self.stdout_fd:
            self._writer(fd).write(data)
            return
        if not data:
            return
        if len(self._output_buffer) + len(data) > 4 * 1024 * 1024:
            raise BufferError("Pending terminal output exceeds 4 MiB")
        self._output_buffer.extend(data)
        if len(self._output_buffer) >= 2 * 1024 * 1024 and not self._output_paused:
            self._output_paused = True
            self.loop.remove_reader(self.master_fd)
        if self._output_task is None or self._output_task.done():
            self._output_task = self.loop.create_task(self._flush_output())

    def _input(self, fd: int) -> None:
        """Forward input to the internal PTY during execution and retain a diagnostic tail."""
        data = self._read(fd)
        self.shell_temp_buffer.extend(data)
        del self.shell_temp_buffer[:-65536]
        try:
            self._write(self.master_fd, data)
        except Exception as exc:
            self._io_failed(exc)

    def _update(self, fd: int) -> None:
        """Consume FIFO frames to update shell state and the received prompt ID."""
        try:
            chunk = os.read(fd, self.chunk_size)
            if not chunk:
                return
            for category, body in self._frame_decoder.feed(chunk):
                if category == PROMPT_ID:
                    if (
                        body
                        and len(body) <= 19
                        and body.isdigit()
                        and int(body) < 2**63
                    ):
                        self._context_id = max(self._context_id, int(body))
                        self._context_event.set()
                else:
                    self.session.update_context(category, body)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            self._io_failed(exc)

    def _track_output_line(self, data: bytes) -> None:
        """Track only linear output outside initialization as a candidate prompt prefix."""
        if not self._initializing and self.adapter.behavior.preserve_output_line:
            # Called before the prompt callback, even when output and the
            # prompt boundary arrive in the same PTY read.
            self._native_output_line.feed(data)

    def _consume_output(self, raw_data: bytes) -> None:
        """Pass a PTY chunk through the sequencer and update history and queued output."""
        data = self.sequencer.interpret(raw_data)
        if self._initializing:
            self._init_output.extend(raw_data)
            del self._init_output[:-65536]
            return
        self.scroll_back.append(raw_data)
        self.scroll_back.append_ld(data)
        self._write(self.stdout_fd, data)

    def _display(self, fd: int) -> None:
        # Batch small reads without monopolizing the event loop or bypassing
        # backpressure. One output task handles everything queued by this callback.
        """Read the PTY within a work budget and handle EOF, backpressure, and I/O
        failures.
        """
        budget = 65536
        try:
            for _ in range(64):
                try:
                    raw_data = os.read(fd, min(self.chunk_size, budget))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    raw_data = b""  # A closed PTY slave reports EIO on Linux.
                if not raw_data:
                    self._closing_output = True
                    self.loop.remove_reader(fd)
                    self._write(self.stdout_fd, self.sequencer.finish())
                    return
                self._consume_output(raw_data)
                budget -= len(raw_data)
                if not budget or self._output_paused:
                    break
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            self._io_failed(exc)

    async def _send(self, data: Union[str, bytes]) -> int:
        """Send an encoded command in ordered chunks and return the byte count."""
        text_bytes = data.encode(self.encoder) if isinstance(data, str) else data
        writer = self._writer(self.master_fd)
        for start in range(0, len(text_bytes), 65536):
            writer.write(text_bytes[start : start + 65536])
            await writer.drain()
        return len(text_bytes)

    async def _exec(self, data: Union[str, bytes]) -> None:
        """Pass input to the shell and wait for the next prompt, state, and output drain.

        Remove one matching echo using the current termios settings. Forward keystrokes
        to the shell while waiting, then detach the raw input reader after the
        completion boundary.
        """
        self.scroll_back.last_output = None
        self._native_output_line.clear()
        self._prompt_prefix = b""
        if isinstance(data, str):
            data = data.encode(self.encoder)
        previous_prompt_id = self._prompt_id
        self.prompt_event.clear()
        self.command_start_event.set()
        self.command_done_event.clear()
        try:
            # The PTY echoes every newline as CRLF, including pasted lines.
            attrs = termios.tcgetattr(self.master_fd)
            echo = data if attrs[3] & termios.ECHO else b""
            if attrs[1] & termios.OPOST and attrs[1] & termios.ONLCR:
                echo = echo.replace(b"\n", b"\r\n")
            self.sequencer.at_masking(echo)
            await self._send(data)
            if not self.prompt_event.is_set():
                self.loop.add_reader(self.dupin_fd, self._input, self.dupin_fd)
            await self.prompt_event.wait()
            if not self.continuation_active.is_set():
                await self._synchronize_prompt(previous_prompt_id)
            await self._drain_output()
        finally:
            self.loop.remove_reader(self.dupin_fd)

    async def _spawn(self) -> None:
        """Start an interactive shell with a controlling PTY and close the parent's slave
        FD.
        """

        def setup_pty():
            """Create a new session in the child and make the slave PTY its controlling
            terminal.
            """
            os.setsid()
            fcntl.ioctl(self.slave_fd, termios.TIOCSCTTY, 0)

        if not self.shell_path or not os.path.isfile(self.shell_path):
            raise FileNotFoundError(f"Shell not found: {self.shell}")

        self.proc = await asyncio.create_subprocess_exec(
            self.shell_path,
            *self.shell_args,
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            env=self._environ,
            cwd=os.getcwd(),
            preexec_fn=setup_pty,
        )

        self.shell_pid = self.proc.pid
        # The parent must not keep the slave alive after the child inherits it.
        self._close_fd("slave_fd")

    async def _init(
        self,
        init_command: bytes,
        timeout: Optional[float],
        *,
        show_newline: bool = True,
    ) -> None:
        """Wait for the initial prompt and state handshake while keeping the event loop
        responsive.

        Monitor shell exit, I/O errors, and timeout together and clean up auxiliary
        waiting tasks.
        """
        self._initializing = True
        self._init_output.clear()
        self._init_event.clear()

        async def handshake():
            """Send the integration command and wait for the prompt and its matching state
            frame.
            """
            await self._send(init_command)
            await self._init_event.wait()
            await self._wait_context()

        ready = asyncio.create_task(handshake())
        exited = asyncio.create_task(self.proc.wait())
        try:
            done, _ = await asyncio.wait(
                [ready, exited, self._fatal_error],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._fatal_error in done:
                raise self._fatal_error.result()
            if ready in done:
                ready.result()
                if show_newline:
                    self._write(self.stdout_fd, b"\r\n")
                await self._drain_output()
                return
            reason = (
                "Shell exited during initialization"
                if exited in done
                else "Shell initialization timed out"
            )
            raise RuntimeError(
                reason + ": " + self._init_output.decode(self.encoder, errors="replace")
            )
        finally:
            self._initializing = False
            ready.cancel()
            exited.cancel()
            await asyncio.gather(ready, exited, return_exceptions=True)

    async def _synchronize_prompt(self, previous_prompt_id):
        """Recovery is permitted only after this session's primary prompt."""
        if self._unhooked_prompt:
            self._unhooked_prompt = False
            await self._init(b"\n", timeout=self.context_timeout, show_newline=False)
        elif self.adapter.refresh:
            await self._init(
                self.refresh_command, timeout=self.context_timeout, show_newline=False
            )
        elif self._prompt_id <= previous_prompt_id:
            # The PS1 marker survived but the prompt hook stopped reporting.
            command = self.adapter.syntax.preserve_status(
                self.init_command.decode(self.encoder)
            )
            await self._init(
                command.encode(self.encoder),
                timeout=self.context_timeout,
                show_newline=False,
            )
        else:
            await self._wait_context()

    async def _shell(self) -> None:
        """Wait for shell exit, then drain final PTY output and incomplete control
        sequences.
        """
        await self.proc.wait()
        # Process exit does not mean the PTY has been drained. Stop the reader
        # before draining, including when a descendant holds the slave open.
        self._closing_output = True
        self.loop.remove_reader(self.master_fd)
        await self._drain_output()
        budget = 65536
        while True:
            try:
                data = os.read(self.master_fd, self.chunk_size)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            self._consume_output(data)
            budget -= len(data)
            if budget <= 0:
                await self._drain_output()
                budget = 65536
        self._write(self.stdout_fd, self.sequencer.finish())
        await self._drain_output()

    async def _prompt(self) -> None:
        """Inject typeahead into the editor and process submissions in pre-hook, execution,
        post-hook order.
        """

        def inject_typehead():
            """Feed typeahead through the real key parser and update editor state and
            rendering.
            """
            if typehead:
                self.session.parser.feed(typehead)
                self.session.parser.flush()
                self.session.app.key_processor.process_keys()
                self.session.app.invalidate()

        while True:
            typehead = ""
            if self.shell_temp_buffer:
                del self.shell_temp_buffer[:]
            while self._pre_input(self.tty_pipe):
                pass
            if self.dupin_buffer:
                idx = self.dupin_buffer.find(b"\n")
                if idx != -1:
                    typehead_bytes = self.dupin_buffer[: idx + 1]
                    typehead = typehead_bytes.decode(self.encoder, errors="ignore")
                    del self.dupin_buffer[: idx + 1]
                else:
                    typehead = self.dupin_buffer.decode(self.encoder, errors="ignore")
                    self.dupin_buffer.clear()

            try:
                self._write(self.stdout_fd, b"\x1b[2K\r")
                await self._drain_output()
                command = await self.session.get(pre_run=inject_typehead)
                await self._drain_output()
                if command is None:
                    command = typehead
            except KeyboardInterrupt:
                self.dupin_buffer.clear()
                if self.continuation_active.is_set():
                    await self._exec(bytes(self.exec_attrs[6][termios.VINTR]))
                continue
            except ShellPassRequest as req:
                await req.callback(*req.args, **req.kwargs)
                continue
            except ShellExitRequest:
                return

            try:
                # Submit a pasted shell block together: waiting after its first
                # line would prevent the remaining body/end from reaching the shell.
                lines = self.adapter.behavior.split_commands(command)
                for cmd in lines:
                    self.last_command = cmd
                    await self.session.pre_exec()

                    await self._exec((cmd + "\n").encode(self.encoder))

                    await self.session.post_exec()
                    await self.session.fallback()

            except ShellExitRequest:
                if command.strip():
                    return

    def _close_fd(self, name: str) -> None:
        """Close an owned FD once and clear its attribute to None."""
        fd = getattr(self, name)
        setattr(self, name, None)
        if fd is not None:
            os.close(fd)

    async def _stop(self) -> None:
        """Cancel session tasks, wait for shell exit, and close output-monitoring
        resources.
        """
        for task in self.tasks or []:
            task.cancel()
        await asyncio.gather(*(self.tasks or []), return_exceptions=True)
        try:
            if self.proc is not None:
                if self.proc.returncode is None:
                    try:
                        # Interactive Bash ignores SIGTERM; hangup also informs its jobs.
                        self.proc.send_signal(signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(self.proc.wait(), 2)
                    except TimeoutError:
                        try:
                            self.proc.kill()
                        except ProcessLookupError:
                            pass
                        await self.proc.wait()
                else:
                    await self.proc.wait()
        finally:
            if self._output_task is not None:
                self._output_task.cancel()
                await asyncio.gather(self._output_task, return_exceptions=True)
            self._output_buffer.clear()
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()

    async def main(self):
        """Acquire temporary integration files, FDs, and terminal settings for the session
        lifetime.

        ExitStack restores resources on normal exit and Python exceptions. External
        SIGTERM currently has no cleanup handler and does not guarantee the same
        restoration.
        """
        self.loop = asyncio.get_running_loop()
        self._fatal_error = self.loop.create_future()
        self._init_event = asyncio.Event()
        self.prompt_event = asyncio.Event()
        self.continuation_active = asyncio.Event()
        self.command_start_event = asyncio.Event()
        self.command_done_event = asyncio.Event()
        self.tasks = []

        with contextlib.ExitStack() as resources:
            try:
                pipe_dir = resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="ish-")
                )
                runtime = Path(pipe_dir) / "integration"
                if not build_binary(directory=runtime):
                    raise RuntimeError(
                        "Failed to build ish_forward. Check the compiler error above."
                    )

                self.exec_attrs = termios.tcgetattr(self.stdin_fd)
                resources.callback(
                    termios.tcsetattr, self.stdin_fd, termios.TCSANOW, self.exec_attrs
                )
                for fd in dict.fromkeys([self.stdin_fd, self.stdout_fd]):
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    resources.callback(fcntl.fcntl, fd, fcntl.F_SETFL, flags)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                self.master_fd, self.slave_fd = pty.openpty()
                resources.callback(self._close_fd, "master_fd")
                resources.callback(self._close_fd, "slave_fd")
                os.set_blocking(self.master_fd, False)
                self.dupin_fd = os.dup(self.stdin_fd)
                resources.callback(self._close_fd, "dupin_fd")

                self.shell_pipe_path = os.path.join(pipe_dir, SHELL_FIFO)
                self.tty_pipe_path = os.path.join(pipe_dir, TTY_FIFO)
                for path, name in [
                    (self.shell_pipe_path, "shell_pipe"),
                    (self.tty_pipe_path, "tty_pipe"),
                ]:
                    os.mkfifo(path, 0o600)
                    setattr(self, name, os.open(path, os.O_RDWR | os.O_NONBLOCK))
                    resources.callback(self._close_fd, name)

                self.xdg_home = install_scripts(
                    directory=runtime,
                    signals=self.signals,
                    forward_path=runtime / FORWARD_BINARY,
                )
                self.shell_integration = str(self.xdg_home / self.adapter.script)
                self.init_command = self.adapter.source(
                    self.shell_integration, self.shell_pipe_path, self.tty_pipe_path
                ).encode(self.encoder)
                self.refresh_command = self.adapter.refresh_command(
                    self.xdg_home
                ).encode(self.encoder)

                tty.setraw(self.stdin_fd)
                previous_resize = self.session.app._on_resize
                resources.callback(
                    setattr, self.session.app, "_on_resize", previous_resize
                )

                def on_resize():
                    """Resize the PTY, then call prompt-toolkit's original resize callback."""
                    self._sigwinch(self.master_fd, *shutil.get_terminal_size())
                    previous_resize()

                self.session.app._on_resize = on_resize

                self.sequencer = Sequencer(
                    encoder=self.encoder, output_callback=self._track_output_line
                )
                self.adapter.configure_sequencer(
                    self.sequencer,
                    prompt_id=self._set_prompt_id,
                    prompt=self._set_prompt,
                    continuation=self._set_continuation,
                    unhooked_prompt=self._set_unhooked_prompt,
                    signals=self.signals,
                )
                for fd, callback in [
                    (self.master_fd, self._display),
                    (self.shell_pipe, self._update),
                    (self.tty_pipe, self._pre_input),
                ]:
                    self.loop.add_reader(fd, callback, fd)
                    resources.callback(self.loop.remove_reader, fd)
                previous_signal = signal.getsignal(signal.SIGWINCH)
                resources.callback(signal.signal, signal.SIGWINCH, previous_signal)
                self.loop.add_signal_handler(
                    signal.SIGWINCH, self._signal_handler, signal.SIGWINCH, None
                )
                resources.callback(self.loop.remove_signal_handler, signal.SIGWINCH)
                self._sigwinch(self.master_fd, *shutil.get_terminal_size())

                self._initializing = True
                await self._spawn()
                await self._init(self.init_command, timeout=60)
                # These coroutines require a live, initialized shell.
                self.tasks = [
                    asyncio.create_task(coro()) for coro in (self._prompt, self._shell)
                ]
                done, _ = await asyncio.wait(
                    [*self.tasks, self._fatal_error],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._fatal_error in done:
                    raise self._fatal_error.result()
                for task in done:
                    task.result()
                await self._drain_output()
            finally:
                await self._stop()

    def run(self):
        """Check Linux support and run the asynchronous session from a synchronous call."""
        if platform.system() != "Linux":
            raise OSError(f"Unsupported operating system: {platform.system()}")
        sys.exit(asyncio.run(self.main()))
