"""Validate shell submissions and temporarily manage command-input PTY settings.

Only an acknowledged primary prompt may use this transport. The terminating
newline stays with the sender until the noncanonical prefix has been consumed
and the saved terminal settings have been restored. Parser recovery must be
verified separately for every adapter; queue readiness alone cannot prove it.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import termios
from enum import Enum

from .constants import CSI, TIOCGPTPEER
from .limits import TYPEAHEAD_LIMIT_BYTES

CANONICAL_LINE_BYTES = 4095
DISABLED_CONTROL = b"\0"  # Linux N_TTY's disabled c_cc entry.
PASTE_START = CSI + b"200~"
PASTE_END = CSI + b"201~"
_PASTE_MARKER = re.compile(b"|".join(map(re.escape, (PASTE_START, PASTE_END))))
_PASTE_ESCAPE = PASTE_START[:1]
_PASTE_PREFIX_LIMIT = max(len(PASTE_START), len(PASTE_END)) - 1

# Validate only the staged prefix; the rest may be the next reader's stdin.
_UNSAFE_STAGED_CONTROL = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")
_FORBIDDEN_INPUT_FLAGS = termios.INLCR | termios.ISTRIP | getattr(termios, "IUCLC", 0)
_FORBIDDEN_LOCAL_FLAGS = getattr(termios, "EXTPROC", 0) | getattr(termios, "PENDIN", 0)
_STAGED_CONTROL_INDICES = tuple(
    getattr(termios, name)
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
    )
    if hasattr(termios, name)
)
_POLL_CLOSED = select.POLLERR | select.POLLHUP | select.POLLNVAL


class ForegroundPaste:
    """Track explicit paste boundaries offered to one foreground shell reader."""

    __slots__ = ("_last", "_prefix")

    def __init__(self) -> None:
        """Start with no observed paste."""
        self.reset()

    def reset(self, *, enabled: bool = True) -> None:
        """Start a fresh observation or invalidate the cancelled batch."""
        self._last = b"" if enabled else None
        self._prefix = b""

    def observe(self, data: bytes | bytearray) -> None:
        """Track complete delimiters and keep a possible split delimiter."""
        if self._last is None or (not self._prefix and _PASTE_ESCAPE not in data):
            return
        if self._prefix:
            data = self._prefix + data
        for marker in _PASTE_MARKER.finditer(data):
            current = marker[0]
            if current == PASTE_END and self._last != PASTE_START:
                # A stray closer makes an orphan in the return ambiguous.
                self.reset(enabled=False)
                return
            self._last = current
        esc = data.rfind(_PASTE_ESCAPE, -_PASTE_PREFIX_LIMIT)
        self._prefix = bytes(data[esc:]) if esc >= 0 else b""

    def returned_end(self, data: bytearray) -> int:
        """Identify the end of an orphan paste only with matching offer evidence."""
        if self._last and data:
            first = _PASTE_MARKER.search(data)
            if first is not None and first[0] == PASTE_END:
                return first.end()
        return 0


def submission_echo(data: bytes, attrs: list) -> bytes:
    """Project submitted bytes into the echo masked by the shell output stream."""
    if not attrs[3] & termios.ECHO:
        return b""
    if attrs[1] & termios.OPOST and attrs[1] & termios.ONLCR:
        return data.replace(b"\n", b"\r\n")
    return data


class InputHandoffMode(Enum):
    """Select input transfer independently of state-refresh script availability.

    PROMPT_ACK preserves returned submissions and holds new keys until the
    primary prompt and its context acknowledge the handoff. NATIVE keeps
    following input with the shell until an explicit reconnect. Neither mode
    changes which editor handles continuation input.
    """

    PROMPT_ACK = "prompt_ack"
    NATIVE = "native"


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
    """Retain native bytes returned after a block or native continuation input.

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
        self.active = enabled and data.find(b"\n", 0, -1) >= 0

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
    if prefix and _UNSAFE_STAGED_CONTROL.search(data, 0, prefix):
        raise InputRejected(
            "Not sent: long first lines cannot contain control characters other than tabs."
        )
    return prefix


def control_character(attrs: list, index: int) -> bytes:
    """Read one termios c_cc slot regardless of its integer or bytes representation."""
    value = attrs[6][index]
    return bytes((value,)) if isinstance(value, int) else bytes(value)


def check_terminal(attrs: list, prefix: bytes) -> None:
    """Reject unverified input modes and active special characters in the prefix."""
    if (
        not attrs[3] & termios.ICANON
        or not attrs[3] & termios.ISIG
        or attrs[0] & _FORBIDDEN_INPUT_FLAGS
        or attrs[3] & _FORBIDDEN_LOCAL_FLAGS
    ):
        raise InputRejected(
            "Not sent: the shell's current TTY mode is not verified for long input."
        )
    for index in _STAGED_CONTROL_INDICES:
        value = control_character(attrs, index)
        if value != DISABLED_CONTROL and (value in prefix or value == b"\n"):
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
        if any(event & _POLL_CLOSED for _, event in events):
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
