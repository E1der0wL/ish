"""One-shot editor grants tied to a confirmed shell input boundary.

The session proves the boundary before granting input. These local request IDs
identify editor attempts, not shell commands or wire messages. A retry after
Ctrl+C or a Python tool can use the same wire boundary with a new request ID.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EditRequest:
    """Permit one editor reply at a shell boundary, with explicit dispatch policy."""

    id: int
    boundary: int
    commands: bool


class EditState:
    """Own the active grant; stale replies cannot consume a later request."""

    def __init__(self):
        """Start without an editor grant or issued request IDs."""
        self._serial = 0
        self._current: EditRequest | None = None

    @property
    def current(self) -> EditRequest | None:
        """Expose the immutable grant without allowing another owner to replace it."""
        return self._current

    def grant(self, boundary: int, *, commands: bool) -> EditRequest:
        """Issue one grant after the session has confirmed the supplied boundary."""
        if boundary <= 0:
            raise ValueError("An editor grant requires a confirmed boundary")
        if self._current is not None:
            raise RuntimeError("An editor request is already active")
        self._serial += 1
        self._current = EditRequest(self._serial, boundary, commands)
        return self._current

    def take(self, request: EditRequest) -> bool:
        """Consume exactly this grant once, rejecting copies and foreign requests."""
        if self._current is None or self._current is not request:
            return False
        self._current = None
        return True

    def revoke(self) -> None:
        """Withdraw the grant when the session changes or closes its input boundary."""
        self._current = None
