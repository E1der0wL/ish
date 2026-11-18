"""Bounded clipboard transactions with normalized wire output and correlated replies.

The child stream owns requests; the existing host input reader owns responses.
OSC 52 and iTerm writes have no success reply. Kitty DONE is relayed only after
the outer terminal reports completion, never merely after queuing output.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import inspect
import re
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ish.lang import Message, i18n

OSC = "\x1b]"
ST = "\x1b\\"
MAX_TEXT = 1024 * 1024
MAX_METADATA = 4096
MIME = re.compile(r"[!-~]{1,255}\Z")


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
    local: bool = False
    task: asyncio.Task | None = None


class ClipboardBroker:
    """Apply explicit policy and route one clipboard transaction at a time."""

    MAX_ID_CHARS = 512
    MAX_BYTES = 64 * 1024 * 1024
    MAX_MIMES = 256
    CHUNK_BYTES = 4096
    WRITE_CHUNK_BYTES = 4095  # Non-final MIME chunks never contain base64 padding.
    REQUEST_TIMEOUT = 30.0
    MAX_QUEUED_REQUESTS = 64
    MAX_QUEUED_BYTES = 4 * MAX_TEXT
    IDENTITY = re.compile(r"[^a-zA-Z0-9_+.-]")
    BASE64 = re.compile(rb"[A-Za-z0-9+/]*={0,2}\Z")
    WRITE_ERRORS = frozenset(("EIO", "EINVAL", "ENOSYS", "EPERM", "EBUSY", "EFBIG"))
    READ_ERRORS = frozenset(("ENOSYS", "EPERM", "EBUSY"))
    TEXT_MIME = "text/plain"
    PROVIDER_ERRORS = {
        errno.ENOSYS: "ENOSYS",
        errno.EPERM: "EPERM",
        errno.EACCES: "EPERM",
        errno.EFBIG: "EFBIG",
    }

    def __init__(
        self, *, reply, emit, profile, allow_read=False, allow_write=True, failed=None
    ):
        """Connect separate child-reply and renderer-output sinks under explicit policy."""
        self.reply, self.emit, self.profile = reply, emit, profile
        self.failed = failed
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
        self._factory = None
        self._provider_name = None
        self._provider = None
        self._sealed = False
        self._tasks = set()
        self._provider_lock = asyncio.Lock()
        self._deferred = deque()
        self._deferred_bytes = 0
        self.failures = 0

    def register_provider(self, name, *, factory):
        """Select one external text provider before any session input is accepted."""
        if self._sealed or self._closed:
            raise RuntimeError(i18n.get(Message.VT_CLIPBOARD_LOCKED))
        if self._factory is not None:
            raise ValueError(i18n.get(Message.VT_CLIPBOARD_DUPLICATE))
        if not isinstance(name, str) or not name or not callable(factory):
            raise TypeError(i18n.get(Message.VT_CLIPBOARD_PROVIDER))
        self._factory = factory
        self._provider_name = name

    @property
    def provider_name(self):
        """Report only the selected module identity, never clipboard contents."""
        return self._provider_name

    def seal(self):
        """Construct the selected provider once, without transferring clipboard data."""
        if self._sealed or self._closed:
            return
        self._sealed = True
        if self._factory is None:
            return
        provider = self._factory()
        valid = callable(getattr(provider, "close", None)) and all(
            inspect.iscoroutinefunction(getattr(provider, method, None))
            for method in ("read", "write", "aclose")
        )
        if not valid:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
            raise TypeError(i18n.get(Message.VT_CLIPBOARD_PROVIDER))
        self._provider = provider

    def _start_provider(self, transfer, text=None):
        transfer.local = True
        transfer.task = asyncio.create_task(self._run_provider(transfer, text))
        self._tasks.add(transfer.task)
        transfer.task.add_done_callback(self._provider_done)

    def _provider_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            if self.failed is not None:
                self.failed(error)
            else:
                task.get_loop().call_exception_handler(
                    {
                        "message": i18n.get(Message.VT_CLIPBOARD_DELIVERY),
                        "exception": error,
                    }
                )

    def _defer(self, operation, data, selection="c", explicit=False):
        if (
            len(self._deferred) >= self.MAX_QUEUED_REQUESTS
            or self._deferred_bytes + len(data) > self.MAX_QUEUED_BYTES
        ):
            self.failures += 1
            return False
        self._deferred.append((operation, bytes(data), selection, explicit))
        self._deferred_bytes += len(data)
        return True

    async def _run_provider(self, transfer, text):
        """Keep asynchronous completion correlated even after cancellation or reset."""
        try:
            async with self._provider_lock:
                if self._closed or self.pending is not transfer:
                    return
                if transfer.operation == "write":
                    if await self._provider.write(text) is not None:
                        raise ValueError
                    data = b""
                else:
                    value = await self._provider.read()
                    if not isinstance(value, str) or len(value) > MAX_TEXT:
                        raise ValueError
                    data = value.encode("utf-8")
                    if len(data) > MAX_TEXT:
                        raise ValueError
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failures += 1
            if not self._closed and self.pending is transfer:
                code = getattr(error, "errno", None)
                status = self.PROVIDER_ERRORS.get(code, "EIO")
                self._fail(status)
            return
        if self._closed or self.pending is not transfer:
            return
        self._finish()
        # Response delivery is an application transport operation. Its errors
        # must reach the application, not masquerade as a provider failure.
        if transfer.protocol == "copy":
            return
        if transfer.protocol == "52":
            self.reply(
                f"{OSC}52;{transfer.selection};{_encode(data)}{ST}".encode("ascii")
            )
            return
        if transfer.operation == "read":
            self._status("read", "OK", child_id=transfer.child_id)
            mime = self.TEXT_MIME
            fields = {"type": "read", "status": "DATA", "mime": _encode(mime.encode())}
            if transfer.child_id:
                fields["id"] = transfer.child_id
            for offset in range(0, max(1, len(data)), self.CHUNK_BYTES):
                self.reply(
                    _packet(fields, data[offset : offset + self.CHUNK_BYTES]).encode(
                        "ascii"
                    )
                )
        self._status(transfer.operation, "DONE", child_id=transfer.child_id)

    def _new(self, operation, **values):
        self._serial += 1
        transfer = Transfer(operation, f"{self._prefix}.{self._serial}", **values)
        self.pending = transfer
        self.idle.clear()
        try:
            transfer.timer = asyncio.get_running_loop().call_later(
                self.REQUEST_TIMEOUT, self.expire, transfer.host_id
            )
        except RuntimeError:
            pass
        return transfer

    def _finish(self):
        transfer, self.pending = self.pending, None
        if transfer is not None and transfer.timer is not None:
            transfer.timer.cancel()
        if transfer is not None and transfer.task is not None:
            if transfer.task is not asyncio.current_task():
                transfer.task.cancel()
        while self._deferred and self.pending is None and not self._closed:
            operation, data, selection, explicit = self._deferred.popleft()
            self._deferred_bytes -= len(data)
            if operation == "osc":
                self._osc52(data)
            else:
                self.write_text(data, selection, explicit=explicit)
        queued, self._queued_copy = self._queued_copy, None
        if queued is not None:
            self.write_text(*queued, explicit=True)
        if self.pending is None:
            self.idle.set()
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
        if transfer.protocol in ("user", "copy"):
            return
        if transfer.protocol == "52":
            self.reply(f"{OSC}52;{transfer.selection};{ST}".encode("ascii"))
        else:
            if transfer.operation == "read" and status not in self.READ_ERRORS:
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
        self._queued_copy = None
        self._deferred.clear()
        self._deferred_bytes = 0
        self.cancel()
        self._capture = None

    def close(self):
        """Release timers and consume future late host replies without replying."""
        if self._closed:
            return
        self._closed = True
        self._queued_copy = None
        self._deferred.clear()
        self._deferred_bytes = 0
        self._finish()
        self._capture = None
        for task in self._tasks:
            task.cancel()
        if self._provider is not None:
            try:
                self._provider.close()
            except Exception:
                self.failures += 1

    async def aclose(self):
        """Reap provider workers without letting a failed provider skip restoration."""
        self.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._provider is not None:
            try:
                await self._provider.aclose()
            except Exception:
                self.failures += 1

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
        self.seal()
        return self.write_text(
            data, "c" if self._provider is not None else "cp", explicit=True
        )

    def read_primary(self, consume: Callable[[str], None]) -> str | None:
        """Read system PRIMARY for one user action, independently of child policy.

        The application cancels this action when its input consumer changes. A
        host without correlated reads has no fallback to a different clipboard.
        Busy child transactions are preserved rather than queued behind new input.
        """
        self.cancel_paste()
        self.seal()
        if (
            self._closed
            or self._provider is not None
            or not self.profile().clipboard_protocol
            or self.pending is not None
        ):
            return None
        transfer = self._new(
            "read",
            protocol="user",
            location="primary",
            requested=(self.TEXT_MIME,),
            consume=consume,
        )
        self.emit(_packet(self._host_metadata(transfer), self.TEXT_MIME.encode()))
        return transfer.host_id

    def cancel_paste(self) -> bool:
        """Forget only the pending user read; stale wire replies remain harmless."""
        if self.pending is None or self.pending.protocol != "user":
            return False
        self._finish()
        return True

    def write_text(self, data: bytes, selection="c", *, explicit=False) -> bool:
        """Queue normalized OSC 52 for explicit selection copy or child text writes."""
        self.seal()
        if (
            self._closed
            or (not explicit and not self.allow_write)
            or len(data) > MAX_TEXT
            or any(char not in "cpqs01234567" for char in selection)
        ):
            return False
        if self._provider is not None:
            if selection not in ("", "c"):
                return False
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                return False
        # An independent text write must not race a host Kitty transaction.
        if self.pending is not None:
            if self._provider is not None and (
                explicit or self.pending.protocol == "copy"
            ):
                return self._defer("copy", data, selection, explicit)
            if explicit:
                self._queued_copy = (bytes(data), selection)
                return True
            return False
        if self._provider is not None:
            transfer = self._new("write", protocol="copy", committed=True, local=True)
            self._start_provider(transfer, text)
        else:
            self.emit(f"{OSC}52;{selection};{_encode(data)}{ST}")
        return True

    def child_osc(self, body: bytes) -> bool:
        """Consume supported complete OSC bodies, without forwarding original bytes."""
        self.seal()
        if self._closed:
            return body.startswith((b"52;", b"5522;", b"1337;Copy"))
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
        if self._provider is not None and self.pending is not None:
            allowed = self.allow_read if payload == b"?" else self.allow_write
            queued = allowed and selection in ("", "c") and self._defer("osc", body)
            if not queued and payload == b"?":
                self.reply(f"{OSC}52;{selection};{ST}".encode("ascii"))
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
            or (self._provider is None and not self.profile().clipboard_protocol)
            or self.pending is not None
            or selection not in ("", "c", "p", "s")
            or (self._provider is not None and selection not in ("", "c"))
        ):
            self.reply(f"{OSC}52;{selection};{ST}".encode("ascii"))
            return
        location = "primary" if selection == "p" else "clipboard"
        transfer = self._new(
            "read",
            protocol="52",
            selection=selection,
            location=location,
            requested=(self.TEXT_MIME,),
        )
        if self._provider is not None:
            self._start_provider(transfer)
        else:
            self.emit(_packet(self._host_metadata(transfer), self.TEXT_MIME.encode()))

    def _host_metadata(self, transfer):
        metadata = {"type": transfer.operation, "id": transfer.host_id}
        if transfer.location == "primary":
            metadata["loc"] = "primary"
        return metadata

    def _begin(self, operation, metadata, payload):
        child_id = self.IDENTITY.sub("", metadata.get("id", ""))[: self.MAX_ID_CHARS]
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
            if not requested or len(requested) > self.MAX_MIMES:
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
            if (
                (self._provider is None and not self.profile().clipboard_protocol)
                or (
                    self._provider is not None
                    and (
                        location != "clipboard"
                        or (operation == "read" and requested != (self.TEXT_MIME,))
                        or extra
                    )
                )
            )
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
        if self._provider is not None:
            transfer.local = True
            if operation == "read":
                self._start_provider(transfer)
            return
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
            identity = self.IDENTITY.sub("", metadata["id"])[: self.MAX_ID_CHARS]
            if identity != transfer.child_id:
                return
        if operation == "wdata" and "mime" not in metadata:
            if payload or transfer.carry:
                raise ValueError("Incomplete clipboard data")
            if transfer.local:
                if transfer.mime != self.TEXT_MIME:
                    self._fail("ENOSYS")
                    return
                text = transfer.text.decode("utf-8")
                transfer.committed = True
                self._start_provider(transfer, text)
                return
            self._flush_tail(transfer)
            transfer.committed = True
            self.emit(_packet({"type": "wdata", "id": transfer.host_id}))
            return
        mime = _mime(_text(metadata.get("mime", "")))
        if transfer.local and (operation != "wdata" or mime != self.TEXT_MIME):
            self._fail("ENOSYS")
            return
        if operation == "walias":
            aliases = _decode(payload, limit=MAX_METADATA).decode("utf-8").split()
            if not aliases or len(aliases) > self.MAX_MIMES:
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
                or len(transfer.seen) >= self.MAX_MIMES
            ):
                raise ValueError("Invalid MIME stream order")
            self._flush_tail(transfer)
            transfer.seen.add(mime)
            transfer.mime = mime
        combined = transfer.carry + payload
        if not self.BASE64.fullmatch(combined):
            raise ValueError("Invalid clipboard base64 alphabet")
        count = len(combined) // 4 * 4
        head, transfer.carry = combined[:count], combined[count:]
        if b"=" in head and transfer.carry:
            raise ValueError("Data follows terminal padding")
        decoded = _decode(head, limit=MAX_TEXT if transfer.local else self.MAX_BYTES)
        transfer.total += len(decoded)
        if transfer.total > (MAX_TEXT if transfer.local else self.MAX_BYTES):
            self._fail("EFBIG")
            return
        if transfer.local:
            transfer.text.extend(decoded)
            return
        outgoing = {
            "type": "wdata",
            "id": transfer.host_id,
            "mime": _encode(mime.encode()),
        }
        normalized = transfer.outgoing_tail + decoded
        count = len(normalized) // 3 * 3
        transfer.outgoing_tail = normalized[count:]
        for offset in range(0, count, self.WRITE_CHUNK_BYTES):
            self.emit(
                _packet(
                    outgoing,
                    normalized[offset : min(count, offset + self.WRITE_CHUNK_BYTES)],
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
        if self._closed or transfer is None or transfer.local:
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
                if status in self.WRITE_ERRORS:
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
            if status in self.READ_ERRORS and not transfer.started:
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
                data = _decode(payload, limit=self.CHUNK_BYTES)
                transfer.total += len(data)
                if transfer.total > (
                    MAX_TEXT if transfer.protocol in ("52", "user") else self.MAX_BYTES
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
