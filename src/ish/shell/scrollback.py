"""Retain bounded shell output history and the last command output."""

from __future__ import annotations

import itertools
from collections import deque

from .limits import SCROLLBACK_MAX_BYTES, SCROLLBACK_MAX_LINES

__all__ = ["Scrollback"]


class Scrollback:
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
            self.append_output(
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
        # The retained partial line has no LF; scan only the newly received bytes.
        parts = bytes(data).split(b"\n")
        if len(parts) > 1:
            first_line = b"".join((self._partial_line, parts[0], b"\n"))
            self._partial_line.clear()
            self._add_line(first_line)
            for part in parts[1:-1]:
                self._add_line(part + b"\n")
        self._partial_line.extend(parts[-1])
        self._trim_history()

    def append_output(self, data):
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
