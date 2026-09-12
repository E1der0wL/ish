"""Dispatch process signals and terminal control keys through per-shell policies.

Only explicitly registered process signals acquire asyncio handlers. Terminal
keys are a separate input path: writing VINTR to the PTY signals its foreground
group, not ish. Policies decide when to cancel; the engine owns all input queues,
writer ordering, and terminal leases. No idle task or polling is added.
Common handlers and the optional zsh acknowledgement policy share this module;
adapters select which per-session terminal handlers are constructed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import termios
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from .constants import (
    LINE_INTERRUPT_ACK_PREFIX,
    LINE_READER_READY_PREFIX,
    PROMPT_ID_LIMIT,
    PROMPT_ID_MAX_DIGITS,
)
from .input import InputRejected

if TYPE_CHECKING:
    from .base import InteractiveShell

CHILD_SHUTDOWN_GRACE = 2.0
CHILD_SHUTDOWN_CHECK_INTERVAL = 0.05
INTERRUPT_ACK_TIMEOUT = 5.0


class SignalScope(Enum):
    """Separate early termination handling from handlers requiring a live PTY."""

    STARTUP = "startup"
    RUNTIME = "runtime"


class InputPhase(Enum):
    """Identify the existing input boundary at which a terminal key was received."""

    SUBMISSION = "submission"
    HANDOFF = "handoff"


class TerminalSignalHandler:
    """Per-session terminal policy; unhandled native input stays with the PTY.

    A selected handler owns the complete input batch, including the disposition
    of bytes following its control character. Stateful handlers must reset on a
    confirmed prompt and shutdown. Instances are never shared across sessions.
    """

    def __init__(self, shell: InteractiveShell):
        """Bind the policy to the engine that owns its transport and input buffers."""
        self.shell = shell

    @property
    def pending(self) -> bool:
        """Report whether this policy still owns input after cancellation."""
        return False

    def handle(self, control: bytes, data: bytes, phase: InputPhase) -> None:
        """Consume a matched control key at a submission or handoff boundary."""
        raise NotImplementedError

    def hold_input(self, control: bytes | None, data: bytes) -> None:
        """Retain input while an asynchronous cancellation still owns the reader."""
        raise NotImplementedError

    def reset(self) -> None:
        """Release any pending policy state after a confirmed prompt or shutdown."""

    def configure_sequencer(self, sequencer, markers) -> None:
        """Optionally register session-scoped protocol replies for this policy."""

    def validate_submission(self) -> None:
        """Optionally require a verified capability before staging a long line."""


class SubmissionInterrupt(TerminalSignalHandler):
    """Preserve the existing SIGINT cancellation and post-interrupt input ordering."""

    def before_cancel(self, staged: bool) -> None:
        """Allow a shell policy to arm a response after restoring the TTY lease."""

    def handle(self, control: bytes, data: bytes, phase: InputPhase) -> None:
        """Restore before VINTR and delegate queue mutation to the owning engine."""
        shell = self.shell
        remaining = data[data.rfind(control) + len(control) :]
        if phase is InputPhase.HANDOFF:
            shell._cancel_handoff(control, remaining)
            return
        staged = shell._input_mode_lease is not None
        if staged:
            # VINTR can resume another reader before a cancelled task's finally.
            shell._input_mode_lease.restore()
        self.before_cancel(staged)
        shell._cancel_submission(control, remaining)


class LineInterrupt:
    """Track a generation-scoped cancellation acknowledgement without idle polling."""

    def __init__(self):
        """Start with no verified handler and no outstanding cancellation."""
        self.ready_id = 0
        self.observed_id = 0
        self.pending_id = 0
        self.released = False
        self.timer = None

    def reset(self):
        """Cancel the one-shot deadline and release only the pending transaction."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.pending_id = 0
        self.released = False

    def observe_ready(self, data: bytes, accepted_id: int) -> None:
        """Accept handler availability only for a newer, well-formed generation."""
        identity, separator, enabled = data.partition(b";")
        if (
            not separator
            or enabled not in (b"0", b"1")
            or not identity.isdigit()
            or len(identity) > PROMPT_ID_MAX_DIGITS
        ):
            return
        value = int(identity)
        if max(accepted_id, self.observed_id) < value < PROMPT_ID_LIMIT:
            self.observed_id = value
            self.ready_id = value if enabled == b"1" else 0


class ZshInterrupt(SubmissionInterrupt):
    """Wait for a verified TRAPINT response before completing a cancelled long line."""

    def __init__(self, shell):
        """Allocate acknowledgement state only for sessions using this policy."""
        super().__init__(shell)
        self.state = LineInterrupt()

    @property
    def pending(self) -> bool:
        """Keep the reader's input ownership until a fresh prompt is accepted."""
        return bool(self.state.pending_id)

    def before_cancel(self, staged: bool) -> None:
        """Arm a response deadline only while cancelling an active TTY lease."""
        if staged:
            self.state.pending_id = self.shell._accepted_prompt_id
            self.state.released = False
            self.state.timer = self.shell.loop.call_later(
                INTERRUPT_ACK_TIMEOUT, self.timeout
            )

    def hold_input(self, control: bytes | None, data: bytes) -> None:
        """Coalesce repeated interrupts so they cannot flush the release newline."""
        if control is not None and control in data:
            self.shell.dupin_buffer.clear()
            data = data[data.rfind(control) + len(control) :]
        self.shell.return_typeahead(data)

    def reset(self) -> None:
        """Release pending cancellation state at the engine's ownership boundary."""
        self.state.reset()

    def configure_sequencer(self, sequencer, markers) -> None:
        """Register the zsh template's capability and SIGINT acknowledgement frames."""
        sequencer.on_prefix(markers.scope(LINE_READER_READY_PREFIX), self.ready)
        sequencer.on_prefix(markers.scope(LINE_INTERRUPT_ACK_PREFIX), self.acknowledge)

    def validate_submission(self) -> None:
        """Reject long input if the current prompt has no verified default-like trap."""
        if self.state.ready_id != self.shell._accepted_prompt_id:
            raise InputRejected(
                "Not sent: long input requires ish's acknowledged SIGINT handler. A custom or removed trap is active; use short input or reconnect after restoring the default trap."
            )

    def ready(self, data: bytes) -> None:
        """Receive the reader's capability alongside its primary prompt."""
        self.state.observe_ready(data, self.shell._accepted_prompt_id)

    def timeout(self) -> None:
        """Fail closed if a removed or broken trap cannot acknowledge cancellation."""
        self.state.timer = None
        if self.state.pending_id and not self.state.released:
            self.shell._io_failed(
                RuntimeError("Shell did not acknowledge long-input cancellation")
            )

    def acknowledge(self, data: bytes) -> None:
        """Release exactly one newline after a matching reply from the owning shell."""
        state, shell = self.state, self.shell
        if (
            not state.pending_id
            or state.released
            or data != str(state.pending_id).encode("ascii")
        ):
            return
        if shell._closing_output or shell._stopping or shell._has_exited():
            return
        if (
            shell._prompt_id != state.pending_id
            or shell._context_id != state.pending_id
            or os.tcgetpgrp(shell.master_fd) != shell.shell_pid
        ):
            shell._io_failed(
                RuntimeError("Shell ownership changed during long-input cancellation")
            )
            return
        # TRAPINT returns 130 and sets zsh's interrupt flag before its reader
        # resumes. Only that response authorizes a LF to discard the old line.
        state.released = True
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        shell._write(shell.master_fd, b"\n")
        shell.input_observer.record(
            "line_interrupt_released", "SHELL", prompt_id=state.pending_id
        )


def shutdown(controller: ShellSignalController, signum: int) -> None:
    """Translate a process termination signal into the existing one-shot request."""
    controller.request_shutdown(signum)


def resize(controller: ShellSignalController, signum: int) -> None:
    """Update the PTY and active Python tool after a window-size signal."""
    controller.shell._resize()


@dataclass(frozen=True)
class ProcessSignal:
    """Register one process callback, or remove the inherited binding with None."""

    signum: int
    handler: Callable[[ShellSignalController, int], None] | None
    scope: SignalScope = SignalScope.STARTUP


@dataclass(frozen=True)
class TerminalSignal:
    """Associate a terminal control slot with a per-session handler factory."""

    signum: int
    control_index: int
    handler: type[TerminalSignalHandler] | None


@dataclass(frozen=True)
class SignalPolicy:
    """Optional adapter overrides; omitted signals retain the common defaults.

    None removes only ish's registration, never installs SIG_IGN. Process and
    terminal namespaces are independent even when they use the same signal.
    """

    process: tuple[ProcessSignal, ...] = ()
    terminal: tuple[TerminalSignal, ...] = ()


DEFAULT_PROCESS_SIGNALS = (
    ProcessSignal(signal.SIGTERM, shutdown),
    ProcessSignal(signal.SIGHUP, shutdown),
    ProcessSignal(signal.SIGWINCH, resize, SignalScope.RUNTIME),
)
DEFAULT_TERMINAL_SIGNALS = (
    TerminalSignal(signal.SIGINT, termios.VINTR, SubmissionInterrupt),
)


def _bindings(defaults, overrides):
    """Resolve immutable adapter overrides without changing shared defaults."""
    result = {binding.signum: binding for binding in defaults}
    for binding in overrides:
        result[binding.signum] = binding
    return tuple(binding for binding in result.values() if binding.handler is not None)


class ShellSignalController:
    """Own handler lifetimes and shell-specific terminal policies for one session."""

    def __init__(self, shell: InteractiveShell, policy: SignalPolicy):
        """Resolve optional adapter registrations without installing OS handlers yet."""
        self.shell = shell
        self.shutdown_signal: int | None = None
        self._session_task = None
        self._process = _bindings(DEFAULT_PROCESS_SIGNALS, policy.process)
        for binding in self._process:
            if binding.signum in (signal.SIGKILL, signal.SIGSTOP):
                raise ValueError("SIGKILL and SIGSTOP cannot have process handlers")
        self._terminal = tuple(
            (binding, binding.handler(shell))
            for binding in _bindings(DEFAULT_TERMINAL_SIGNALS, policy.terminal)
        )

    @contextlib.contextmanager
    def install(self, scope: SignalScope):
        """Install only this scope and unwind even a partially failed registration.

        Like asyncio signal registration itself, this must run in the main
        thread. The session must own these event-loop signal registrations;
        arbitrary preexisting asyncio callbacks cannot be read via its public API.
        Previous Python signal dispositions are restored after cleanup.
        """
        loop = self.shell.loop

        def restore(signum, previous):
            """Remove the loop callback before restoring the original disposition."""
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous)

        with contextlib.ExitStack() as handlers:
            for binding in self._process:
                if binding.scope is scope:
                    previous = signal.getsignal(binding.signum)
                    loop.add_signal_handler(
                        binding.signum, binding.handler, self, binding.signum
                    )
                    handlers.callback(restore, binding.signum, previous)
            yield

    def request_shutdown(self, signum: int) -> None:
        """Keep the first exit cause and never cancel cleanup a second time."""
        if self.shutdown_signal is not None:
            return
        self.shutdown_signal = signum
        shell = self.shell
        shell._native_input_active = False
        shell._send_generation += 1
        if (
            self._session_task is not None
            and not self._session_task.done()
            and not shell._stopping
        ):
            self._session_task.cancel()

    def notify_resize(self) -> None:
        """Use the selected WINCH policy while prompt-toolkit owns its OS handler."""
        for binding in self._process:
            if binding.signum == signal.SIGWINCH:
                binding.handler(self, signal.SIGWINCH)
                return

    async def run(self, session_factory):
        """Return the session result unless a requested shutdown takes precedence."""
        self.shutdown_signal = None
        self.shell._stopping = False
        with self.install(SignalScope.STARTUP):
            self._session_task = self.shell.loop.create_task(session_factory())
            try:
                status = await self._session_task
            except asyncio.CancelledError:
                if self.shutdown_signal is None:
                    raise
            finally:
                self._session_task = None
            if self.shutdown_signal is not None:
                return 128 + self.shutdown_signal
            return status

    @staticmethod
    def _control(attrs, index: int) -> bytes | None:
        """Read the configured signal character only when ISIG enables it."""
        if not attrs[3] & termios.ISIG:
            return None
        value = attrs[6][index]
        value = bytes((value,)) if isinstance(value, int) else bytes(value)
        return value if value != b"\0" else None

    def terminal_handler(self, signum: int) -> TerminalSignalHandler | None:
        """Return the session's selected handler without mutating adapter defaults."""
        return next(
            (
                handler
                for binding, handler in self._terminal
                if binding.signum == signum
            ),
            None,
        )

    def consume_input(self, data: bytes) -> bool:
        """Dispatch only guarded input; ordinary native input needs no TTY query."""
        shell = self.shell
        pending = next(
            (
                (binding, handler)
                for binding, handler in self._terminal
                if handler.pending
            ),
            None,
        )
        sending = shell._send_task is not None and not shell._send_task.done()
        if not self._terminal or not (pending or sending or shell._handoff_pending):
            return False
        attrs = termios.tcgetattr(shell.master_fd)
        if pending:
            binding, handler = pending
            handler.hold_input(self._control(attrs, binding.control_index), data)
            return True
        phase = InputPhase.SUBMISSION if sending else InputPhase.HANDOFF
        # If future policies register several keys, the first key in wire order
        # selects the batch owner. Each handler determines its remainder policy.
        matches = []
        for binding, handler in self._terminal:
            control = self._control(attrs, binding.control_index)
            if control is not None and control in data:
                matches.append((data.index(control), handler, control))
        if not matches:
            return False
        _, handler, control = min(matches, key=lambda match: match[0])
        handler.handle(control, data, phase)
        return True

    def configure_sequencer(self, sequencer, markers) -> None:
        """Bind only registered policies' session-scoped acknowledgement messages."""
        for _, handler in self._terminal:
            handler.configure_sequencer(sequencer, markers)

    def validate_submission(self) -> None:
        """Ask selected policies to check capabilities before a staged submission."""
        if self.terminal_handler(signal.SIGINT) is None:
            raise InputRejected(
                "Not sent: long input requires a registered terminal SIGINT policy."
            )
        for _, handler in self._terminal:
            handler.validate_submission()

    def reset(self) -> None:
        """Cancel pending policy deadlines when the reader is released or stopped."""
        for _, handler in self._terminal:
            handler.reset()

    def _signal_foreground(self, group: int | None, signum: int) -> bool:
        """Signal only a verified group in the shell's separately owned session."""
        proc = self.shell.proc
        if group is None or proc is None or group <= 0:
            return False
        try:
            if group == os.getpgrp() or os.getsid(group) != proc.pid:
                return False
            os.killpg(group, signum)
            return True
        except ProcessLookupError:
            return False

    async def stop_child(self) -> None:
        """Hang up and reap the shell, with bounded cleanup of its foreground job."""
        shell = self.shell
        foreground = None
        if self.shutdown_signal is not None and shell.master_fd is not None:
            with contextlib.suppress(OSError):
                foreground = os.tcgetpgrp(shell.master_fd)
                self._signal_foreground(foreground, signal.SIGTERM)
        deadline = shell.loop.time() + CHILD_SHUTDOWN_GRACE
        try:
            if shell.proc is not None:
                if shell.proc.returncode is None:
                    try:
                        # Interactive Bash ignores TERM; HUP also informs its jobs.
                        shell.proc.send_signal(signal.SIGHUP)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(shell.proc.wait(), CHILD_SHUTDOWN_GRACE)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            shell.proc.kill()
                        await shell.proc.wait()
                else:
                    await shell.proc.wait()
        finally:
            if foreground is not None:
                # The shell may exit before an uncooperative foreground job.
                # These checks only run during shutdown, never in idle sessions.
                with contextlib.suppress(OSError):
                    while self._signal_foreground(foreground, 0):
                        if shell.loop.time() >= deadline:
                            self._signal_foreground(foreground, signal.SIGKILL)
                            break
                        await asyncio.sleep(CHILD_SHUTDOWN_CHECK_INTERVAL)
