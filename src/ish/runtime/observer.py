"""Observe input consumers without controlling readers, queues, or terminal modes.

The bounded trace stores counts and lifecycle metadata, never input contents. It is
disabled by default and performs no file I/O. All observations run on the owning
event-loop thread; snapshots can be exported explicitly outside the input path.
"""

from __future__ import annotations

import os
import termios
from collections import Counter, deque
from time import monotonic_ns

# Stable labels exposed by trace snapshots. They do not grant input ownership.
EDITOR = "EDITOR"
SHELL = "SHELL"
TOOL = "TOOL"
PROCESS_EXIT = "process_exit"
_DEFAULT_CAPACITY = 4096
_DEFAULT_UNIT = "bytes"

_BEGIN = "begin"
_END = "end"
_READ = "read"
_OVERLAP = "overlap"
_UNKNOWN_RELEASE = "unknown_release"
_INACTIVE_READ = "inactive_read"
_TERMINAL_STATE = "terminal_state"
_TERMINAL_UNAVAILABLE = "terminal_unavailable"

# Built-in fields of the JSON snapshot. Callers may add event metadata.
_SEQUENCE = "sequence"
_TIME_NS = "time_ns"
_EVENT = "event"
_OWNER = "owner"
_ACTIVE = "active"
_ENABLED = "enabled"
_CAPACITY = "capacity"
_DROPPED = "dropped_events"
_TOTALS = "totals"
_ISSUES = "issues"
_EVENTS = "events"
_EVENT_HEADER = frozenset((_SEQUENCE, _TIME_NS, _EVENT, _OWNER, _ACTIVE))


class InputObserver:
    """Record overlapping consumers and reads outside their observed lifetimes.

    Tokens identify observations only. No caller may use them to authorize, reject,
    flush, or reroute input. Missing coverage (for example a plugin reading stdin
    directly) cannot be detected by this observer.
    """

    def __init__(
        self, *, enabled: bool = False, capacity: int = _DEFAULT_CAPACITY
    ) -> None:
        """Prepare a bounded event ring and cumulative counters."""
        if capacity < 1:
            raise ValueError("Input trace capacity must be positive")
        self.enabled = enabled
        self._events: deque[dict] = deque(maxlen=capacity)
        self._active: dict[int, str] = {}
        self._sequence = 0
        self._token = 0
        self._totals: Counter[str] = Counter()
        self._issues: Counter[str] = Counter()

    def record(self, event: str, owner: str = "", **metadata: int | str) -> None:
        """Append metadata without reading input or changing execution decisions."""
        if not self.enabled:
            return
        self._sequence += 1
        self._totals[event] += 1
        if not _EVENT_HEADER.isdisjoint(metadata):
            metadata = {
                key: value
                for key, value in metadata.items()
                if key not in _EVENT_HEADER
            }
        self._events.append(
            {
                _SEQUENCE: self._sequence,
                _TIME_NS: monotonic_ns(),
                _EVENT: event,
                _OWNER: owner,
                _ACTIVE: list(self._active.values()),
                **metadata,
            }
        )

    def begin(self, owner: str) -> int | None:
        """Observe a consumer becoming active; overlaps are recorded, not rejected."""
        if not self.enabled:
            return None
        self._token += 1
        token = self._token
        self._active[token] = owner
        self.record(_BEGIN, owner, token=token)
        if len(self._active) > 1:
            self._issues[_OVERLAP] += 1
            self.record(_OVERLAP, owner, token=token)
        return token

    def end(self, token: int | None) -> None:
        """Observe release of this token without touching an actual input reader."""
        if token is None:
            return
        owner = self._active.pop(token, None)
        if owner is None:
            self._issues[_UNKNOWN_RELEASE] += 1
            self.record(_UNKNOWN_RELEASE, token=token)
        else:
            self.record(_END, owner, token=token)

    def read(
        self, owner: str, token: int | None, count: int, *, unit=_DEFAULT_UNIT
    ) -> None:
        """Observe an existing read result, preserving its bytes or parsed keys."""
        if not self.enabled:
            return
        if self._active.get(token) != owner:
            self._issues[_INACTIVE_READ] += 1
            self.record(_INACTIVE_READ, owner, token=token or 0)
        self.record(_READ, owner, count=count, unit=unit, token=token or 0)

    def snapshot(self) -> dict:
        """Return an independent JSON-compatible snapshot, including trace truncation."""
        return {
            _ENABLED: self.enabled,
            _CAPACITY: self._events.maxlen,
            _DROPPED: self._sequence - len(self._events),
            _ACTIVE: list(self._active.values()),
            _TOTALS: dict(self._totals),
            _ISSUES: dict(self._issues),
            _EVENTS: [
                event | {_ACTIVE: list(event[_ACTIVE])} for event in self._events
            ],
        }

    def sample(self, owner: str, fd: int | None, boundary: str) -> None:
        """Sample terminal metadata at transitions only, without changing any modes.

        A foreground group or raw mode does not prove who is reading input. These
        values are diagnostic evidence only; unavailable descriptors are harmless.
        Disabled observation performs no terminal syscalls.
        """
        if not self.enabled or fd is None:
            return
        try:
            attrs = termios.tcgetattr(fd)
            rows, columns = termios.tcgetwinsize(fd)
            foreground = os.tcgetpgrp(fd)
        except (OSError, termios.error, ValueError, TypeError) as exc:
            self.record(
                _TERMINAL_UNAVAILABLE,
                owner,
                boundary=boundary,
                error=getattr(exc, "errno", 0) or 0,
            )
            return
        self.record(
            _TERMINAL_STATE,
            owner,
            boundary=boundary,
            canonical=int(bool(attrs[3] & termios.ICANON)),
            echo=int(bool(attrs[3] & termios.ECHO)),
            signals=int(bool(attrs[3] & termios.ISIG)),
            foreground=foreground,
            rows=rows,
            columns=columns,
        )
