"""Bounded notification text, independent of the host's delivery mechanism.

OSC 9/777 originate in libghostty's callback. The OSC 99 supplement implements
chunked title/body and correlated capability queries only. Activation, close,
replacement, icons, buttons and application focus are not synthesized.
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from dataclasses import dataclass, field

from .identity import PROGRAM


@dataclass(frozen=True, slots=True)
class Notification:
    """Plain, bounded text; never an application-provided host escape code."""

    MAX_BYTES = 16 * 1024

    title: str
    body: str

    @classmethod
    def from_bytes(cls, title: bytes, body: bytes) -> Notification | None:
        """Remove terminal control characters before crossing the host boundary."""
        if len(title) + len(body) > cls.MAX_BYTES:
            return None
        try:
            values = []
            for data in (title, body):
                text = data.decode("utf-8")
                values.append(
                    "".join(
                        " " if char in "\r\n\t" else char
                        for char in text
                        if char in "\r\n\t"
                        or not (ord(char) < 32 or 127 <= ord(char) < 160)
                    )
                )
        except UnicodeError:
            return None
        if not any(values):
            return None
        return cls(*values)

    def osc9(self) -> str:
        """Serialize normalized text; the HostBroker still owns actual writing."""
        safe = self.from_bytes(self.title.encode("utf-8"), self.body.encode("utf-8"))
        if safe is None:
            return ""
        text = ": ".join(value for value in (safe.title, safe.body) if value)
        # OSC 9 also has numeric subcommands (progress, cwd, etc.) on some
        # hosts. A fixed non-numeric prefix keeps plain text in its namespace.
        return f"\x1b]9;{PROGRAM}: {text}\x1b\\"


@dataclass(slots=True)
class _Pending:
    """Only incomplete protocol payload, never a second command/history model."""

    text: dict = field(
        default_factory=lambda: {"title": bytearray(), "body": bytearray()}
    )
    carry: dict = field(default_factory=lambda: {"title": b"", "body": b""})
    pieces: int = 0
    failed: bool = False


class Notifications:
    """Consume owned notification input with explicit on/off permission.

    Pending records are bounded. Exhaustion disables this optional extension
    until protocol cancellation/reset, rather than publishing an orphan tail.
    No notification payload or request ID is written to diagnostic logs.
    """

    MAX_PENDING = 16
    MAX_PIECES = 64
    MAX_METADATA = 1024
    MAX_ID = 128
    MAX_PLAIN_CHUNK = 2048
    MAX_ENCODED_CHUNK = 4096
    HEADER = struct.Struct("<I")
    IDENTIFIER = re.compile(rb"[A-Za-z0-9_+.-]+\Z")
    KEY = re.compile(rb"[A-Za-z]\Z")
    VALUE = re.compile(rb"[A-Za-z0-9_\-/+.,(){}\[\]*&^%$#@!`~?]*\Z")
    CAPABILITIES = b"p=title,body:o=always"

    def __init__(self, publish, reply, *, enabled=False):
        """Accept normalized delivery and the existing child PTY reply owner."""
        self.enabled = enabled
        self._publish = publish
        self._reply = reply
        self._pending = {}
        self._blocked = False
        self._closed = False
        self.rejected = 0

    def native(self, payload: bytes) -> None:
        """Accept a copied native OSC 9/777 callback payload once."""
        if not self.enabled or self._closed:
            return
        if len(payload) < self.HEADER.size:
            self.rejected += 1
            return
        length = self.HEADER.unpack_from(payload)[0]
        if length > len(payload) - self.HEADER.size:
            self.rejected += 1
            return
        offset = self.HEADER.size + length
        self._emit(payload[self.HEADER.size : offset], payload[offset:])

    def _emit(self, title, body):
        value = Notification.from_bytes(title, body)
        if value is None:
            self.rejected += 1
        else:
            self._publish(value)

    def _metadata(self, data):
        if len(data) > self.MAX_METADATA:
            return None
        values = {}
        for entry in data.split(b":") if data else ():
            key, separator, value = entry.partition(b"=")
            if (
                not separator
                or not self.KEY.fullmatch(key)
                or not self.VALUE.fullmatch(value)
            ):
                return None
            if key in values:
                return None
            values[key] = value
        identifier = values.get(b"i", b"")
        if identifier and (
            len(identifier) > self.MAX_ID or not self.IDENTIFIER.fullmatch(identifier)
        ):
            return None
        return values

    def handle(self, control) -> bool:
        """Own OSC 99 even when disabled; never fall back to another handler."""
        if control.kind != "osc" or not control.body.startswith(b"99;"):
            return False
        if not self.enabled or self._closed:
            return True
        metadata, separator, payload = control.body[3:].partition(b";")
        values = self._metadata(metadata) if separator else None
        if values is None:
            self.rejected += 1
            self._pending.clear()
            self._blocked = True
            return True
        identifier = values.get(b"i", b"")
        kind = values.get(b"p", b"title")
        if kind == b"?":
            if identifier and not payload:
                self._reply(
                    b"\x1b]99;i="
                    + identifier
                    + b":p=?;"
                    + self.CAPABILITIES
                    + b"\x1b\\"
                )
            return True
        if self._blocked or kind not in (b"title", b"body"):
            return True
        if identifier not in self._pending:
            if len(self._pending) >= self.MAX_PENDING:
                self._pending.clear()
                self._blocked = True
                self.rejected += 1
                return True
            self._pending[identifier] = _Pending()
        pending = self._pending[identifier]
        pending.pieces += 1
        done, encoded = values.get(b"d", b"1"), values.get(b"e", b"0")
        occasion = values.get(b"o", b"always")
        if (
            done not in (b"0", b"1")
            or encoded not in (b"0", b"1")
            or occasion != b"always"
            or pending.pieces > self.MAX_PIECES
        ):
            pending.failed = True
        part = kind.decode("ascii")
        if not pending.failed:
            try:
                limit = (
                    self.MAX_ENCODED_CHUNK if encoded == b"1" else self.MAX_PLAIN_CHUNK
                )
                if len(payload) > limit:
                    raise ValueError
                if encoded == b"1":
                    data = pending.carry[part] + payload
                    size = len(data) if data.endswith(b"=") else len(data) // 4 * 4
                    pending.carry[part] = data[size:]
                    payload = base64.b64decode(data[:size], validate=True)
                elif pending.carry[part]:
                    raise ValueError
                pending.text[part].extend(payload)
                if sum(map(len, pending.text.values())) > Notification.MAX_BYTES:
                    raise ValueError
            except (ValueError, binascii.Error):
                pending.failed = True
        if done != b"0":
            del self._pending[identifier]
            if not pending.failed:
                try:
                    for part, carry in pending.carry.items():
                        if carry:
                            pending.text[part].extend(
                                base64.b64decode(
                                    carry + b"=" * (-len(carry) % 4), validate=True
                                )
                            )
                except (ValueError, binascii.Error):
                    pending.failed = True
            if pending.failed:
                self.rejected += 1
            else:
                self._emit(bytes(pending.text["title"]), bytes(pending.text["body"]))
        return True

    def cancel(self):
        """Forget incomplete text at CAN/SUB/RIS; stale tails cannot concatenate."""
        self._pending.clear()
        self._blocked = False

    def close(self):
        """Release incomplete transfers and permanently stop optional effects."""
        self.cancel()
        self._closed = True
