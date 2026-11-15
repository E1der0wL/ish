"""Bounded clipboard transactions with normalized wire output and correlated replies.

The child stream owns requests; the existing host input reader owns responses.
OSC 52 and iTerm writes have no success reply. Kitty DONE is relayed only after
the outer terminal reports completion, never merely after queuing output.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

OSC = "\x1b]"
ST = "\x1b\\"
MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_METADATA = 4096
MAX_MIMES = 256
CHUNK_BYTES = 4096
WRITE_CHUNK_BYTES = 4095  # Non-final MIME chunks never contain base64 padding.
REQUEST_TIMEOUT = 30.0
IDENTITY = re.compile(r"[^a-zA-Z0-9_+.-]")
BASE64 = re.compile(rb"[A-Za-z0-9+/]*={0,2}\Z")
MIME = re.compile(r"[!-~]{1,255}\Z")
WRITE_ERRORS = frozenset(("EIO", "EINVAL", "ENOSYS", "EPERM", "EBUSY", "EFBIG"))
READ_ERRORS = frozenset(("ENOSYS", "EPERM", "EBUSY"))
TEXT_MIME = "text/plain"


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(data: bytes, *, limit=MAX_TEXT) -> bytes:
    if len(data) > (limit + 2) // 3 * 4:
        raise ValueError("Clipboard payload exceeds limit")
    decoded = base64.b64decode(data, validate=True)
    if len(decoded) > limit:
        raise ValueError("Clipboard payload exceeds limit")
    return decoded


def _text(data: str, *, limit=MAX_METADATA) -> str:
    value = _decode(data.encode("ascii"), limit=limit).decode("utf-8")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Control character in clipboard metadata")
    return value


def _mime(value: str) -> str:
    if not MIME.fullmatch(value) or ":" in value or ";" in value:
        raise ValueError("Invalid clipboard MIME type")
    return value


def _packet(metadata: dict[str, str], payload: bytes | None = None) -> str:
    body = ":".join(f"{key}={value}" for key, value in metadata.items())
    return (
        OSC
        + "5522;"
        + body
        + (";" + _encode(payload) if payload is not None else "")
        + ST
    )


def _parse(body: bytes) -> tuple[dict[str, str], bytes]:
    metadata, _, payload = body.partition(b";")
    if len(metadata) > MAX_METADATA:
        raise ValueError("Clipboard metadata exceeds limit")
    fields = {}
    for item in metadata.decode("ascii").split(":"):
        key, equal, value = item.partition("=")
        if not equal or not key or key in fields:
            raise ValueError("Invalid clipboard metadata")
        fields[key] = value
    return fields, payload


@dataclass
class Transfer:
    """One active host transaction, distinct from the child's optional identity."""

    operation: str
    host_id: str
    child_id: str = ""
    protocol: str = "5522"
    location: str = "clipboard"
    selection: str = "c"
    requested: tuple[str, ...] = ()
    total: int = 0
    mime: str | None = None
    seen: set[str] = field(default_factory=set)
    carry: bytes = b""
    outgoing_tail: bytes = b""
    text: bytearray = field(default_factory=bytearray)
    started: bool = False
    committed: bool = False
    timer: object = None
    consume: Callable[[str], None] | None = None


class ClipboardBroker:
    """Apply explicit policy and route one clipboard transaction at a time."""

    def __init__(self, *, reply, emit, profile, allow_read=False, allow_write=True):
        """Connect separate child-reply and renderer-output sinks under explicit policy."""
        self.reply, self.emit, self.profile = reply, emit, profile
        self.allow_read, self.allow_write = allow_read, allow_write
        self.pending: Transfer | None = None
        self.idle = asyncio.Event()
        self.idle.set()
        self._prefix = "ish." + secrets.token_hex(12)
        self._serial = 0
        self._closed = False
        self._capture: bytearray | None = None
        self._capture_valid = False
        self._discard_write = False
        self._queued_copy = None

    def _new(self, operation, **values):
        self._serial += 1
        transfer = Transfer(operation, f"{self._prefix}.{self._serial}", **values)
        self.pending = transfer
        self.idle.clear()
        try:
            transfer.timer = asyncio.get_running_loop().call_later(
                REQUEST_TIMEOUT, self.expire, transfer.host_id
            )
        except RuntimeError:
            pass
        return transfer

    def _finish(self):
        transfer, self.pending = self.pending, None
        self.idle.set()
        if transfer is not None and transfer.timer is not None:
            transfer.timer.cancel()
        queued, self._queued_copy = self._queued_copy, None
        if queued is not None:
            self.write_text(*queued, explicit=True)
        return transfer

    def _status(self, operation, status, *, child_id="", location="clipboard"):
        metadata = {"type": operation, "status": status}
        if location == "primary":
            metadata["loc"] = "primary"
        if child_id:
            metadata["id"] = child_id
        self.reply(_packet(metadata).encode("ascii"))

    def _fail(self, status):
        transfer = self._finish()
        if transfer is None:
            return
        if transfer.operation == "write":
            self._discard_write = True
        if transfer.protocol == "user":
            return
        if transfer.protocol == "52":
            self.reply(f"{OSC}52;{transfer.selection};{ST}".encode("ascii"))
        else:
            if transfer.operation == "read" and status not in READ_ERRORS:
                status = "EBUSY"
            self._status(
                transfer.operation,
                status,
                child_id=transfer.child_id,
                location=transfer.location,
            )

    def expire(self, identity):
        """Fail only the matching clipboard request; deadlines never imply readiness."""
        if self.pending is not None and self.pending.host_id == identity:
            self._fail("EBUSY")

    def cancel(self):
        """Discard a cancelled transfer without exposing an unfinished capture."""
        self._fail("EBUSY")
        if self._capture is not None:
            self._capture.clear()
        self._capture_valid = False
        self._discard_write = True

    def reset(self):
        """Release capture ownership only at an explicit terminal reset boundary."""
        self.cancel()
        self._capture = None

    def close(self):
        """Release timers and consume future late host replies without replying."""
        self._closed = True
        self._finish()
        self._capture = None

    @property
    def capturing(self):
        """Whether iTerm's bounded capture currently owns subsequent stream bytes."""
        return self._capture is not None

    def capture(self, data: bytes) -> bool:
        """Consume iTerm capture data; the framer retains EndCopy as a control."""
        if self._capture is None:
            return False
        if self._capture_valid:
            if len(self._capture) + len(data) > MAX_TEXT:
                self._capture.clear()
                self._capture_valid = False
            else:
                self._capture.extend(data)
        return True

    def copy_selection(self, data: bytes) -> bool:
        """Keep an explicit user's copy separate from child clipboard permissions."""
        return self.write_text(data, "cp", explicit=True)

    def read_primary(self, consume: Callable[[str], None]) -> str | None:
        """Read system PRIMARY for one user action, independently of child policy.

        The application cancels this action when its input consumer changes. A
        host without correlated reads has no fallback to a different clipboard.
        Busy child transactions are preserved rather than queued behind new input.
        """
        self.cancel_paste()
        if (
            self._closed
            or not self.profile().clipboard_protocol
            or self.pending is not None
        ):
            return None
        transfer = self._new(
            "read",
            protocol="user",
            location="primary",
            requested=(TEXT_MIME,),
            consume=consume,
        )
        self.emit(_packet(self._host_metadata(transfer), TEXT_MIME.encode()))
        return transfer.host_id

    def cancel_paste(self) -> bool:
        """Forget only the pending user read; stale wire replies remain harmless."""
        if self.pending is None or self.pending.protocol != "user":
            return False
        self._finish()
        return True

    def write_text(self, data: bytes, selection="c", *, explicit=False) -> bool:
        """Queue normalized OSC 52 for explicit selection copy or child text writes."""
        if (
            self._closed
            or (not explicit and not self.allow_write)
            or len(data) > MAX_TEXT
            or any(char not in "cpqs01234567" for char in selection)
        ):
            return False
        # An independent text write must not race a host Kitty transaction.
        if self.pending is not None:
            if explicit:
                self._queued_copy = (bytes(data), selection)
                return True
            return False
        self.emit(f"{OSC}52;{selection};{_encode(data)}{ST}")
        return True

    def child_osc(self, body: bytes) -> bool:
        """Consume supported complete OSC bodies, without forwarding original bytes."""
        if self.capturing:
            if body == b"1337;EndCopy":
                data = bytes(self._capture)
                valid = self._capture_valid
                self._capture = None
                self._capture_valid = False
                if valid:
                    self.write_text(data)
                return True
            return False  # Caller captures this control's original bytes.
        if body.startswith(b"1337;CopyToClipboard="):
            self._capture = bytearray()
            self._capture_valid = body == b"1337;CopyToClipboard="
            return True
        if body.startswith(b"1337;Copy=:"):
            try:
                self.write_text(_decode(body[len(b"1337;Copy=:") :]))
            except (ValueError, binascii.Error):
                pass
            return True
        if body.startswith(b"52;"):
            self._osc52(body[3:])
            return True
        if not body.startswith(b"5522;"):
            return False
        if self._closed:
            return True
        try:
            metadata, payload = _parse(body[5:])
            operation = metadata.get("type")
            if operation in ("write", "read"):
                self._begin(operation, metadata, payload)
            elif operation in ("wdata", "walias"):
                self._write(operation, metadata, payload)
        except (ValueError, UnicodeError, binascii.Error):
            if self.pending is not None and self.pending.operation == "write":
                self._fail("EINVAL")
        return True

    def child_csi(self, body: bytes) -> bool:
        """Keep unimplemented unsolicited paste/OTP mode permanently reset.

        Explicit OSC 5522 transactions are supported independently. Advertising
        paste-event mode would also promise focus-aware MIME/OTP delivery, which
        this broker does not yet provide.
        """
        if body == b"?5522$p":
            self.reply(b"\x1b[?5522;4$y")
            return True
        return body in (b"?5522h", b"?5522l")

    def _osc52(self, body):
        selector, separator, payload = body.partition(b";")
        if not separator or len(selector) > 16:
            return
        try:
            selection = selector.decode("ascii")
        except UnicodeError:
            return
        if any(char not in "cpqs01234567" for char in selection):
            return
        if payload != b"?":
            if len(payload) > (MAX_TEXT + 2) // 3 * 4:
                return
            try:
                data = _decode(payload)
            except (ValueError, binascii.Error):
                # OSC 52 explicitly defines a non-base64 payload as clear.
                data = b""
            self.write_text(data, selection)
            return
        # Correlate reads using Kitty IDs. Never issue an identity-free host read.
        if (
            not self.allow_read
            or not self.profile().clipboard_protocol
            or self.pending is not None
            or selection not in ("", "c", "p", "s")
        ):
            self.reply(f"{OSC}52;{selection};{ST}".encode("ascii"))
            return
        location = "primary" if selection == "p" else "clipboard"
        transfer = self._new(
            "read",
            protocol="52",
            selection=selection,
            location=location,
            requested=(TEXT_MIME,),
        )
        self.emit(_packet(self._host_metadata(transfer), TEXT_MIME.encode()))

    def _host_metadata(self, transfer):
        metadata = {"type": transfer.operation, "id": transfer.host_id}
        if transfer.location == "primary":
            metadata["loc"] = "primary"
        return metadata

    def _begin(self, operation, metadata, payload):
        child_id = IDENTITY.sub("", metadata.get("id", ""))[:512]
        location = metadata.get("loc", "clipboard")
        if location not in ("clipboard", "primary"):
            self._status(operation, "ENOSYS", child_id=child_id)
            return
        # Validate before applying policy: malformed reads have no error response.
        requested = ()
        if operation == "read":
            requested = tuple(
                _decode(payload, limit=MAX_METADATA).decode("utf-8").split()
            )
            if not requested or len(requested) > MAX_MIMES:
                raise ValueError("Invalid MIME request")
            for mime in requested:
                _mime(mime)
        elif payload:
            raise ValueError("Unexpected write payload")
        extra = {}
        for key in ("name", "pw"):
            if key in metadata:
                extra[key] = _encode(_text(metadata[key]).encode("utf-8"))
        allowed = self.allow_write if operation == "write" else self.allow_read
        # A MIME listing reveals no clipboard contents and is permitted by spec.
        if operation == "read" and requested == (".",):
            allowed = self.allow_write or self.allow_read
        error = (
            "EPERM"
            if not allowed
            else "ENOSYS"
            if not self.profile().clipboard_protocol
            else "EBUSY"
            if self.pending is not None
            else None
        )
        if error:
            self._status(operation, error, child_id=child_id, location=location)
            if operation == "write":
                self._discard_write = True
            return
        transfer = self._new(
            operation, child_id=child_id, location=location, requested=requested
        )
        self._discard_write = False
        outgoing = self._host_metadata(transfer)
        outgoing.update(extra)
        self.emit(
            _packet(
                outgoing, " ".join(requested).encode() if operation == "read" else None
            )
        )

    def _write(self, operation, metadata, payload):
        transfer = self.pending
        if (
            self._discard_write
            or transfer is None
            or transfer.operation != "write"
            or transfer.committed
        ):
            return
        if "id" in metadata:
            identity = IDENTITY.sub("", metadata["id"])[:512]
            if identity != transfer.child_id:
                return
        if operation == "wdata" and "mime" not in metadata:
            if payload or transfer.carry:
                raise ValueError("Incomplete clipboard data")
            self._flush_tail(transfer)
            transfer.committed = True
            self.emit(_packet({"type": "wdata", "id": transfer.host_id}))
            return
        mime = _mime(_text(metadata.get("mime", "")))
        if operation == "walias":
            aliases = _decode(payload, limit=MAX_METADATA).decode("utf-8").split()
            if not aliases or len(aliases) > MAX_MIMES:
                raise ValueError("Invalid MIME aliases")
            for alias in aliases:
                _mime(alias)
            self.emit(
                _packet(
                    {
                        "type": "walias",
                        "id": transfer.host_id,
                        "mime": _encode(mime.encode()),
                    },
                    " ".join(aliases).encode(),
                )
            )
            return
        if mime != transfer.mime:
            if (
                transfer.carry
                or mime in transfer.seen
                or len(transfer.seen) >= MAX_MIMES
            ):
                raise ValueError("Invalid MIME stream order")
            self._flush_tail(transfer)
            transfer.seen.add(mime)
            transfer.mime = mime
        combined = transfer.carry + payload
        if not BASE64.fullmatch(combined):
            raise ValueError("Invalid clipboard base64 alphabet")
        count = len(combined) // 4 * 4
        head, transfer.carry = combined[:count], combined[count:]
        if b"=" in head and transfer.carry:
            raise ValueError("Data follows terminal padding")
        decoded = _decode(head, limit=MAX_BYTES)
        transfer.total += len(decoded)
        if transfer.total > MAX_BYTES:
            self._fail("EFBIG")
            return
        outgoing = {
            "type": "wdata",
            "id": transfer.host_id,
            "mime": _encode(mime.encode()),
        }
        normalized = transfer.outgoing_tail + decoded
        count = len(normalized) // 3 * 3
        transfer.outgoing_tail = normalized[count:]
        for offset in range(0, count, WRITE_CHUNK_BYTES):
            self.emit(
                _packet(
                    outgoing,
                    normalized[offset : min(count, offset + WRITE_CHUNK_BYTES)],
                )
            )
        if not normalized and not transfer.carry:
            self.emit(_packet(outgoing, b""))

    def _flush_tail(self, transfer):
        if transfer.outgoing_tail:
            self.emit(
                _packet(
                    {
                        "type": "wdata",
                        "id": transfer.host_id,
                        "mime": _encode(transfer.mime.encode()),
                    },
                    transfer.outgoing_tail,
                )
            )
            transfer.outgoing_tail = b""

    def host_reply(self, sequence: str) -> bool:
        """Validate correlated host replies before encoding the child's protocol."""
        if not sequence.startswith((OSC + "52;", OSC + "5522;")):
            return False
        if sequence.startswith(OSC + "52;"):
            return True  # No identity-free host read is ever issued.
        transfer = self.pending
        if self._closed or transfer is None:
            return True
        ending = 2 if sequence.endswith(ST) else 1 if sequence.endswith("\x07") else 0
        if not ending:
            return True
        correlated = False
        completed = None
        try:
            metadata, payload = _parse(
                sequence[len(OSC + "5522;") : -ending].encode("ascii")
            )
            if (
                metadata.get("id") != transfer.host_id
                or metadata.get("type") != transfer.operation
            ):
                return True
            correlated = True
            status = metadata.get("status")
            if transfer.operation == "write":
                if status in WRITE_ERRORS:
                    self._fail(status)
                elif status == "DONE" and transfer.committed and not payload:
                    self._finish()
                    self._status(
                        "write",
                        "DONE",
                        child_id=transfer.child_id,
                        location=transfer.location,
                    )
                return True
            if status in READ_ERRORS and not transfer.started:
                self._fail(status)
                return True
            if status == "OK" and not transfer.started and not payload:
                transfer.started = True
                if transfer.protocol == "5522":
                    self._status(
                        "read",
                        "OK",
                        child_id=transfer.child_id,
                        location=transfer.location,
                    )
                return True
            if status == "DATA" and transfer.started:
                mime = _mime(_text(metadata.get("mime", "")))
                if mime not in transfer.requested:
                    raise ValueError("Unrequested MIME response")
                data = _decode(payload, limit=CHUNK_BYTES)
                transfer.total += len(data)
                if transfer.total > (
                    MAX_TEXT if transfer.protocol in ("52", "user") else MAX_BYTES
                ):
                    raise ValueError("Clipboard response exceeds limit")
                if transfer.protocol in ("52", "user"):
                    transfer.text.extend(data)
                else:
                    outgoing = {"type": "read", "status": "DATA"}
                    if transfer.location == "primary":
                        outgoing["loc"] = "primary"
                    if transfer.child_id:
                        outgoing["id"] = transfer.child_id
                    outgoing["mime"] = _encode(mime.encode())
                    self.reply(_packet(outgoing, data).encode("ascii"))
                return True
            if status == "DONE" and transfer.started and not payload:
                # Decode before completion so malformed UTF-8 never reaches a
                # paste callback or falls through into a child-protocol reply.
                text = (
                    transfer.text.decode("utf-8")
                    if transfer.protocol == "user"
                    else None
                )
                self._finish()
                if transfer.protocol == "user":
                    completed = (transfer.consume, text)
                elif transfer.protocol == "52":
                    self.reply(
                        f"{OSC}52;{transfer.selection};{_encode(transfer.text)}{ST}".encode(
                            "ascii"
                        )
                    )
                else:
                    self._status(
                        "read",
                        "DONE",
                        child_id=transfer.child_id,
                        location=transfer.location,
                    )
        except (ValueError, UnicodeError, binascii.Error):
            if correlated:
                self._fail("EIO")
        if completed is not None and completed[0] is not None:
            completed[0](completed[1])
        return True
