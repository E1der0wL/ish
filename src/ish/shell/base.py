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
import struct
import sys
import tempfile
import termios
import tty
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

import psutil

from ish.config import config
from ish.runtime.fdio import FDWriter
from ish.runtime.observer import InputObserver

from .adapters import get_adapter
from .constants import (
    FORWARD_BINARY,
    PROMPT_ID,
    PROMPT_ID_LIMIT,
    PROMPT_ID_MAX_DIGITS,
    SHELL_FIFO,
    TIOCGPTPEER,
    TTY_FIFO,
    SessionSignals,
)
from .guard import spawn_environment
from .input import (
    InputModeLease,
    InputRejected,
    SubmittedInput,
    check_terminal,
    staged_prefix,
)
from .integration import build_binary_async, install_scripts
from .limits import (
    BYTES_PER_MIB,
    DEFAULT_READ_CHUNK_BYTES,
    DIAGNOSTIC_TAIL_BYTES,
    FIFO_READ_CHUNK_BYTES,
    OUTPUT_QUEUE_HIGH_BYTES,
    OUTPUT_QUEUE_LIMIT_BYTES,
    OUTPUT_QUEUE_LOW_BYTES,
    SCROLLBACK_MAX_BYTES,
    SCROLLBACK_MAX_LINES,
    STREAM_CHUNK_BYTES,
    TYPEAHEAD_LIMIT_BYTES,
)
from .prefix import OutputLine
from .protocol import FrameDecoder
from .request import ShellExitRequest, ShellPassRequest
from .sequencer import Sequencer
from .signals import ShellSignalController, SignalScope
from .state import TerminalState

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from ish.ui.prompt import Prompt

__all__ = ["InteractiveShell"]


class ScrollBack:
    """Split the payload byte budget equally between history and last output.

    Discard the oldest bytes first. UTF-8 cuts display replacement characters;
    truncation flags let consumers distinguish a retained tail from full output.
    """

    def __init__(
        self,
        max_lines=SCROLLBACK_MAX_LINES,
        max_bytes=SCROLLBACK_MAX_BYTES,
        encoder="utf-8",
    ):
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
    input ownership only after confirming a fresh prompt and its state frame.
    Signal policies request transport operations; buffers stay owned by this engine.
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
        "_native_output_line",
        "_preload_path",
        "_prompt_prefix",
        "signals",
        "context_timeout",
        "_output_buffer",
        "_output_task",
        "_output_paused",
        "_closing_output",
        "resize_callback",
        "_accepted_prompt_id",
        "_pending_prompt_id",
        "_send_task",
        "_send_interrupted",
        "_deferred_input",
        "input_observer",
        "_input_observation",
        "_handoff_pending",
        "_native_input_active",
        "_input_epoch",
        "_send_generation",
        "terminal_state",
        "_stopping",
        "_input_mode_lease",
        "signal_controller",
        "_submitted_input",
        "_literal_input",
    )

    # =============================================
    # [Initialization] Configure the adapter and prepare per-session state.
    # =============================================

    def __init__(
        self,
        shell: str = "bash",
        encoder: str = sys.stdout.encoding or "utf-8",
        stdin: int = sys.stdin.fileno(),
        stdout: int = sys.stdout.fileno(),
        stderr: int = sys.stderr.fileno(),
        prompt: Prompt = None,
        chunk: int = DEFAULT_READ_CHUNK_BYTES,
        input_observer: InputObserver | None = None,
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
            self.shell_path = os.path.abspath(self.shell_path)
            shell_name = os.path.basename(self.shell_path)

        self.adapter = get_adapter(shell_name or shell, self.shell_path)
        self.shell = self.adapter.name
        self.shell_args = self.adapter.args
        self.signals = SessionSignals.create()
        self.context_timeout = 5.0
        self._output_buffer = bytearray()
        self._output_task = None
        self._output_paused = False
        self._closing_output = False
        self.resize_callback: Optional[Callable[[], None]] = None
        self._context_id = self._prompt_id = 0
        self._accepted_prompt_id = self._pending_prompt_id = 0
        self._send_task = None
        self._send_interrupted = False
        self._deferred_input = bytearray()
        self.input_observer = input_observer or InputObserver()
        self._input_observation = None
        self._handoff_pending = False
        self._native_input_active = False
        self._input_epoch = 0
        self._send_generation = 0
        self._submitted_input = SubmittedInput()
        self._literal_input = bytearray()
        self.terminal_state = TerminalState()
        self._stopping = False
        self._input_mode_lease = None
        self._context_event = asyncio.Event()
        self._native_output_line = OutputLine()
        self._preload_path = None
        self._prompt_prefix = b""

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
            max_lines=SCROLLBACK_MAX_LINES,
            max_bytes=SCROLLBACK_MAX_BYTES,
            encoder=self.encoder,
        )

        self.sequencer: Optional[Sequencer] = None

        self.session: Prompt = prompt
        self._writers = {}
        self._fatal_error = None
        self._initializing = False
        self._init_output = bytearray()
        self._init_event = None
        self.signal_controller = ShellSignalController(self, self.adapter.signal_policy)

    # =============================================
    # [Utility] Shared process, terminal, descriptor, and I/O helpers.
    # =============================================

    def _has_exited(self) -> bool:
        """Consult the child watcher's cached status without probing the process."""
        return self.proc is not None and self.proc.returncode is not None

    @staticmethod
    def _set_window_size(fd, col, row, xpix=0, ypix=0) -> None:
        """Set the PTY window size in rows and columns."""
        try:
            win_size = struct.pack("HHHH", row, col, xpix, ypix)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, win_size)
        except OSError:
            return

    def _resize(self) -> None:
        """Resize the shell first, then notify any active Python tool of its new size."""
        if self.master_fd is None:
            return
        try:
            rows, columns = termios.tcgetwinsize(self.stdin_fd)
        except (OSError, termios.error):
            rows, columns = 0, 0
        if not rows or not columns:
            fallback = shutil.get_terminal_size()
            rows, columns = rows or fallback.lines, columns or fallback.columns
        self._set_window_size(self.master_fd, columns, rows)
        if self.resize_callback is not None:
            self.resize_callback()

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

    def _close_fd(self, name: str) -> None:
        """Close an owned FD once and clear its attribute to None."""
        fd = getattr(self, name)
        setattr(self, name, None)
        if fd is not None:
            os.close(fd)

    def _restore_terminal(self) -> None:
        """Restore saved termios, tolerating a terminal destroyed during hangup."""
        try:
            termios.tcsetattr(self.stdin_fd, termios.TCSANOW, self.exec_attrs)
        except termios.error as exc:
            if self.signal_controller.shutdown_signal is None or exc.args[0] not in (
                errno.EIO,
                errno.ENOTTY,
                errno.EBADF,
            ):
                raise

    # =============================================
    # [Internal API: Input] Collect typeahead, cancel submissions, and transmit input.
    # =============================================

    def _pre_input(self, fd: int) -> Optional[bool]:
        """Collect forwarded typeahead in a bounded buffer and report whether data was
        read.
        """
        try:
            chunk = os.read(fd, FIFO_READ_CHUNK_BYTES)
            if not chunk:
                return
            if self._send_interrupted:
                self.input_observer.record("typeahead_discard", bytes=len(chunk))
                return True
            if self._submitted_input.active:
                self._submitted_input.append(chunk)
                self.input_observer.record("submission_returned", bytes=len(chunk))
                return True
            if len(self.dupin_buffer) + len(chunk) > TYPEAHEAD_LIMIT_BYTES:
                self._io_failed(
                    BufferError(
                        f"Pending typeahead exceeds {TYPEAHEAD_LIMIT_BYTES / BYTES_PER_MIB:g} MiB"
                    )
                )
                return
            self.dupin_buffer.extend(chunk)
            self.input_observer.record(
                "typeahead_received", bytes=len(chunk), pending=len(self.dupin_buffer)
            )
            return True
        except BufferError as exc:
            self._io_failed(exc)
            return
        except (BlockingIOError, OSError):
            return

    def _input(self, fd: int, epoch: int | None = None) -> None:
        """Forward input to the internal PTY during execution and retain a diagnostic tail."""
        if (
            not self._native_input_active
            or (epoch is not None and epoch != self._input_epoch)
            or self._closing_output
            or self._has_exited()
        ):
            return
        data = self._read(fd)
        self.input_observer.read("SHELL", self._input_observation, len(data))
        self.shell_temp_buffer.extend(data)
        del self.shell_temp_buffer[:-DIAGNOSTIC_TAIL_BYTES]
        try:
            if self.signal_controller.consume_input(data):
                return
            if self._handoff_pending and (
                self._send_task is None or self._send_task.done()
            ):
                if len(self._deferred_input) + len(data) > TYPEAHEAD_LIMIT_BYTES:
                    raise BufferError(
                        f"Input during handoff exceeds {TYPEAHEAD_LIMIT_BYTES / BYTES_PER_MIB:g} MiB"
                    )
                self._deferred_input.extend(data)
                self.input_observer.record("handoff_input_deferred", bytes=len(data))
                return
            if self._send_task is not None and not self._send_task.done():
                if len(self._deferred_input) + len(data) > TYPEAHEAD_LIMIT_BYTES:
                    raise BufferError(
                        f"Input during submission exceeds {TYPEAHEAD_LIMIT_BYTES / BYTES_PER_MIB:g} MiB"
                    )
                self._deferred_input.extend(data)
                self.input_observer.record(
                    "input_deferred",
                    "SHELL",
                    bytes=len(data),
                    pending=len(self._deferred_input),
                )
                return
            self._write(self.master_fd, data)
            self.input_observer.record("input_forwarded", "SHELL", bytes=len(data))
        except Exception as exc:
            self._io_failed(exc)

    def _discard_terminal_input(self) -> None:
        """Flush submitted bytes on both sides of the PTY input path, preserving output.

        TCIFLUSH on the master would discard command output, not shell input.
        Open the slave only for this ioctl so it cannot keep an exited shell alive.
        TIOCGPTPEER avoids reopening a possibly renamed or permission-changed path.
        """
        peer = fcntl.ioctl(
            self.master_fd, TIOCGPTPEER, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC
        )
        try:
            termios.tcflush(self.master_fd, termios.TCOFLUSH)
            termios.tcflush(peer, termios.TCIFLUSH)
        finally:
            os.close(peer)

    def _cancel_handoff(self, control: bytes, remaining: bytes) -> None:
        """Cancel an input handoff and retain only post-control typeahead."""
        discard_submitted = self._handoff_pending and self._submitted_input.active
        self._deferred_input.clear()
        self.dupin_buffer.clear()
        self._submitted_input.clear()
        self._literal_input.clear()
        # A helper may already have written cancelled bytes to the FIFO before
        # its read callback runs. Discard them through the confirmed boundary,
        # keeping later keys outside that FIFO until ownership is transferred.
        self._send_interrupted |= discard_submitted
        self._handoff_pending = discard_submitted
        confirmed_prompt = (
            self._accepted_prompt_id < self._pending_prompt_id == self._context_id
        )
        if not (discard_submitted and confirmed_prompt):
            # At an already confirmed primary prompt only the queued block is
            # being cancelled. Signalling that idle reader can consume the next
            # command as interrupt recovery (notably with zsh's disabled ZLE).
            self._write(self.master_fd, control)
        self.return_typeahead(remaining)

    def _resume_native_typeahead(self) -> None:
        """Return held keys to a native command that won a continuation cancel race."""
        data = bytes(self.dupin_buffer)
        self.dupin_buffer.clear()
        if data and not (self._closing_output or self._stopping or self._has_exited()):
            self._write(self.master_fd, data)
            self.input_observer.record("input_forwarded", "SHELL", bytes=len(data))

    def _cancel_submission(self, control: bytes, remaining: bytes) -> None:
        """Discard cancelled transport data before a policy-selected control byte.

        The signal policy has already restored any TTY lease. Queue ownership,
        cancellation generations, and post-control byte ordering remain here.
        """
        self._send_generation += 1
        self._send_interrupted = True
        self._handoff_pending = False
        self.input_observer.record(
            "submission_interrupt", "SHELL", generation=self._send_generation
        )
        if self._send_task is not None:
            self._send_task.cancel()
        self._deferred_input.clear()
        self.dupin_buffer.clear()
        self._submitted_input.clear()
        self._literal_input.clear()
        self._accepted_prompt_id = self._prompt_id
        self._pending_prompt_id = 0
        if self.prompt_event is not None:
            self.prompt_event.clear()
        self._writer(self.master_fd).discard()
        self._discard_terminal_input()
        if self.sequencer is not None:
            self.sequencer.at_masking(b"")
        self._write(self.master_fd, control)
        if self.adapter.refresh:
            # This adapter has no automatic prompt acknowledgement. Its old
            # FIFO is idle until explicit reconnect, so discard it now and keep
            # following user bytes with native input, including recovery.
            while self.tty_pipe is not None and self._pre_input(self.tty_pipe):
                pass
            self._send_interrupted = False
            self._write(self.master_fd, remaining)
        else:
            self.return_typeahead(remaining)
        self.input_observer.terminal("SHELL", self.master_fd, "interrupt")

    async def _send(
        self, data: Union[str, bytes], generation: int | None = None
    ) -> int:
        """Send an encoded command in ordered chunks and return the byte count."""
        text_bytes = data.encode(self.encoder) if isinstance(data, str) else data
        if generation is None:
            generation = self._send_generation
        self.input_observer.record(
            "submission", "SHELL", bytes=len(text_bytes), generation=generation
        )
        writer = self._writer(self.master_fd)
        sent = 0
        for start in range(0, len(text_bytes), STREAM_CHUNK_BYTES):
            if (
                generation != self._send_generation
                or self._has_exited()
                or self._closing_output
            ):
                break
            writer.write(text_bytes[start : start + STREAM_CHUNK_BYTES])
            await writer.drain()
            sent += min(STREAM_CHUNK_BYTES, len(text_bytes) - start)
            # drain() can finish synchronously; even an immediately writable
            # PTY must allow the input callback to observe an interrupt.
            await asyncio.sleep(0)
        return sent

    async def _send_staged(self, data: bytes, prefix: int, generation: int) -> int:
        """Transmit a verified first-line prefix before committing its newline."""
        lease = InputModeLease(self.master_fd, data[:prefix])
        self._input_mode_lease = lease
        try:
            suffix = data[prefix:]
            echo = suffix if lease.saved[3] & termios.ECHO else b""
            if lease.saved[1] & termios.OPOST and lease.saved[1] & termios.ONLCR:
                echo = echo.replace(b"\n", b"\r\n")
            # The prefix has echo disabled; only arm suffix masking when LF
            # is about to be sent, so intervening background output stays intact.
            self.sequencer.at_masking(b"")
            lease.start()
            self.input_observer.record("long_input_started", "SHELL", bytes=prefix)
            sent = await self._send(data[:prefix], generation)
            if generation != self._send_generation or sent != prefix:
                return sent
            async with asyncio.timeout(5):
                while lease.pending():
                    if self._has_exited() or self._closing_output:
                        raise OSError("Shell exited during long input")
                    await asyncio.sleep(0.001)
            if self._prompt_id != self._accepted_prompt_id:
                raise RuntimeError("Shell abandoned a long input before its newline")
        finally:
            try:
                lease.close()
            finally:
                self._input_mode_lease = None
                self.input_observer.record("long_input_restored", "SHELL")
        self.sequencer.at_masking(echo)
        return sent + await self._send(data[prefix:], generation)

    # =============================================
    # [Internal API: Output] Queue, parse, and display PTY output with backpressure.
    # =============================================

    def _write(self, fd: int, data: Union[bytes, bytearray]) -> None:
        """Send output to an FD writer or enqueue it for the UI and apply backpressure."""
        if fd != self.stdout_fd:
            self._writer(fd).write(data)
            return
        if not data:
            return
        if len(self._output_buffer) + len(data) > OUTPUT_QUEUE_LIMIT_BYTES:
            raise BufferError(
                f"Pending terminal output exceeds {OUTPUT_QUEUE_LIMIT_BYTES / BYTES_PER_MIB:g} MiB"
            )
        self._output_buffer.extend(data)
        if (
            len(self._output_buffer) >= OUTPUT_QUEUE_HIGH_BYTES
            and not self._output_paused
        ):
            self._output_paused = True
            self.loop.remove_reader(self.master_fd)
        if self._output_task is None or self._output_task.done():
            self._output_task = self.loop.create_task(self._flush_output())

    def _resume_output(self):
        """Register PTY reads again when the output queue reaches its low watermark."""
        if (
            self._output_paused
            and not self._closing_output
            and len(self._output_buffer) <= OUTPUT_QUEUE_LOW_BYTES
        ):
            self._output_paused = False
            self.loop.add_reader(self.master_fd, self._display, self.master_fd)

    async def _flush_output(self):
        """Drain queued output in order and coordinate writes with the active editor
        renderer.
        """
        try:
            while self._output_buffer:
                data = bytes(self._output_buffer[:STREAM_CHUNK_BYTES])
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
            del self._init_output[:-DIAGNOSTIC_TAIL_BYTES]
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
        budget = STREAM_CHUNK_BYTES
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
                    self.input_observer.record("pty_eof", "SHELL")
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

    # =============================================
    # [Internal API: Integration] Validate prompt signals and synchronize shell context.
    # =============================================

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
                        and len(body) <= PROMPT_ID_MAX_DIGITS
                        and body.isdigit()
                        and int(body) < PROMPT_ID_LIMIT
                    ):
                        self._context_id = max(self._context_id, int(body))
                        if self._context_id > self._accepted_prompt_id:
                            self._handoff_pending = True
                        self._context_event.set()
                else:
                    self.session.update_context(category, body)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            self._io_failed(exc)

    def _set_prompt_id(self, data: bytes) -> bool:
        """Accept only valid, increasing session IDs and pass invalid signals through."""
        if not data or len(data) > PROMPT_ID_MAX_DIGITS or not data.isdigit():
            return False
        value = int(data)
        if not self._prompt_id < value < PROMPT_ID_LIMIT:
            return False
        self._prompt_id = value
        return True

    def _set_prompt(self, prompt: bytes) -> Optional[bytes]:
        """Stage a fresh prompt candidate; replayed PS1 is ordinary program output.

        Only the matching state frame can transfer input back to the editor. A
        surviving marker without a hook must never authorize a maintenance write.
        """
        if self._prompt_id <= self._accepted_prompt_id:
            return prompt
        self._pending_prompt_id = self._prompt_id
        self.input_observer.record(
            "prompt_candidate", prompt_id=self._prompt_id, context_id=self._context_id
        )
        self.message = prompt
        if not self._initializing:
            self._prompt_prefix = self._native_output_line.prefix
        self.prompt_event.set()
        self._context_event.set()
        if self._initializing:
            self._init_event.set()

    def _set_unhooked_prompt(self, prompt: bytes) -> Optional[bytes]:
        """Keep an unconfirmed caret prompt with native input, without sending a newline."""
        return self._set_prompt(prompt)

    def _set_continuation(
        self, prompt: bytes, *, buffered: bool = False
    ) -> Optional[bytes]:
        """Keep continuation input with the shell or pass it to the editor per adapter
        policy.
        """
        self.continuation_active.set()
        if not self.adapter.refresh:
            # Bytes returned after native continuation input already passed
            # through the PTY. Resume complete lines as a block and restore an
            # unfinished tail literally, just like an initial multiline paste.
            self._submitted_input.active = True
        if buffered:
            # The shell checked readiness before reading its next line. The
            # editor already displayed that submitted input; omit only this PS2.
            return None
        if self.adapter.behavior.native_continuation:
            # The shell may already have read the complete foreach body into
            # its own buffer. Keep one input owner until the primary prompt.
            # Returning bytes preserves the prompt's position in the PTY output.
            return prompt
        self.session.set_prompt(prompt, self.encoder)
        # A custom UI continuation policy must not flush or replay shell input.
        self.prompt_event.set()

    async def _wait_context(self) -> None:
        """Wait within a deadline for the state FIFO to reach the current prompt ID."""
        while self._context_id != (self._pending_prompt_id or self._prompt_id):
            self._context_event.clear()
            try:
                await asyncio.wait_for(self._context_event.wait(), self.context_timeout)
            except TimeoutError:
                raise RuntimeError(
                    "Shell context was not received after a confirmed prompt"
                ) from None

    def _accept_prompt(self) -> None:
        """Transfer input ownership only after a fresh prompt and its state agree."""
        if self._closing_output or self._has_exited():
            return
        if not self._accepted_prompt_id < self._pending_prompt_id == self._context_id:
            raise RuntimeError("Cannot accept an unconfirmed shell prompt")
        self.signal_controller.reset()
        if self.loop is not None and self.dupin_fd is not None:
            self.loop.remove_reader(self.dupin_fd)
            self._native_input_active = False
            self.input_observer.end(self._input_observation)
            self._input_observation = None
        if self._send_interrupted:
            # A previous primary hook may have moved submitted suffix bytes
            # into the typeahead FIFO before VINTR arrived. The new prompt is
            # emitted after its forwarder exits, so drain those bytes now.
            while self._pre_input(self.tty_pipe):
                pass
            self._send_interrupted = False
        self._accepted_prompt_id = self._pending_prompt_id
        # The helper forwarded older PTY input before emitting this prompt.
        # Read that FIFO before appending newer input held during the handoff.
        while self.tty_pipe is not None and self._pre_input(self.tty_pipe):
            pass
        self.return_typeahead(bytes(self._deferred_input))
        self._deferred_input.clear()
        self.input_observer.record(
            "prompt_accepted", prompt_id=self._accepted_prompt_id
        )
        self.command_done_event.set()
        self.continuation_active.clear()
        self.session.set_prompt(self._prompt_prefix + self.message, self.encoder)
        self.input_observer.terminal("SHELL", self.master_fd, "prompt_accepted")

    async def _synchronize_prompt(self):
        """Confirm context without ever injecting recovery commands into shell stdin."""
        await self._wait_context()
        self._accept_prompt()

    # =============================================
    # [Core API] Coordinate shell startup, command execution, and session cleanup.
    # =============================================

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

        with spawn_environment(
            self._environ, self._preload_path, self.adapter.preload
        ) as (env, fds):
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    self.shell_path,
                    *self.shell_args,
                    stdin=self.slave_fd,
                    stdout=self.slave_fd,
                    stderr=self.slave_fd,
                    env=env,
                    pass_fds=fds,
                    cwd=os.getcwd(),
                    preexec_fn=setup_pty,
                )
            )
            try:
                self.proc = await asyncio.shield(spawn)
            finally:
                if self.proc is None:
                    # Keep the library FD alive until shielded exec has finished.
                    with contextlib.suppress(Exception):
                        self.proc = await spawn

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
            self._accept_prompt()

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
            if ready in done and exited not in done and not self._has_exited():
                ready.result()
                if show_newline:
                    self._write(self.stdout_fd, b"\r\n")
                await self._drain_output()
                return
            reason = (
                "Shell exited during initialization"
                if exited in done or self._has_exited()
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

    async def _exec(self, data: Union[str, bytes]) -> None:
        """Execute one accepted block, resuming unread native suffixes unchanged.

        An intermediate primary prompt is a transport boundary, not another
        editor submission. Preserve native reads and continuation syntax by
        resending the whole returned stream, including an incomplete final line.
        Only a suffix with no submitted newline returns to the editor as literal
        text. Fresh keys held outside the PTY remain in their existing queue.
        """
        if isinstance(data, str):
            data = data.encode(self.encoder)
        self._submitted_input.begin(data, enabled=not self.adapter.refresh)
        try:
            while True:
                await self._exec_once(data)
                if not self._submitted_input.active or self._has_exited():
                    return
                returned = self._submitted_input.take()
                if not returned:
                    return
                if b"\n" not in returned:
                    self._literal_input.extend(returned)
                    return
                self.input_observer.record("submission_resumed", bytes=len(returned))
                data = returned
        finally:
            self._submitted_input.clear()

    async def _exec_once(self, data: Union[str, bytes]) -> None:
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
        prefix = self.validate_submission(data)
        self.prompt_event.clear()
        self.command_start_event.set()
        self.command_done_event.clear()
        self._send_interrupted = False
        self._handoff_pending = False
        self._deferred_input.clear()
        self._send_generation += 1
        self._input_epoch += 1
        if hasattr(self.session, "take_typeahead"):
            if self.adapter.refresh:
                data += self.session.take_typeahead()
            elif self._submitted_input.active:
                # Keys already retained by the editor or an earlier handoff
                # precede future native reads, but follow the submitted block.
                # Keep them separate from command echo and transport validation.
                self._deferred_input.extend(self.session.take_typeahead())
        try:
            # The PTY echoes every newline as CRLF, including pasted lines.
            attrs = termios.tcgetattr(self.master_fd)
            self.input_observer.terminal("SHELL", self.master_fd, "submission")
            echo = data if attrs[3] & termios.ECHO else b""
            if attrs[1] & termios.OPOST and attrs[1] & termios.ONLCR:
                echo = echo.replace(b"\n", b"\r\n")
            self.sequencer.at_masking(echo)
            self.loop.add_reader(
                self.dupin_fd, self._input, self.dupin_fd, self._input_epoch
            )
            self._native_input_active = True
            self._input_observation = self.input_observer.begin("SHELL")
            generation = self._send_generation
            self._send_task = asyncio.create_task(
                self._send_staged(data, prefix, generation)
                if prefix
                else self._send(data, generation)
            )
            try:
                await self._send_task
            except InputRejected:
                # A final lease check can reject changed settings before any
                # bytes are sent. The acknowledged prompt still belongs to us.
                self.command_start_event.clear()
                self.command_done_event.set()
                self.prompt_event.set()
                self.return_typeahead(bytes(self._deferred_input))
                raise
            except asyncio.CancelledError:
                if (
                    generation == self._send_generation
                    or asyncio.current_task().cancelling()
                ):
                    raise
            finally:
                self._send_task = None
            if (
                self._deferred_input
                and not self._handoff_pending
                and not self._has_exited()
            ):
                self._write(self.master_fd, self._deferred_input)
                self.input_observer.record(
                    "deferred_forwarded", "SHELL", bytes=len(self._deferred_input)
                )
                self._deferred_input.clear()
            await self.prompt_event.wait()
            await self._synchronize_prompt()
            await self._drain_output()
        finally:
            self.loop.remove_reader(self.dupin_fd)
            self._native_input_active = False
            self.input_observer.end(self._input_observation)
            self._input_observation = None
            self._deferred_input.clear()

    async def _prompt(self) -> None:
        """Inject typeahead into the editor and process submissions in pre-hook, execution,
        post-hook order.
        """

        def inject_typehead():
            """Feed typeahead through the real key parser and update editor state and
            rendering.
            """
            if self._literal_input:
                self.session.feed_literal_input(bytes(self._literal_input))
                self._literal_input.clear()
            if typehead:
                self.input_observer.record(
                    "typeahead_injected", "EDITOR", bytes=len(typehead)
                )
                if hasattr(self.session, "feed_typeahead"):
                    self.session.feed_typeahead(typehead)
                else:
                    self.session.parser.feed(
                        typehead.decode(self.encoder, errors="replace")
                    )
                    self.session.parser.flush()
                self.session.app.key_processor.process_keys()
                self.session.app.invalidate()

        retry_command = None
        while True:
            if self._has_exited():
                return
            typehead = b""
            if self.shell_temp_buffer:
                del self.shell_temp_buffer[:]
            while self._pre_input(self.tty_pipe):
                pass
            if self.dupin_buffer:
                idx = self.dupin_buffer.find(b"\n")
                if idx != -1:
                    typehead_bytes = self.dupin_buffer[: idx + 1]
                    typehead = bytes(typehead_bytes)
                    del self.dupin_buffer[: idx + 1]
                else:
                    typehead = bytes(self.dupin_buffer)
                    self.dupin_buffer.clear()

            try:
                self._write(self.stdout_fd, b"\x1b[2K\r")
                await self._drain_output()
                options = {"pre_run": inject_typehead}
                if retry_command is not None:
                    options["default"] = retry_command
                    retry_command = None
                command = await self.session.get(**options)
                await self._drain_output()
                if self._has_exited():
                    return
                if command is None:
                    # Internal tools already completed without running a shell
                    # command. Their unread input belongs to the next editor.
                    continue
            except KeyboardInterrupt:
                self.dupin_buffer.clear()
                self._literal_input.clear()
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
                    self.validate_submission((cmd + "\n").encode(self.encoder))
                    await self.session.pre_exec()

                    await self._exec((cmd + "\n").encode(self.encoder))

                    await self.session.post_exec()
                    await self.session.fallback()

            except InputRejected as exc:
                # The UI normally rejects before accepting Enter. Revalidate
                # here for custom sessions or hooks that changed terminal modes.
                retry_command = command
                self._write(self.stdout_fd, (str(exc) + "\r\n").encode(self.encoder))
            except ShellExitRequest:
                if command.strip():
                    return

    async def _shell(self) -> int:
        """Return the child status after draining final output and control sequences."""
        status = await self.proc.wait()
        self._native_input_active = False
        if self.dupin_fd is not None:
            self.loop.remove_reader(self.dupin_fd)
        self._send_generation += 1
        writer = self._writers.get(self.master_fd)
        if writer is not None:
            writer.discard()
        self.input_observer.record("process_exit", "SHELL", status=status)
        # Process exit does not mean the PTY has been drained. Stop the reader
        # before draining, including when a descendant holds the slave open.
        self._closing_output = True
        self.loop.remove_reader(self.master_fd)
        await self._drain_output()
        budget = STREAM_CHUNK_BYTES
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
                budget = STREAM_CHUNK_BYTES
        self._write(self.stdout_fd, self.sequencer.finish())
        await self._drain_output()
        return status

    async def _stop(self) -> None:
        """Cancel session tasks, wait for shell exit, and close output-monitoring
        resources.
        """
        self._native_input_active = False
        self._send_generation += 1
        self.signal_controller.reset()
        if self.dupin_fd is not None:
            self.loop.remove_reader(self.dupin_fd)
        for task in self.tasks or []:
            task.cancel()
        await asyncio.gather(*(self.tasks or []), return_exceptions=True)
        try:
            await self.signal_controller.stop_child()
        finally:
            if self.signal_controller.shutdown_signal is not None:
                # Drain only within a shutdown deadline. A disconnected terminal
                # or blocked output consumer must not prevent termios/FD cleanup.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    async with asyncio.timeout(0.5):
                        if self.master_fd is not None:
                            self.loop.remove_reader(self.master_fd)
                            self._closing_output = True
                            for _ in range(16):
                                data = self._read(self.master_fd)
                                if not data:
                                    break
                                self._consume_output(data)
                                await self._drain_output()
                        await self._drain_output()
            if self._output_task is not None:
                self._output_task.cancel()
                await asyncio.gather(self._output_task, return_exceptions=True)
            self._output_buffer.clear()
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()
            if self.signal_controller.shutdown_signal is not None and os.isatty(
                self.stdout_fd
            ):
                # The output FD is still nonblocking. Failure here is harmless:
                # ExitStack must still restore termios and release session files.
                with contextlib.suppress(OSError):
                    os.write(self.stdout_fd, self.terminal_state.restore())

    async def _run_session(self) -> int:
        """Acquire temporary integration files, FDs, and terminal settings for the session
        lifetime.

        ExitStack restores resources after normal exit, exceptions, and the
        cancellation requested by main's catchable termination handlers.
        Return the natural child status, or zero for an editor-only exit. The
        cleanup-induced child termination is not the editor's exit result.
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
                # Each session owns its files; another session must never replace or
                # clean up live FIFOs. Resolve rc overrides before the shell can cd.
                cache_dir = config.CACHE_DIR.expanduser().resolve()
                cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                pipe_dir = resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="session-", dir=cache_dir)
                )
                runtime = Path(pipe_dir) / "integration"
                if not await build_binary_async(directory=runtime):
                    raise RuntimeError(
                        "Failed to prepare ish_forward. Check the error above."
                    )
                if self.adapter.preload is not None:
                    library = self.adapter.preload.library
                    if not await build_binary_async(directory=runtime, library=library):
                        raise RuntimeError(f"Failed to prepare {library.binary_name}.")
                    self._preload_path = runtime / library.binary_name

                self.exec_attrs = termios.tcgetattr(self.stdin_fd)
                resources.callback(self._restore_terminal)
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
                    adapter=self.adapter,
                )
                self.shell_integration = str(self.xdg_home / self.adapter.script)
                self.init_command = self.adapter.source(
                    self.shell_integration, self.shell_pipe_path, self.tty_pipe_path
                ).encode(self.encoder)

                tty.setraw(self.stdin_fd)
                previous_resize = self.session.app._on_resize
                resources.callback(
                    setattr, self.session.app, "_on_resize", previous_resize
                )

                def on_resize():
                    """Dispatch the resize policy before prompt-toolkit updates its UI."""
                    self.signal_controller.notify_resize()
                    previous_resize()

                self.session.app._on_resize = on_resize

                self.sequencer = Sequencer(
                    encoder=self.encoder,
                    output_callback=self._track_output_line,
                    control_callback=self.terminal_state.observe,
                )
                self.adapter.configure_sequencer(
                    self.sequencer,
                    prompt_id=self._set_prompt_id,
                    prompt=self._set_prompt,
                    continuation=self._set_continuation,
                    unhooked_prompt=self._set_unhooked_prompt,
                    signals=self.signals,
                )
                self.signal_controller.configure_sequencer(self.sequencer, self.signals)
                for fd, callback in [
                    (self.master_fd, self._display),
                    (self.shell_pipe, self._update),
                    (self.tty_pipe, self._pre_input),
                ]:
                    self.loop.add_reader(fd, callback, fd)
                    resources.callback(self.loop.remove_reader, fd)
                resources.enter_context(
                    self.signal_controller.install(SignalScope.RUNTIME)
                )
                self._resize()

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
                status = 0
                if self._has_exited():
                    # A queued editor completion can win the wait in the same
                    # turn as process exit. Let the shell task drain its tail.
                    status = await self.tasks[1]
                await self._drain_output()
                return status
            finally:
                self._stopping = True
                await self._stop()

    # =============================================
    # [External API] Expose input handoff, validation, and session entry points.
    # =============================================

    def tool_context(self) -> tuple[str, dict[str, str]]:
        """Snapshot exported state at a confirmed primary prompt for a Python tool.

        FIFO updates populate session.context synchronously, independently of
        completion scans. Read the actual shell cwd once per tool because PWD may
        be unset or modified. Never fall back to the ish parent's startup state.
        """
        if (
            self._has_exited()
            or self._stopping
            or self._closing_output
            or self._initializing
            or self._accepted_prompt_id <= 0
            or self._accepted_prompt_id != self._context_id
            or self._accepted_prompt_id != self._prompt_id
            or self.command_done_event is None
            or not self.command_done_event.is_set()
            or self.continuation_active is None
            or self.continuation_active.is_set()
        ):
            raise RuntimeError("Python tools require a confirmed primary shell prompt")
        environ = self.session.context.environ
        if environ is None:
            raise RuntimeError("Shell environment is unavailable for the Python tool")
        cwd = psutil.Process(self.shell_pid).cwd()
        if not cwd or not os.path.isabs(cwd):
            raise RuntimeError(
                "Shell working directory is unavailable for the Python tool"
            )
        return cwd, dict(environ)

    def return_typeahead(self, data: bytes) -> None:
        """Append unread input returned by the previous consumer within the byte limit."""
        if len(self.dupin_buffer) + len(data) > TYPEAHEAD_LIMIT_BYTES:
            raise BufferError(
                f"Pending typeahead exceeds {TYPEAHEAD_LIMIT_BYTES / BYTES_PER_MIB:g} MiB"
            )
        self.dupin_buffer.extend(data)
        if data:
            self.input_observer.record(
                "input_returned", bytes=len(data), pending=len(self.dupin_buffer)
            )

    def validate_submission(self, data: bytes) -> int:
        """Validate editor input without changing terminal settings or writing bytes."""
        prefix = staged_prefix(data, self.adapter.long_input, self.shell)
        if not prefix:
            return 0
        self.signal_controller.validate_submission()
        if (
            self._has_exited()
            or self._closing_output
            or self._accepted_prompt_id <= 0
            or self._accepted_prompt_id != self._context_id
            or self._accepted_prompt_id != self._prompt_id
            or self.command_done_event is None
            or not self.command_done_event.is_set()
            or self.continuation_active is None
            or self.continuation_active.is_set()
        ):
            raise InputRejected(
                "Not sent: long input requires a confirmed primary shell prompt."
            )
        try:
            attrs = termios.tcgetattr(self.master_fd)
            if os.tcgetpgrp(self.master_fd) != self.shell_pid:
                raise InputRejected(
                    "Not sent: the shell does not own the terminal for long input."
                )
            check_terminal(attrs, data[:prefix])
        except (OSError, termios.error) as exc:
            raise InputRejected(
                "Not sent: the shell terminal is unavailable for long input."
            ) from exc
        return prefix

    async def main(self):
        """Run acquisition and cleanup inside the adapter's signal-handler lifetime."""
        self.loop = asyncio.get_running_loop()
        return await self.signal_controller.run(self._run_session)

    def run(self):
        """Check Linux support and run the asynchronous session from a synchronous call."""
        if platform.system() != "Linux":
            raise OSError(f"Unsupported operating system: {platform.system()}")
        status = asyncio.run(self.main())
        if self.signal_controller.shutdown_signal is not None:
            # Python changes the exit status to 120 if its final stdio flush
            # fails. Only redirect a broken stream after cleanup, immediately
            # before this CLI process exits; embedded main() never changes it.
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except OSError as exc:
                    if exc.errno not in (errno.EIO, errno.EPIPE, errno.EBADF):
                        raise
                    target = stream.fileno()
                    sink = os.open(os.devnull, os.O_WRONLY)
                    try:
                        os.dup2(sink, target)
                    finally:
                        # open() may reuse the broken standard descriptor itself.
                        if sink != target:
                            os.close(sink)
        # asyncio reports a child signal death as -N; CLI callers expect 128+N.
        sys.exit(128 - status if status is not None and status < 0 else status)
