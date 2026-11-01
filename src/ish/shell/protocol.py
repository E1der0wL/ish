"""Shared context identities, FIFO framing, and native helper call layouts.

FIFO framing: SOH ISH2 RS (category US base64(body) RS ... ) EOT.

Version 2 bodies cannot contain frame/record delimiters. Environments use
NUL-separated NAME=value records to preserve embedded newlines in values.
Legacy unversioned frames remain readable during integration script updates.
"""

import base64
from dataclasses import dataclass

from .constants import PROMPT_ID_LIMIT, PROMPT_ID_MAX_DIGITS

SOH, RS, US, EOT = b"\x01", b"\x1e", b"\x1f", b"\x04"
VERSION = b"ISH2"


def parse_prompt_id(raw: bytes) -> int | None:
    """Read a bounded decimal identity; callers own freshness and transition checks.

    Zero remains valid for context frames before a prompt has been accepted.
    """
    if not raw or len(raw) > PROMPT_ID_MAX_DIGITS or not raw.isdigit():
        return None
    value = int(raw)
    return value if value < PROMPT_ID_LIMIT else None


@dataclass(frozen=True)
class ForwardCommand:
    """Declare one native helper call and project its argv layout into C and shell."""

    option: str
    arguments: tuple[str, ...] = ()

    @property
    def argc(self) -> int:
        """Return the argument count including the executable and optional flag."""
        return 1 + bool(self.option) + len(self.arguments)

    def index(self, argument: str) -> int:
        """Return the C argv index of a declared argument."""
        return 1 + bool(self.option) + self.arguments.index(argument)

    def shell(self, **arguments: str) -> str:
        """Order already quoted shell expressions without changing their quoting."""
        if arguments.keys() != set(self.arguments):
            raise ValueError(f"Expected helper arguments: {self.arguments!r}")
        words = (self.option,) if self.option else ()
        return " ".join((*words, *(arguments[name] for name in self.arguments)))


CONTEXT_COMMAND = ForwardCommand("--context", ("path", "status", "prompt_id"))
TIME_COMMAND = ForwardCommand("--time")
CLOCK_COMMAND = ForwardCommand("--clock")
FORWARD_COMMAND = ForwardCommand(
    "", ("tty_path", "ack_path", "prompt_id", "context_path")
)


class FrameDecoder:
    """Decode multiple FIFO frames and partial tails within a byte limit."""

    def __init__(self, max_frame_bytes=16 * 1024 * 1024):
        """Initialize the receive buffer and maximum frame size."""
        self.buffer = bytearray()
        self.max_frame_bytes = max_frame_bytes

    def feed(self, chunk: bytes):
        """Return complete category-payload pairs from a chunk and retain the remaining
        tail.

        Decode ISH2 payloads as strict base64 and reject invalid encodings or oversized
        frames.
        """
        self.buffer.extend(chunk)
        records = []
        consumed = 0
        while True:
            start = self.buffer.find(SOH, consumed)
            if start < 0:
                consumed = len(self.buffer)
                break
            end = self.buffer.find(EOT, start + 1)
            # SIGINT can stop a context writer halfway through a frame. A
            # fresh versioned header cannot occur inside a base64 body; resume
            # there instead of decoding two generations as one damaged frame.
            restart = self.buffer.rfind(
                SOH + VERSION + RS,
                start + 1,
                end if end >= 0 else len(self.buffer),
            )
            if restart >= 0:
                start = restart
            if end < 0:
                consumed = start
                if len(self.buffer) - start > self.max_frame_bytes:
                    self.buffer.clear()
                    raise ValueError("Shell context frame exceeds size limit")
                break
            if end - start > self.max_frame_bytes:
                self.buffer.clear()
                raise ValueError("Shell context frame exceeds size limit")
            payload = bytes(self.buffer[start + 1 : end])
            consumed = end + 1
            encoded = payload.startswith(VERSION + RS)
            if encoded:
                payload = payload[len(VERSION) + 1 :]
            for record in payload.split(RS):
                if US not in record:
                    continue
                category, body = record.split(US, 1)
                if encoded:
                    body = base64.b64decode(body, validate=True)
                records.append((category.decode("ascii"), body))
        del self.buffer[:consumed]
        return records
