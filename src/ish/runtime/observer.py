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


class InputObserver:
    """Record overlapping consumers and reads outside their observed lifetimes.

    Tokens identify observations only. No caller may use them to authorize, reject,
    flush, or reroute input. Missing coverage (for example a plugin reading stdin
    directly) cannot be detected by this observer.
    """

    def __init__(self, *, enabled: bool = False, capacity: int = 4096) -> None:
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
        self._events.append(
            {
                "sequence": self._sequence,
                "time_ns": monotonic_ns(),
                "event": event,
                "owner": owner,
                "active": list(self._active.values()),
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
        self.record("begin", owner, token=token)
        if len(self._active) > 1:
            self._issues["overlap"] += 1
            self.record("overlap", owner, token=token)
        return token

    def end(self, token: int | None) -> None:
        """Observe release of this token without touching an actual input reader."""
        if token is None:
            return
        owner = self._active.pop(token, None)
        if owner is None:
            self._issues["unknown_release"] += 1
            self.record("unknown_release", token=token)
        else:
            self.record("end", owner, token=token)

    def read(self, owner: str, token: int | None, count: int, *, unit="bytes") -> None:
        """Observe an existing read result, preserving its bytes or parsed keys."""
        if not self.enabled:
            return
        if self._active.get(token) != owner:
            self._issues["inactive_read"] += 1
            self.record("inactive_read", owner, token=token or 0)
        self.record("read", owner, count=count, unit=unit, token=token or 0)

    def snapshot(self) -> dict:
        """Return an independent JSON-compatible snapshot, including trace truncation."""
        return {
            "enabled": self.enabled,
            "capacity": self._events.maxlen,
            "dropped_events": self._sequence - len(self._events),
            "active": list(self._active.values()),
            "totals": dict(self._totals),
            "issues": dict(self._issues),
            "events": [
                dict(event, active=list(event["active"])) for event in self._events
            ],
        }

    def terminal(self, owner: str, fd: int | None, boundary: str) -> None:
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
                "terminal_unavailable",
                owner,
                boundary=boundary,
                error=getattr(exc, "errno", 0) or 0,
            )
            return
        self.record(
            "terminal_state",
            owner,
            boundary=boundary,
            canonical=int(bool(attrs[3] & termios.ICANON)),
            echo=int(bool(attrs[3] & termios.ECHO)),
            signals=int(bool(attrs[3] & termios.ISIG)),
            foreground=foreground,
            rows=rows,
            columns=columns,
        )
