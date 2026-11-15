"""Recognize private prompt records while relaying every other byte unchanged.

Unknown control strings stream incrementally; image sizes have no parser limit.
The bounded buffer holds only a possible private record or short CSI prefix.
"""

from __future__ import annotations

from collections.abc import Callable

ESC = 0x1B
BEL = 0x07
CANCEL = (0x18, 0x1A)
STRING_STARTS = {ord("]"), ord("P"), ord("_"), ord("^"), ord("X")}
MAX_RECORD = 256
MAX_SEQUENCE = 128


class Stream:
    """Keep host controls intact and order UI events with shell output."""

    def __init__(self, nonce: str, write, event, unsafe):
        """Accept session-local events and a single ordered outer writer."""
        self.prefix = b"\x1b]777;ish;" + nonce.encode("ascii") + b";"
        self.write: Callable[[bytes], None] = write
        self.event = event
        self.unsafe = unsafe
        self.state = "ground"
        self.pending = bytearray()
        self.kind = 0
        self.escaped = False
        self.last_serial = 0
        self.utf8_left = 0

    @property
    def boundary(self):
        """Return whether UI bytes may be inserted without splitting a record."""
        return self.state == "ground" and self.utf8_left == 0

    def _private(self):
        raw = bytes(self.pending)
        suffix = 2 if raw.endswith(b"\x1b\\") else 1
        body = raw[len(self.prefix) : -suffix]
        try:
            number, kind = body.split(b";")
            serial = int(number)
        except ValueError:
            self.write(raw)
            return
        if kind not in (b"P", b"H", b"B") or not number.isdigit():
            self.write(raw)
        elif serial > self.last_serial:
            self.last_serial = serial
            self.event(kind.decode("ascii"), serial)

    def _control(self, raw):
        if raw.startswith(b"\x1b["):
            body, final = raw[2:-1], raw[-1:]
            safe = final in (
                b"A",
                b"B",
                b"C",
                b"D",
                b"E",
                b"F",
                b"G",
                b"K",
                b"m",
                b"P",
                b"@",
                b"X",
            )
            safe |= final in (b"h", b"l") and body in (b"?25", b"?2004")
            if not safe:
                self.unsafe()
        else:
            self.unsafe()
        self.write(raw)

    def feed(self, data: bytes):
        """Forward fragments, including oversized and unfinished host strings."""
        output = bytearray()

        def flush():
            if output:
                self.write(bytes(output))
                output.clear()

        for byte in data:
            continuation = bool(self.utf8_left and 0x80 <= byte <= 0xBF)
            if continuation:
                self.utf8_left -= 1
            else:
                self.utf8_left = 0
                if 0xC2 <= byte <= 0xDF:
                    self.utf8_left = 1
                elif 0xE0 <= byte <= 0xEF:
                    self.utf8_left = 2
                elif 0xF0 <= byte <= 0xF4:
                    self.utf8_left = 3
            if self.state == "ground":
                if continuation:
                    output.append(byte)
                    continue
                if byte == ESC:
                    flush()
                    self.pending = bytearray((byte,))
                    self.state = "escape"
                elif byte in (0x90, 0x98, 0x9D, 0x9E, 0x9F, 0x9B):
                    flush()
                    self.unsafe()
                    self.kind = ord("]") if byte == 0x9D else 0
                    self.state = "raw_csi" if byte == 0x9B else "string"
                    self.escaped = False
                    output.append(byte)
                else:
                    output.append(byte)
                continue

            if self.state == "string":
                output.append(byte)
                if (
                    byte in CANCEL
                    or (self.escaped and byte == ord("\\"))
                    or (byte == 0x9C and not continuation)
                    or (self.kind == ord("]") and byte == BEL)
                ):
                    self.state = "ground"
                    self.escaped = False
                else:
                    self.escaped = byte == ESC
                continue

            if self.state == "raw_csi":
                output.append(byte)
                if byte in CANCEL or 0x40 <= byte <= 0x7E:
                    self.state = "ground"
                continue

            if self.state == "raw_escape":
                output.append(byte)
                if byte in CANCEL or 0x30 <= byte <= 0x7E:
                    self.state = "ground"
                continue

            self.pending.append(byte)
            if self.state == "escape":
                if byte == ord("]") and len(self.pending) == 2:
                    self.state = "candidate"
                elif byte in STRING_STARTS and len(self.pending) == 2:
                    self.kind = byte
                    self.state = "string"
                    self.escaped = False
                    self.unsafe()
                    self.write(bytes(self.pending))
                    self.pending.clear()
                elif byte == ord("[") and len(self.pending) == 2:
                    self.state = "csi"
                elif 0x30 <= byte <= 0x7E or byte in CANCEL:
                    self.state = "ground"
                    self._control(bytes(self.pending))
                    self.pending.clear()
                elif len(self.pending) > MAX_SEQUENCE:
                    self.unsafe()
                    self.write(bytes(self.pending))
                    self.pending.clear()
                    self.state = "raw_escape"
            elif self.state == "csi":
                if 0x40 <= byte <= 0x7E or byte in CANCEL:
                    self.state = "ground"
                    self._control(bytes(self.pending))
                    self.pending.clear()
                elif len(self.pending) > MAX_SEQUENCE:
                    self.unsafe()
                    self.write(bytes(self.pending))
                    self.pending.clear()
                    self.state = "raw_csi"
            elif self.state == "candidate":
                raw = bytes(self.pending)
                complete = byte == BEL or raw.endswith(b"\x1b\\")
                matches = self.prefix.startswith(raw) or raw.startswith(self.prefix)
                if complete and raw.startswith(self.prefix):
                    self.state = "ground"
                    self._private()
                    self.pending.clear()
                elif not matches or len(raw) > MAX_RECORD or byte in CANCEL:
                    self.unsafe()
                    self.write(raw)
                    self.pending.clear()
                    self.state = "ground" if complete or byte in CANCEL else "string"
                    self.kind = ord("]")
                    self.escaped = byte == ESC
        flush()

    def finish(self):
        """Preserve an unfinished prefix at EOF without adding terminal bytes."""
        if self.pending:
            self.write(bytes(self.pending))
            self.pending.clear()


class Keys:
    """Intercept only an explicitly enabled shortcut, outside bracketed paste."""

    TOKENS = (b"\x1bOQ", b"\x1b[12~", b"\x1b[200~", b"\x1b[201~")

    def __init__(self, write, popup):
        """Keep shell bytes intact; the caller owns escape ambiguity timing."""
        self.write, self.popup = write, popup
        self.pending = bytearray()
        self.paste = False

    def feed(self, data):
        """Recognize F2 without stealing bytes inside a pasted command."""
        for index, byte in enumerate(data):
            self.pending.append(byte)
            while self.pending:
                raw = bytes(self.pending)
                if raw in self.TOKENS:
                    self.pending.clear()
                    if raw == self.TOKENS[2]:
                        self.paste = True
                        self.write(raw)
                    elif raw == self.TOKENS[3]:
                        self.paste = False
                        self.write(raw)
                    elif self.paste or not self.popup(raw):
                        self.write(raw)
                    else:
                        # The rest of this input read belongs to the popup even
                        # if Enter closes it synchronously while processing it.
                        self.write(data[index + 1 :])
                        return
                    break
                if any(token.startswith(raw) for token in self.TOKENS):
                    break
                self.write(bytes((self.pending.pop(0),)))

    def flush(self):
        """Resolve a lone escape as native input, without asserting shell readiness."""
        if self.pending:
            self.write(bytes(self.pending))
            self.pending.clear()
