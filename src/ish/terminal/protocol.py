"""Frame child control strings without interpreting terminal or shell state.

Adapters consume only complete, bounded records. Everything else goes to the
existing native parser in order. No child bytes are written to the outer TTY.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ESC = 0x1B
BEL = 0x07
ST = 0x9C
CANCEL = frozenset((0x18, 0x1A))
MAX_STRING_BYTES = 32 * 1024 * 1024
MAX_SEQUENCE_BYTES = 4096
STRING_KINDS = {
    ord("]"): "osc",
    ord("P"): "dcs",
    ord("_"): "apc",
    ord("^"): "pm",
    ord("X"): "sos",
}
C1_KINDS = {0x9D: "osc", 0x90: "dcs", 0x9F: "apc", 0x9E: "pm", 0x98: "sos", 0x9B: "csi"}


@dataclass(frozen=True)
class Control:
    """One exact wire record with its framing removed from the adapter body."""

    kind: str
    body: bytes
    raw: bytes


class ProtocolStream:
    """Preserve byte order across arbitrary PTY read fragmentation."""

    def __init__(
        self,
        write: Callable[[bytes], None],
        handle: Callable[[Control], bool],
        *,
        cancel: Callable[[], None] | None = None,
        max_string_bytes: int = MAX_STRING_BYTES,
    ):
        """Use the existing VT writer and narrowly scoped protocol adapters."""
        if max_string_bytes < 4:
            raise ValueError("Control string limit is too small")
        self.write, self.handle, self.cancel = write, handle, cancel
        self.limit = max_string_bytes
        self._state = "ground"
        self._kind = ""
        self._buffer = bytearray()
        self._prefix = 0
        self._drop = False
        self._escaped = False
        self._utf8_left = 0

    def _standalone(self, byte: int) -> bool:
        """Do not mistake a UTF-8 continuation byte for an eight-bit control."""
        if self._utf8_left and 0x80 <= byte <= 0xBF:
            self._utf8_left -= 1
            return False
        self._utf8_left = 0
        if 0xC2 <= byte <= 0xDF:
            self._utf8_left = 1
        elif 0xE0 <= byte <= 0xEF:
            self._utf8_left = 2
        elif 0xF0 <= byte <= 0xF4:
            self._utf8_left = 3
        return True

    def _start(self, byte: int, kind: str = ""):
        self._state = "escape" if byte == ESC else "csi" if kind == "csi" else "string"
        self._kind = kind
        self._buffer = bytearray((byte,))
        self._prefix = 1
        self._drop = self._escaped = False
        self._utf8_left = 0

    def _finish(self, suffix: int = 0):
        if not self._drop:
            raw = bytes(self._buffer)
            body = raw[self._prefix : len(raw) - suffix if suffix else None]
            if not self.handle(Control(self._kind, body, raw)):
                self.write(raw)
        self._state = "ground"
        self._buffer.clear()
        self._drop = self._escaped = False
        self._utf8_left = 0

    def _abort(self):
        self._state = "ground"
        self._buffer.clear()
        self._drop = self._escaped = False
        self._utf8_left = 0
        if self.cancel is not None:
            self.cancel()

    def feed(self, data: bytes):
        """Frame controls and forward ordinary text before the next adapter call."""
        if (
            self._state == "ground"
            and data.isascii()
            and not any(control in data for control in (b"\x1b", b"\x18", b"\x1a"))
        ):
            self._utf8_left = 0
            if data:
                self.write(data)
            return
        text = bytearray()

        def flush():
            if text:
                self.write(bytes(text))
                text.clear()

        for byte in data:
            standalone = self._standalone(byte)
            if self._state == "ground":
                if byte == ESC or (standalone and byte in C1_KINDS):
                    flush()
                    self._start(byte, C1_KINDS.get(byte, ""))
                elif byte in CANCEL:
                    flush()
                    self._abort()
                    self.write(bytes((byte,)))
                else:
                    text.append(byte)
                continue

            if byte in CANCEL:
                self._abort()
                self.write(bytes((byte,)))
                continue

            if self._state == "string":
                if self._escaped and byte != ord("\\"):
                    self._abort()
                    self._start(ESC)
                    # The current byte belongs to the replacement escape.
                else:
                    if not self._drop:
                        self._buffer.append(byte)
                    if self._escaped and byte == ord("\\"):
                        self._finish(2)
                    elif (standalone and byte == ST) or (
                        self._kind == "osc" and byte == BEL
                    ):
                        self._finish(1)
                    else:
                        self._escaped = byte == ESC
                        if len(self._buffer) > self.limit:
                            self._buffer.clear()
                            self._drop = True
                            if self.cancel is not None:
                                self.cancel()
                    continue

            if byte == ESC:
                self._abort()
                self._start(byte)
                continue
            self._buffer.append(byte)
            if self._state == "escape":
                if len(self._buffer) == 2 and byte in STRING_KINDS:
                    self._kind, self._state, self._prefix = (
                        STRING_KINDS[byte],
                        "string",
                        2,
                    )
                elif len(self._buffer) == 2 and byte == ord("["):
                    self._kind, self._state, self._prefix = "csi", "csi", 2
                elif 0x30 <= byte <= 0x7E:
                    self._kind = "esc"
                    self._finish()
            elif 0x40 <= byte <= 0x7E:
                self._finish()
            if len(self._buffer) > MAX_SEQUENCE_BYTES:
                self._buffer.clear()
                self._drop = True
        flush()

    def close(self):
        """Discard an unfinished record; never display its payload as user text."""
        self._abort()
