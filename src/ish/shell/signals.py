"""Dispatch process signals and terminal control keys through per-shell policies.

Only explicitly registered process signals acquire asyncio handlers. Terminal
keys are a separate input path: writing VINTR to the PTY signals its foreground
group, not ish. Policies decide when to cancel; the engine owns all input queues,
writer ordering, and terminal leases. No idle task or polling is added.
Common handlers and session lifecycle control live here; adapters select
optional shell-specific handlers from adapter.handlers.
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

import psutil

from .input import InputRejected

if TYPE_CHECKING:
    from .base import InteractiveShell

CHILD_SHUTDOWN_GRACE = 2.0
CHILD_SHUTDOWN_CHECK_INTERVAL = 0.05


class SignalScope(Enum):
    """Separate early termination handling from handlers requiring a live PTY."""

    STARTUP = "startup"
    RUNTIME = "runtime"


class InputPhase(Enum):
    """Identify the existing input boundary at which a terminal key was received."""

    SUBMISSION = "submission"
    HANDOFF = "handoff"
    CONTINUATION = "continuation"


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

    @property
    def native_continuation(self) -> bool:
        """Opt into native continuation dispatch while the shell owns its reader."""
        return False

    def handle(self, control: bytes, data: bytes, phase: InputPhase) -> bool | None:
        """Consume a matched key, or return False to keep the batch on its native path."""
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
            shell.cancel_handoff(control, remaining)
            return
        # VINTR can resume another reader before a cancelled task's finally.
        staged = shell.restore_input_mode()
        self.before_cancel(staged)
        shell.cancel_submission(control, remaining)


def shutdown(controller: ShellSignalController, signum: int) -> None:
    """Translate a process termination signal into the existing one-shot request."""
    controller.request_shutdown(signum)


def resize(controller: ShellSignalController, signum: int) -> None:
    """Update the PTY and active Python tool after a window-size signal."""
    controller.shell.resize()


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


class _ForegroundGroup:
    """Retain live member identities for one foreground group during shutdown.

    Capture before signalling the shell, while its children still belong to the
    original session. Process ancestry is not required. A retained member can
    authorize group delivery after the leader exits, including delivery to new
    members of that same group. Once all retained members disappear or leave,
    fail closed instead of adopting a potentially reused group/session number.
    No process enumeration or identity checks run during interactive input.
    """

    def __init__(self, group: int, session: int):
        """Snapshot only this group's members, without using process_iter's cache as identity."""
        self.group = group
        self.session = session
        self.members: list[psutil.Process] = []
        if group <= 0 or session <= 0 or group == os.getpgrp():
            return
        try:
            for candidate in psutil.process_iter():
                try:
                    if (
                        os.getpgid(candidate.pid) != group
                        or os.getsid(candidate.pid) != session
                    ):
                        continue
                    # process_iter caches Process instances across calls. A fresh
                    # object records the current PID/create-time identity instead.
                    member = psutil.Process(candidate.pid)
                    member.create_time()
                    if self._matches(member):
                        self.members.append(member)
                except (OSError, psutil.Error):
                    continue
        except (OSError, psutil.Error):
            # Partial visibility may still yield a verified surviving member.
            pass

    def _matches(self, member: psutil.Process) -> bool:
        """Recheck identity and membership, excluding zombies that cannot be signalled."""
        try:
            return (
                member.is_running()
                and os.getpgid(member.pid) == self.group
                and os.getsid(member.pid) == self.session
                and member.status() != psutil.STATUS_ZOMBIE
                and member.is_running()
            )
        except (OSError, psutil.Error):
            return False

    def send(self, signum: int) -> bool:
        """Signal the group only while a captured live member still verifies ownership."""
        if self.group <= 0 or self.group == os.getpgrp():
            return False
        for member in self.members:
            if self._matches(member):
                try:
                    os.killpg(self.group, signum)
                    return True
                except OSError:
                    return False
        return False


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
        shell.invalidate_input()
        if (
            self._session_task is not None
            and not self._session_task.done()
            and not shell.stopping
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
        phase = shell.signal_input_phase
        native = any(handler.native_continuation for _, handler in self._terminal)
        if not self._terminal or not (
            pending or phase is not InputPhase.CONTINUATION or native
        ):
            return False
        attrs = termios.tcgetattr(shell.master_fd)
        if pending:
            binding, handler = pending
            handler.hold_input(self._control(attrs, binding.control_index), data)
            return True
        # If future policies register several keys, the first key in wire order
        # selects the batch owner. Each handler determines its remainder policy.
        matches = []
        for binding, handler in self._terminal:
            if phase is InputPhase.CONTINUATION and not handler.native_continuation:
                continue
            control = self._control(attrs, binding.control_index)
            if control is not None and control in data:
                matches.append((data.index(control), handler, control))
        if not matches:
            return False
        _, handler, control = min(matches, key=lambda match: match[0])
        return handler.handle(control, data, phase) is not False

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

    async def stop_child(self) -> None:
        """Hang up and reap the shell, with bounded cleanup of its foreground job."""
        shell = self.shell
        foreground = None
        if (
            self.shutdown_signal is not None
            and shell.master_fd is not None
            and shell.proc is not None
        ):
            with contextlib.suppress(OSError):
                foreground = _ForegroundGroup(
                    os.tcgetpgrp(shell.master_fd), shell.proc.pid
                )
                foreground.send(signal.SIGTERM)
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
                    while foreground.send(0):
                        if shell.loop.time() >= deadline:
                            foreground.send(signal.SIGKILL)
                            break
                        await asyncio.sleep(CHILD_SHUTDOWN_CHECK_INTERVAL)
