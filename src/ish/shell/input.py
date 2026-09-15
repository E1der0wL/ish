"""Validate shell submissions and temporarily manage command-input PTY settings.

Only an acknowledged primary prompt may use this transport. The terminating
newline stays with the sender until the noncanonical prefix has been consumed
and the saved terminal settings have been restored. Parser recovery must be
verified separately for every adapter; queue readiness alone cannot prove it.
"""

from __future__ import annotations

import fcntl
import os
import select
import termios
from enum import Enum

from .constants import TIOCGPTPEER
from .limits import TYPEAHEAD_LIMIT_BYTES

CANONICAL_LINE_BYTES = 4095


class LongInputMode(Enum):
    """Select an adapter's verified transport for an oversized physical line."""

    REJECT = "reject"
    STAGED_FIRST_LINE = "staged_first_line"


class InputRejected(ValueError):
    """Reject a submission before transmitting it, retaining its byte position."""

    def __init__(self, message: str, byte_offset: int = 0):
        """Keep a displayable explanation and the offending physical line's offset."""
        super().__init__(message)
        self.byte_offset = byte_offset


class SubmittedInput:
    """Retain native bytes returned during one accepted multiline submission.

    A PTY has already interpreted terminal controls, so its returned bytes are
    not fresh editor key presses. Keep this stream separate from keys retained
    before PTY delivery. Do not infer provenance by comparing byte contents.
    """

    def __init__(self) -> None:
        """Start outside a submission, with no returned native input."""
        self.active = False
        self.returned = bytearray()

    def begin(self, data: bytes, *, enabled: bool) -> None:
        """Enable native resumption only for accepted multiline shell blocks."""
        self.clear()
        self.active = enabled and b"\n" in data.removesuffix(b"\n")

    def append(self, data: bytes) -> None:
        """Bound retained native input independently of the editor key queue."""
        if len(self.returned) + len(data) > TYPEAHEAD_LIMIT_BYTES:
            raise BufferError("Returned submission exceeds the pending input limit")
        self.returned.extend(data)

    def take(self) -> bytes:
        """Detach the full returned stream once, without splitting shell syntax."""
        data = bytes(self.returned)
        self.returned.clear()
        return data

    def clear(self) -> None:
        """Forget a completed or cancelled submission and its pending bytes."""
        self.active = False
        self.returned.clear()


def staged_prefix(data: bytes, mode: LongInputMode, shell: str) -> int:
    """Validate the entire block and return the long first-line length, or zero.

    Count encoded bytes, not display columns or Unicode characters. Later lines
    can belong to a program's stdin; do not infer ownership from shell syntax.
    """
    if len(data) <= CANONICAL_LINE_BYTES:
        return 0
    start = 0
    prefix = 0
    while start < len(data):
        end = data.find(b"\n", start)
        if end < 0:
            end = len(data)
        if end - start > CANONICAL_LINE_BYTES:
            if mode is not LongInputMode.STAGED_FIRST_LINE:
                raise InputRejected(
                    f"Not sent: {shell} does not support lines over "
                    f"{CANONICAL_LINE_BYTES} bytes in ish yet.",
                    start,
                )
            if start:
                raise InputRejected(
                    "Not sent: long lines are supported only on the first line "
                    "at the primary prompt. Shorten the later line.",
                    start,
                )
            prefix = end
        start = end + 1
    if prefix and any(byte < 32 and byte != 9 or byte == 127 for byte in data[:prefix]):
        raise InputRejected(
            "Not sent: long first lines cannot contain control characters other than tabs."
        )
    return prefix


def check_terminal(attrs: list, prefix: bytes) -> None:
    """Reject unverified input modes and active special characters in the prefix."""
    forbidden_input = sum(
        getattr(termios, name, 0) for name in ("INLCR", "ISTRIP", "IUCLC")
    )
    forbidden_local = sum(getattr(termios, name, 0) for name in ("EXTPROC", "PENDIN"))
    if (
        not attrs[3] & termios.ICANON
        or not attrs[3] & termios.ISIG
        or attrs[0] & forbidden_input
        or attrs[3] & forbidden_local
    ):
        raise InputRejected(
            "Not sent: the shell's current TTY mode is not verified for long input."
        )
    for name in (
        "VINTR",
        "VQUIT",
        "VSUSP",
        "VSTART",
        "VSTOP",
        "VLNEXT",
        "VDISCARD",
        "VERASE",
        "VKILL",
        "VWERASE",
        "VREPRINT",
        "VEOL",
        "VEOL2",
        "VEOF",
    ):
        index = getattr(termios, name, None)
        if index is None:
            continue
        value = attrs[6][index]
        value = bytes([value]) if isinstance(value, int) else value
        if value != b"\0" and (value in prefix or value == b"\n"):
            raise InputRejected(
                "Not sent: long input conflicts with a configured TTY control character."
            )


class InputModeLease:
    """Own a temporary slave descriptor and restore exactly one settings snapshot."""

    def __init__(self, master: int, prefix: bytes):
        """Open and validate the peer without changing its settings yet."""
        self.fd = fcntl.ioctl(
            master,
            TIOCGPTPEER,
            os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        self.active = False
        try:
            self.saved = termios.tcgetattr(self.fd)
            check_terminal(self.saved, prefix)
            self.reader = select.poll()
            self.reader.register(self.fd, select.POLLIN)
        except BaseException:
            os.close(self.fd)
            raise

    def start(self) -> None:
        """Disable canonical accumulation and echo for the newline-free prefix."""
        attrs = list(self.saved)
        attrs[6] = list(self.saved[6])
        attrs[3] &= ~(termios.ICANON | termios.ECHO)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.active = True

    def pending(self) -> bool:
        """Check slave readiness, including Linux N_TTY's pending flip-buffer work.

        Do not substitute a bare TIOCINQ check: it can report zero before queued
        flip-buffer data has reached N_TTY. No other sender may run during this
        lease, and the shell adapter must retain a whole physical input line.
        """
        events = self.reader.poll(0)
        if any(
            event & (select.POLLERR | select.POLLHUP | select.POLLNVAL)
            for _, event in events
        ):
            raise OSError("Shell terminal closed during long input")
        return bool(events)

    def restore(self) -> None:
        """Restore before newline delivery or an interrupt can activate another reader."""
        if self.active:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            self.active = False

    def close(self) -> None:
        """Restore on every exit path and release the transient slave descriptor."""
        try:
            self.restore()
        finally:
            os.close(self.fd)
