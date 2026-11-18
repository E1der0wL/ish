"""Schedule native selection gestures without duplicating text or range policy."""

from __future__ import annotations

import asyncio
import time


class SelectionController:
    """Own one local drag's timer; libghostty owns its range and click behavior."""

    AUTOSCROLL_SECONDS = 0.05

    def __init__(self, terminal, *, changed, copied):
        """Connect native gestures to renderer invalidation and explicit user copy."""
        self.terminal = terminal
        self.changed, self.copied = changed, copied
        self.active = False
        self._timer = None
        self._generation = 0
        self._closed = False
        self._pointer = (0, 0, None)
        self._rectangle = False

    def _stop(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _call(self, action, x=0, y=0, *, position=None):
        state = self.terminal.selection(
            action,
            x,
            y,
            position=position,
            rectangle=self._rectangle,
            time_ns=time.monotonic_ns(),
        )
        self.changed()
        return state

    def _schedule(self, state):
        if not self.active or not state.autoscroll:
            return
        self._timer = asyncio.get_running_loop().call_later(
            self.AUTOSCROLL_SECONDS, self._tick, self._generation
        )

    def _tick(self, generation):
        if self._closed or not self.active or generation != self._generation:
            return
        self._timer = None
        x, y, position = self._pointer
        self._schedule(self._call("autoscroll", x, y, position=position))

    def press(self, x, y, *, rectangle=False, position=None):
        """Begin a native click sequence; Alt rectangular policy is fixed at press."""
        if self._closed:
            return None
        self._stop()
        self.active = True
        self._pointer = (x, y, position)
        self._rectangle = rectangle
        state = self._call("press", x, y, position=position)
        self._schedule(state)
        return state

    def drag(self, x, y, *, position=None):
        """Retain out-of-pane coordinates so native selection chooses autoscroll."""
        if self._closed or not self.active:
            return None
        self._stop()
        self._pointer = (x, y, position)
        state = self._call("drag", x, y, position=position)
        self._schedule(state)
        return state

    def release(self, x, y, *, position=None):
        """End the drag, preserving native repeat-click state and selected text."""
        if self._closed or not self.active:
            return None
        self._stop()
        self.active = False
        self._pointer = (x, y, position)
        state = self._call("release", x, y, position=position)
        if self.terminal.selection_text():
            self.copied()
        return state

    def reset(self):
        """Cancel capture and queued ticks, leaving the native highlight intact."""
        self._stop()
        self.active = False
        if not self._closed:
            self._call("reset")
        self._pointer = (0, 0, None)
        self._rectangle = False

    def close(self):
        """Release the gesture before the terminal and ignore late timer callbacks."""
        if self._closed:
            return
        self.reset()
        self._closed = True
