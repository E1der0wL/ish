"""Implement shell-specific signal policies selected by the adapter registry.

The common signal controller owns handler lifetimes; these handlers use the
engine's input API and never register process signals themselves.
"""

from __future__ import annotations

import os

from ..constants import (
    LINE_INTERRUPT_ACK_PREFIX,
    LINE_READER_READY_PREFIX,
    PROMPT_ID_LIMIT,
    PROMPT_ID_MAX_DIGITS,
)
from ..input import InputRejected
from ..signals import InputPhase, SubmissionInterrupt

INTERRUPT_ACK_TIMEOUT = 5.0


class LineInterrupt:
    """Track a generation-scoped cancellation acknowledgement without idle polling."""

    def __init__(self):
        """Start with no verified handler and no outstanding cancellation."""
        self.ready_id = 0
        self.observed_id = 0
        self.pending_id = 0
        self.released = False
        self.continuation = False
        self.timer = None

    def reset(self):
        """Cancel the one-shot deadline and release only the pending transaction."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.pending_id = 0
        self.released = False
        self.continuation = False

    def observe_ready(self, data: bytes, accepted_id: int) -> None:
        """Accept fresh readiness, or revoke the current generation at preexec."""
        identity, separator, enabled = data.partition(b";")
        if (
            not separator
            or enabled not in (b"0", b"1")
            or not identity.isdigit()
            or len(identity) > PROMPT_ID_MAX_DIGITS
        ):
            return
        value = int(identity)
        # preexec may revoke reader ownership within the accepted generation.
        # It cannot reenable that generation, even if a ready frame is replayed.
        if value == self.observed_id and enabled == b"0":
            self.ready_id = 0
        if max(accepted_id, self.observed_id) < value < PROMPT_ID_LIMIT:
            self.observed_id = value
            self.ready_id = value if enabled == b"1" else 0


class ZshInterrupt(SubmissionInterrupt):
    """Release cancelled no-ZLE input only after a verified reader acknowledgement."""

    def __init__(self, shell):
        """Allocate acknowledgement state only for sessions using this policy."""
        super().__init__(shell)
        self.state = LineInterrupt()

    @property
    def pending(self) -> bool:
        """Keep the reader's input ownership until a fresh prompt is accepted."""
        return bool(self.state.pending_id)

    @property
    def native_continuation(self) -> bool:
        """Guard continuation keys only until preexec revokes reader ownership."""
        return bool(
            self.state.ready_id
            and self.state.ready_id == self.shell.accepted_prompt_id
            and self.shell.continuation_active is not None
            and self.shell.continuation_active.is_set()
        )

    def handle(self, control: bytes, data: bytes, phase: InputPhase) -> bool | None:
        """Extend acknowledged cancellation to native continuation input."""
        if phase is not InputPhase.CONTINUATION:
            return super().handle(control, data, phase)
        if os.tcgetpgrp(self.shell.master_fd) != self.shell.shell_pid:
            return False
        self.before_cancel(True)
        self.state.continuation = True
        remaining = data[data.rfind(control) + len(control) :]
        self.shell.cancel_handoff(control, remaining)
        return True

    def before_cancel(self, staged: bool) -> None:
        """Arm a response deadline for a staged line or native continuation."""
        if staged:
            self.state.pending_id = self.shell.accepted_prompt_id
            self.state.released = False
            self.state.timer = self.shell.loop.call_later(
                INTERRUPT_ACK_TIMEOUT, self.timeout
            )

    def hold_input(self, control: bytes | None, data: bytes) -> None:
        """Coalesce repeated interrupts so they cannot flush the release newline."""
        if control is not None and control in data:
            self.shell.discard_typeahead()
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
        """Reject long input unless both interrupt and reader hooks are verified."""
        if self.state.ready_id != self.shell.accepted_prompt_id:
            raise InputRejected(
                "Not sent: long input requires ish's verified SIGINT and preexec hooks. A hook is unavailable or a custom trap is active; use short input or reconnect after restoring the default trap."
            )

    def ready(self, data: bytes) -> None:
        """Track readiness and release input if command execution won the race."""
        self.state.observe_ready(data, self.shell.accepted_prompt_id)
        if (
            self.state.continuation
            and self.state.pending_id
            and not self.state.released
            and not self.state.ready_id
        ):
            self.reset()
            self.shell.resume_native_typeahead()

    def timeout(self) -> None:
        """Fail closed if a removed or broken trap cannot acknowledge cancellation."""
        self.state.timer = None
        if self.state.pending_id and not self.state.released:
            self.shell.fail_io(
                RuntimeError("Shell did not acknowledge input cancellation")
            )

    def acknowledge(self, data: bytes) -> None:
        """Release exactly one newline after a matching reply from the owning shell."""
        state, shell = self.state, self.shell
        identity, separator, reading = data.partition(b";")
        if (
            not state.pending_id
            or state.released
            or identity != str(state.pending_id).encode("ascii")
            or not separator
            or reading not in (b"0", b"1")
        ):
            return
        if not shell.input_open:
            return
        if reading == b"0" and state.continuation:
            self.reset()
            shell.resume_native_typeahead()
            return
        if (
            reading != b"1"
            or not shell.prompt_matches(state.pending_id)
            or os.tcgetpgrp(shell.master_fd) != shell.shell_pid
        ):
            shell.fail_io(
                RuntimeError("Shell ownership changed during input cancellation")
            )
            return
        # TRAPINT returns 130 and sets zsh's interrupt flag before its reader
        # resumes. Only that response authorizes a LF to discard the old line.
        state.released = True
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        shell.write_control(b"\n")
        shell.input_observer.record(
            "line_interrupt_released", "SHELL", prompt_id=state.pending_id
        )
