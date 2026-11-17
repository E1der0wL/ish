"""Frame child control strings without interpreting terminal or shell state.

Adapters consume only complete, bounded records. No child bytes are written to
the outer TTY. Screen operations and command semantics remain native-owned.

The framing boundary follows pinned libghostty 1.0.1: ground is UTF-8-only,
C1 transitions exist inside headers, and high bytes are payload inside OSC or
DCS passthrough/ignore. DCS's small header state exists only to locate those
boundaries; parameters and commands are never interpreted here.

Independent ESC/CSI C0 effects execute immediately unless clipboard capture
owns the bytes. Their positions stay in the original record for capture, but
are removed from native replay. Oversize records are dropped through their
boundary and counted; already executed C0 effects are not rolled back.
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
C1_INTRODUCERS = {
    0x9D: ord("]"),
    0x90: ord("P"),
    0x9F: ord("_"),
    0x9E: ord("^"),
    0x98: ord("X"),
    0x9B: ord("["),
}
C1_EXECUTE = frozenset((*range(0x80, 0x90), *range(0x91, 0x98), 0x99, 0x9A))
DCS_HEADERS = frozenset(("entry", "param", "intermediate"))
APC_KINDS = frozenset(("apc", "pm", "sos"))
EXECUTE = frozenset((*range(0x18), 0x19, *range(0x1C, 0x20)))


@dataclass(frozen=True)
class Control:
    """Exact original wire, plus body without framing or executed header C0."""

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
        capture: Callable[[Control], bool] | None = None,
        opaque: Callable[[], bool] | None = None,
        max_string_bytes: int = MAX_STRING_BYTES,
    ):
        """Use the existing VT writer and narrowly scoped protocol adapters."""
        if max_string_bytes < 4:
            raise ValueError("Control string limit is too small")
        self.write, self.handle, self.cancel, self.capture = (
            write,
            handle,
            cancel,
            capture,
        )
        self.opaque = opaque
        self.limit = max_string_bytes
        self._state = "ground"
        self._kind = ""
        self._buffer = bytearray()
        self._prefix = 0
        self._drop = False
        self._escaped = False
        self._utf8_left = 0
        self._dcs = ""
        self.dropped = 0
        self._executed = set()

    def _track_utf8(self, byte: int):
        """Track an unfinished codepoint without promoting raw C1 to controls."""
        if self._utf8_left and 0x80 <= byte <= 0xBF:
            self._utf8_left -= 1
            return
        self._utf8_left = 0
        if 0xC2 <= byte <= 0xDF:
            self._utf8_left = 1
        elif 0xE0 <= byte <= 0xEF:
            self._utf8_left = 2
        elif 0xF0 <= byte <= 0xF4:
            self._utf8_left = 3

    @property
    def complete(self):
        """Whether input ended outside a control or a split UTF-8 codepoint."""
        return self._state == "ground" and self._utf8_left == 0

    def _start(self, byte: int, kind: str = ""):
        self._state = "escape" if byte == ESC else "csi" if kind == "csi" else "string"
        self._kind = kind
        self._buffer = bytearray((byte,))
        self._prefix = 1
        self._drop = self._escaped = False
        self._utf8_left = 0
        self._dcs = "entry" if kind == "dcs" else ""
        self._executed.clear()

    def _finish(self, suffix: int = 0):
        control = None
        execute = b""
        native = b""
        if not self._drop:
            raw = bytes(self._buffer)
            end = len(raw) - suffix if suffix else len(raw)
            boundary = end if self._kind in ("csi", "esc") else self._prefix
            positions = frozenset(
                index for index in range(boundary) if raw[index] in EXECUTE
            )
            execute = bytes(raw[index] for index in sorted(positions - self._executed))
            native = raw
            body = raw[self._prefix : end]
            if positions:
                native = self._without(raw, positions)
                if boundary > self._prefix:
                    body = bytes(
                        raw[index]
                        for index in range(self._prefix, end)
                        if index not in positions
                    )
            control = Control(self._kind, body, raw)
        self._state = "ground"
        self._buffer.clear()
        self._drop = self._escaped = False
        self._utf8_left = 0
        self._executed.clear()
        # Release framing before invoking a writer: an exception after a partial
        # native write must never leave the same record available for replay.
        if control is not None:
            if self.capture is not None and self.capture(control):
                return
            if execute:
                self.write(execute)
            if not self.handle(control):
                self.write(self._native_bytes(native))

    def _abort(self, ending=b"", *, preserve=False):
        raw = (
            self._native_bytes(self._without(bytes(self._buffer), self._executed))
            if preserve and not self._drop
            else b""
        )
        self._state = "ground"
        self._buffer.clear()
        self._drop = self._escaped = False
        self._utf8_left = 0
        self._executed.clear()
        if self.cancel is not None:
            self.cancel()
        if preserve and (raw or ending):
            self.write(raw + ending)

    def _restart(self, byte):
        """Preserve the native exit action, then give adapters a ground boundary.

        The replacement introducer can be consumed by an adapter. Finish the
        old native state with its actual exit byte, and cancel only the newly
        entered, empty state. Replaying an unhandled replacement is then safe.
        C1 introducers are normalized because ground itself is UTF-8-only.
        """
        self._abort(bytes((byte, 0x18)), preserve=True)
        self._start(byte, C1_KINDS.get(byte, ""))

    @staticmethod
    def _without(raw, positions):
        pieces, offset = [], 0
        for index in sorted(positions):
            pieces.append(raw[offset:index])
            offset = index + 1
        pieces.append(raw[offset:])
        return b"".join(pieces)

    @staticmethod
    def _native_bytes(raw):
        """Re-enter a C1-origin record after the native abort has reached ground."""
        if raw and raw[0] in C1_INTRODUCERS:
            return bytes((ESC, C1_INTRODUCERS[raw[0]])) + raw[1:]
        return raw

    def _store(self, byte):
        if self._drop:
            return
        self._buffer.append(byte)
        limit = self.limit if self._state == "string" else MAX_SEQUENCE_BYTES
        if len(self._buffer) > limit:
            self._buffer.clear()
            self._executed.clear()
            self._drop = True
            self.dropped += 1
            if self.cancel is not None:
                self.cancel()

    def _dcs_byte(self, byte):
        """Track only DCS framing: high bytes are data after its header ends."""
        if self._dcs not in DCS_HEADERS:
            return
        if 0x40 <= byte <= 0x7E:
            self._dcs = "payload"
        elif self._dcs == "intermediate" and 0x30 <= byte <= 0x3F:
            self._dcs = "ignore"
        elif byte == 0x3A or (self._dcs == "param" and 0x3C <= byte <= 0x3F):
            self._dcs = "ignore"
        elif 0x20 <= byte <= 0x2F:
            self._dcs = "intermediate"
        elif 0x30 <= byte <= 0x3F:
            self._dcs = "param"

    def feed(self, data: bytes):
        """Frame controls and forward ordinary text before the next adapter call."""
        if len(data) == 0:
            return
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
            if self._state == "ground":
                interrupted_utf8 = bool(self._utf8_left)
                self._track_utf8(byte)
                # Pinned libghostty decodes ground as UTF-8. A raw C1 byte is
                # malformed text there, not a new adapter-owned control.
                if byte == ESC:
                    flush()
                    if interrupted_utf8 and (self.opaque is None or not self.opaque()):
                        # A consumed adapter must not join text from opposite
                        # sides of its introducer into one native codepoint.
                        self.write(b"\x1b\x18")
                    self._start(byte)
                elif byte in CANCEL:
                    flush()
                    self._abort()
                    self.write(bytes((byte,)))
                else:
                    text.append(byte)
                continue

            if byte in CANCEL:
                self._abort(bytes((byte,)), preserve=True)
                continue

            if self._state == "string" and self._escaped and byte != ord("\\"):
                if not self._drop:
                    self._buffer.pop()
                self._restart(ESC)

            if (
                self._state in ("escape", "escape_intermediate", "csi")
                and byte in EXECUTE
            ):
                if self.opaque is None or not self.opaque():
                    self.write(bytes((byte,)))
                    if not self._drop:
                        self._executed.add(len(self._buffer))
                self._store(byte)
                continue

            c1_active = (
                self._state != "string"
                or self._kind in APC_KINDS
                or (self._kind == "dcs" and self._dcs in DCS_HEADERS)
            )
            if c1_active:
                if byte in C1_KINDS:
                    if self._kind in APC_KINDS and byte in (0x98, 0x9E, 0x9F):
                        self._store(byte)
                    else:
                        self._restart(byte)
                    continue
                if byte == ST or byte in C1_EXECUTE:
                    self._abort(bytes((byte,)), preserve=True)
                    continue

            if self._state == "string":
                self._store(byte)
                if self._escaped and byte == ord("\\"):
                    self._finish(2)
                elif self._kind == "osc" and byte == BEL:
                    self._finish(1)
                else:
                    self._escaped = byte == ESC
                    if self._kind == "dcs":
                        self._dcs_byte(byte)
                continue

            if byte == ESC:
                self._restart(byte)
                continue
            self._store(byte)
            if self._state == "escape":
                if byte in STRING_KINDS:
                    self._kind, self._state, self._prefix = (
                        STRING_KINDS[byte],
                        "string",
                        len(self._buffer),
                    )
                    self._dcs = "entry" if self._kind == "dcs" else ""
                elif byte == ord("["):
                    self._kind, self._state, self._prefix = (
                        "csi",
                        "csi",
                        len(self._buffer),
                    )
                elif 0x20 <= byte <= 0x2F:
                    self._state = "escape_intermediate"
                elif 0x30 <= byte <= 0x7E:
                    self._kind = "esc"
                    self._finish()
            elif self._state == "escape_intermediate":
                if 0x30 <= byte <= 0x7E:
                    self._kind = "esc"
                    self._finish()
            elif 0x40 <= byte <= 0x7E:
                self._finish()
        flush()

    def close(self):
        """Discard an unfinished record; never display its payload as user text."""
        self._abort()
