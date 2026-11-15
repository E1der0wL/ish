"""Own one popup handoff's identity and allowed lifecycle transitions.

Closing a popup, including cancelling its value, is a normal widget return.
The handoff completes only after the done reply is queued and a native primary
arrives. A cancelled handoff may remain attached to App until that input boundary;
its terminal outcome and the lifetime of held native input are separate facts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    """Distinguish UI release, queued reply, native completion and cancellation."""

    WAITING = "waiting"
    POPUP = "popup"
    RETURNING = "returning"
    RESUMING = "resuming"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


TRANSITIONS = {
    Stage.WAITING: frozenset((Stage.POPUP, Stage.CANCELLED)),
    Stage.POPUP: frozenset((Stage.RETURNING, Stage.CANCELLED)),
    Stage.RETURNING: frozenset((Stage.RESUMING, Stage.CANCELLED)),
    Stage.RESUMING: frozenset((Stage.COMPLETE, Stage.CANCELLED)),
    Stage.COMPLETE: frozenset(),
    Stage.CANCELLED: frozenset(),
}
TERMINAL = frozenset((Stage.COMPLETE, Stage.CANCELLED))


@dataclass
class Request:
    """Correlate one forwarded shortcut with one native boundary and helper."""

    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    pending: bytearray = field(default_factory=bytearray)
    _serial: int | None = field(default=None, init=False)
    _stage: Stage = field(default=Stage.WAITING, init=False)

    @property
    def serial(self) -> int | None:
        """Return the first native boundary identity; it cannot be rebound."""
        return self._serial

    @property
    def stage(self) -> Stage:
        """Expose lifecycle state without allowing callers to assign transitions."""
        return self._stage

    def _advance(self, target: Stage) -> bool:
        if target not in TRANSITIONS[self._stage]:
            return False
        self._stage = target
        if target in TERMINAL:
            self.ready.set()
            self.finished.set()
        return True

    def bind(self, serial: int) -> bool:
        """Latch one real boundary without granting focus to a helper."""
        if (
            self.stage != Stage.WAITING
            or self.serial is not None
            or type(serial) is not int
            or serial <= 0
        ):
            return False
        self._serial = serial
        self.ready.set()
        return True

    def claim(self, serial: int) -> bool:
        """Grant this handoff once, only to its matching native helper."""
        if type(serial) is not int or self.serial is None or serial != self.serial:
            return False
        return self._advance(Stage.POPUP)

    def begin_return(self) -> bool:
        """Record popup focus release while retaining the native input barrier."""
        if not self._advance(Stage.RETURNING):
            return False
        self.finished.set()
        return True

    def reply_queued(self) -> bool:
        """Record a queued done reply, without asserting receipt or native readiness."""
        return self._advance(Stage.RESUMING)

    def complete(self) -> bool:
        """Finish a normal return when the coordinator receives the next primary."""
        return self._advance(Stage.COMPLETE)

    def cancel(self) -> bool:
        """Revoke an unfinished exchange without changing a terminal outcome."""
        return self._advance(Stage.CANCELLED)
