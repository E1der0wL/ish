"""Private Xlib selection worker (no PTK imports or terminal escape sequences).

Uses the system libX11, like terminal Vim. See ICCCM selections and INCR:
https://www.x.org/releases/current/doc/xorg-docs/icccm/icccm.html
This module is launched by x11_clipboard.py, never in the UI process.
"""

import ctypes as C
import locale
import os
import select
import signal
import sys
import time
from dataclasses import dataclass, fields
from typing import NamedTuple

if __package__:
    from .clipboard_wire import MAX_BYTES, ClipboardText, X11Wire
else:
    from clipboard_wire import MAX_BYTES, ClipboardText, X11Wire


CHUNK = 64 * 1024
PROPERTY_CHANGE = 1 << 22
SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY, PROPERTY_NOTIFY = 29, 30, 31, 28
U = C.c_ulong
P = C.c_void_p
INT = C.c_int
COMMON = [("type", INT), ("serial", U), ("send_event", INT), ("display", P)]


class SelectionRequest(C.Structure):
    _fields_ = COMMON + [
        (name, U)
        for name in ("owner", "requestor", "selection", "target", "property", "time")
    ]


class SelectionNotify(C.Structure):
    _fields_ = COMMON + [
        (name, U) for name in ("requestor", "selection", "target", "property", "time")
    ]


class PropertyNotify(C.Structure):
    _fields_ = (
        COMMON + [(name, U) for name in ("window", "atom", "time")] + [("state", INT)]
    )


class Event(C.Union):
    _fields_ = [
        ("type", INT),
        ("request", SelectionRequest),
        ("selection", SelectionNotify),
        ("property", PropertyNotify),
        ("pad", C.c_long * 24),
    ]


class TextProperty(C.Structure):
    _fields_ = [("value", P), ("encoding", U), ("format", INT), ("nitems", U)]


class XlibABI:
    LIBRARY = "libX11.so.6"
    ABSENT = None
    SUCCESS = 0
    FALSE = 0
    TRUE = 1
    CURRENT_TIME = 0
    NULL_ATOM = 0
    ANY_PROPERTY_TYPE = 0
    PROPERTY_REPLACE = 0
    PROPERTY_NEW_VALUE = 0
    PROPERTY_DELETED = 1
    FORMAT_BYTES = 8
    FORMAT_LONGS = 32
    LONG_BYTES = 4
    NO_EVENT_MASK = 0
    EVENT_SERIAL = 0
    WINDOW_ARGUMENTS = (0, 0, 1, 1, 0, 0, 0)
    TEXT_STRING = 0
    TEXT_COMPOUND = 1
    TEXT_ICCCM = 3
    SIGNATURES = {
        "XOpenDisplay": (P, [C.c_char_p]),
        "XCloseDisplay": (INT, [P]),
        "XDefaultRootWindow": (U, [P]),
        "XConnectionNumber": (INT, [P]),
        "XCreateSimpleWindow": (
            U,
            [P, U, INT, INT, C.c_uint, C.c_uint, C.c_uint, U, U],
        ),
        "XInternAtom": (U, [P, C.c_char_p, INT]),
        "XSelectInput": (INT, [P, U, C.c_long]),
        "XSetSelectionOwner": (INT, [P, U, U, U]),
        "XGetSelectionOwner": (U, [P, U]),
        "XConvertSelection": (INT, [P, U, U, U, U, U]),
        "XChangeProperty": (INT, [P, U, U, U, INT, INT, P, INT]),
        "XDeleteProperty": (INT, [P, U, U]),
        "XGetWindowProperty": (
            INT,
            [
                P,
                U,
                U,
                C.c_long,
                C.c_long,
                INT,
                U,
                C.POINTER(U),
                C.POINTER(INT),
                C.POINTER(U),
                C.POINTER(U),
                C.POINTER(P),
            ],
        ),
        "XPending": (INT, [P]),
        "XNextEvent": (INT, [P, C.POINTER(Event)]),
        "XSendEvent": (INT, [P, U, INT, C.c_long, C.POINTER(Event)]),
        "XFree": (INT, [P]),
        "Xutf8TextListToTextProperty": (
            INT,
            [P, C.POINTER(C.c_char_p), INT, INT, C.POINTER(TextProperty)],
        ),
        "Xutf8TextPropertyToTextList": (
            INT,
            [
                P,
                C.POINTER(TextProperty),
                C.POINTER(C.POINTER(C.c_char_p)),
                C.POINTER(INT),
            ],
        ),
        "XFreeStringList": (None, [C.POINTER(C.c_char_p)]),
    }


@dataclass(frozen=True, slots=True)
class SelectionAtoms:
    CLIPBOARD: int
    UTF8_STRING: int
    COMPOUND_TEXT: int
    STRING: int
    TEXT: int
    TARGETS: int
    TIMESTAMP: int
    MULTIPLE: int
    ATOM_PAIR: int
    ATOM: int
    INTEGER: int
    INCR: int
    _HUB_CLIPBOARD: int

    @classmethod
    def create(cls, x, display):
        return cls(
            **{
                field.name: x.XInternAtom(
                    display, field.name.encode(ClipboardText.UTF8), XlibABI.FALSE
                )
                for field in fields(cls)
            }
        )

    @property
    def read_targets(self):
        return (self.UTF8_STRING, self.COMPOUND_TEXT, self.STRING)

    @property
    def advertised(self):
        return [
            self.TARGETS,
            self.TIMESTAMP,
            self.MULTIPLE,
            self.UTF8_STRING,
            self.COMPOUND_TEXT,
            self.STRING,
            self.TEXT,
        ]

    @property
    def text_styles(self):
        return {
            self.STRING: XlibABI.TEXT_STRING,
            self.COMPOUND_TEXT: XlibABI.TEXT_COMPOUND,
            self.TEXT: XlibABI.TEXT_ICCCM,
        }


class PropertyValue(NamedTuple):
    kind: int
    format: int
    data: bytes | tuple[int, ...]


class ConvertedText(NamedTuple):
    kind: int
    data: bytes


class TransferKey(NamedTuple):
    window: int
    property: int


class IncrementalTransfer(NamedTuple):
    kind: int
    data: bytes
    offset: int
    updated_at: float


class WorkerLifetime:
    PROCESS_LIBRARY = None
    USE_ERRNO = True
    PARENT_DEATH_SIGNAL = 1
    UNUSED = 0
    SUCCESS = 0
    INIT_PID = 1
    STDIN = 0
    PARENT_PROBE_BYTES = 1
    ENVIRONMENT_LOCALE = ""
    FALLBACK_LOCALE = "C"
    ERROR = "Cannot bind clipboard worker lifetime"


class Selection:
    ABSENT = None
    EMPTY_BYTES = b""
    BYTE_NEWLINE = b"\n"
    LATIN1 = "latin-1"
    KEEP_PROPERTY = False
    DELETE_PROPERTY = True
    WATCH_PARENT = False
    WATCH_PARENT_ENABLED = True
    ACCEPTED = True
    REJECTED = False
    DEADLINE_SECONDS = 2.5
    PARENT_POLL_SECONDS = 1
    TRANSFER_TTL_SECONDS = 10
    START_OFFSET = 0
    PAIR_WIDTH = 2
    SINGLE_TEXT = 1
    LIBRARY_ERROR = "libX11 unavailable"
    DISPLAY_ERROR = "X11 display unavailable"
    PROPERTY_ERROR = "X11 property unavailable or too large"
    INCREMENTAL_ERROR = "Invalid incremental clipboard text"
    TOO_LARGE = "Clipboard text too large"
    INVALID_TEXT = "Invalid clipboard text"
    CONVERSION_ERROR = "Clipboard text conversion failed"
    NO_TARGET = "Clipboard has no text target"
    OWNERSHIP_ERROR = "Could not own clipboard"

    def __init__(self) -> None:
        try:
            self.x = C.CDLL(XlibABI.LIBRARY)
        except OSError as exc:
            raise ImportError(self.LIBRARY_ERROR) from exc
        for name, (result, arguments) in XlibABI.SIGNATURES.items():
            function = getattr(self.x, name)
            function.restype, function.argtypes = result, arguments
        # A disappearing paste requestor must not kill the selection owner.
        # Xlib's fatal INT/O handler remains confined to this private process.
        handler_type = C.CFUNCTYPE(INT, P, P)
        self.error_handler = handler_type(lambda display, error: XlibABI.SUCCESS)
        self.x.XSetErrorHandler.argtypes = [handler_type]
        self.x.XSetErrorHandler.restype = P
        self.x.XSetErrorHandler(self.error_handler)
        self.display = self.x.XOpenDisplay(XlibABI.ABSENT)
        if not self.display:
            raise ConnectionError(self.DISPLAY_ERROR)
        self.window = self.x.XCreateSimpleWindow(
            self.display,
            self.x.XDefaultRootWindow(self.display),
            *XlibABI.WINDOW_ARGUMENTS,
        )
        self.x.XSelectInput(self.display, self.window, PROPERTY_CHANGE)
        self.fd = self.x.XConnectionNumber(self.display)
        self.atoms = SelectionAtoms.create(self.x, self.display)
        self.transfers = {}
        self.content = self.EMPTY_BYTES
        self.timestamp = XlibABI.CURRENT_TIME

    def close(self) -> None:
        self.x.XCloseDisplay(self.display)

    def property(
        self, window: int, atom: int, *, delete: bool = KEEP_PROPERTY
    ) -> PropertyValue:
        kind, size, count, after, value = U(), INT(), U(), U(), P()
        status = self.x.XGetWindowProperty(
            self.display,
            window,
            atom,
            self.START_OFFSET,
            MAX_BYTES // XlibABI.LONG_BYTES,
            delete,
            XlibABI.ANY_PROPERTY_TYPE,
            C.byref(kind),
            C.byref(size),
            C.byref(count),
            C.byref(after),
            C.byref(value),
        )
        try:
            if status or after.value:
                raise RuntimeError(self.PROPERTY_ERROR)
            if size.value == XlibABI.FORMAT_LONGS:
                data = tuple(
                    C.cast(value, C.POINTER(U))[index] for index in range(count.value)
                )
            elif size.value == XlibABI.FORMAT_BYTES:
                data = C.string_at(value, count.value)
            else:
                data = self.EMPTY_BYTES
            return PropertyValue(kind.value, size.value, data)
        finally:
            if value:
                self.x.XFree(value)

    def put(
        self, window: int, atom: int, kind: int, data, size: int = XlibABI.FORMAT_BYTES
    ) -> None:
        buffer = (
            (U * len(data))(*data)
            if size == XlibABI.FORMAT_LONGS
            else C.create_string_buffer(data)
        )
        self.x.XChangeProperty(
            self.display,
            window,
            atom,
            kind,
            size,
            XlibABI.PROPERTY_REPLACE,
            buffer,
            len(data),
        )

    def event(self, deadline: float, *, watch_parent: bool = WATCH_PARENT) -> Event:
        while time.monotonic() < deadline:
            if self.x.XPending(self.display):
                event = Event()
                self.x.XNextEvent(self.display, C.byref(event))
                return event
            # XPending already flushes requests and checks Xlib's event queue.
            # Another XFlush can read a newly arriving event into that queue;
            # polling only the socket afterward would then miss the event.
            ready, _, _ = select.select(
                [self.fd, *([WorkerLifetime.STDIN] if watch_parent else [])],
                [],
                [],
                max(0, deadline - time.monotonic()),
            )
            if (
                watch_parent
                and WorkerLifetime.STDIN in ready
                and not os.read(WorkerLifetime.STDIN, WorkerLifetime.PARENT_PROBE_BYTES)
            ):
                raise EOFError
        raise TimeoutError

    def read(self) -> bytes:
        if not self.x.XGetSelectionOwner(self.display, self.atoms.CLIPBOARD):
            return self.EMPTY_BYTES
        deadline = time.monotonic() + self.DEADLINE_SECONDS
        prop = self.atoms._HUB_CLIPBOARD
        for target in self.atoms.read_targets:
            self.x.XConvertSelection(
                self.display,
                self.atoms.CLIPBOARD,
                target,
                prop,
                self.window,
                XlibABI.CURRENT_TIME,
            )
            while True:
                event = self.event(deadline)
                if event.type == SELECTION_NOTIFY and event.selection.target == target:
                    break
            if not event.selection.property:
                continue
            kind, size, data = self.property(
                self.window, prop, delete=self.DELETE_PROPERTY
            )
            if kind == self.atoms.INCR:
                chunks, length = [], 0
                while True:
                    event = self.event(deadline)
                    if (
                        event.type != PROPERTY_NOTIFY
                        or event.property.atom != prop
                        or event.property.state != XlibABI.PROPERTY_NEW_VALUE
                    ):
                        continue
                    kind, size, data = self.property(
                        self.window, prop, delete=self.DELETE_PROPERTY
                    )
                    if not kind:  # An already consumed initial property notification.
                        continue
                    if size != XlibABI.FORMAT_BYTES:
                        raise RuntimeError(self.INCREMENTAL_ERROR)
                    if not data:
                        break
                    length += len(data)
                    if length > MAX_BYTES:
                        raise RuntimeError(self.TOO_LARGE)
                    chunks.append(data)
                data = self.EMPTY_BYTES.join(chunks)
            if size != XlibABI.FORMAT_BYTES:
                raise RuntimeError(self.INVALID_TEXT)
            if kind == self.atoms.UTF8_STRING:
                return data.decode(ClipboardText.UTF8).encode(ClipboardText.UTF8)
            if kind == self.atoms.STRING:
                return data.decode(self.LATIN1).encode(ClipboardText.UTF8)
            buffer = C.create_string_buffer(data)
            text = TextProperty(C.cast(buffer, P), kind, size, len(data))
            strings, count = C.POINTER(C.c_char_p)(), INT()
            status = self.x.Xutf8TextPropertyToTextList(
                self.display, C.byref(text), C.byref(strings), C.byref(count)
            )
            try:
                if status < XlibABI.SUCCESS:
                    raise RuntimeError(self.CONVERSION_ERROR)
                return self.BYTE_NEWLINE.join(
                    strings[index] for index in range(count.value)
                )
            finally:
                if strings:
                    self.x.XFreeStringList(strings)
        raise RuntimeError(self.NO_TARGET)

    def convert(self, target: int) -> ConvertedText | None:
        if target == self.atoms.UTF8_STRING:
            return ConvertedText(target, self.content)
        styles = self.atoms.text_styles
        if target not in styles:
            return self.ABSENT
        text = TextProperty()
        strings = (C.c_char_p * self.SINGLE_TEXT)(self.content)
        status = self.x.Xutf8TextListToTextProperty(
            self.display, strings, self.SINGLE_TEXT, styles[target], C.byref(text)
        )
        try:
            if (
                status != XlibABI.SUCCESS
            ):  # Never report a lossy conversion as successful.
                return self.ABSENT
            return ConvertedText(text.encoding, C.string_at(text.value, text.nitems))
        finally:
            if text.value:
                self.x.XFree(text.value)

    def answer(self, window: int, target: int, prop: int) -> bool:
        if not prop:
            return self.REJECTED
        if target == self.atoms.TARGETS:
            self.put(
                window,
                prop,
                self.atoms.ATOM,
                self.atoms.advertised,
                XlibABI.FORMAT_LONGS,
            )
        elif target == self.atoms.TIMESTAMP:
            self.put(
                window, prop, self.atoms.INTEGER, [self.timestamp], XlibABI.FORMAT_LONGS
            )
        elif target == self.atoms.MULTIPLE:
            kind, size, data = self.property(window, prop)
            if (
                kind not in (self.atoms.ATOM_PAIR, self.atoms.ATOM)
                or size != XlibABI.FORMAT_LONGS
                or len(data) % self.PAIR_WIDTH
            ):
                return self.REJECTED
            pairs = list(data)
            for index in range(self.START_OFFSET, len(pairs), self.PAIR_WIDTH):
                if (
                    pairs[index] == target
                    or pairs[index + 1] == prop
                    or not self.answer(window, pairs[index], pairs[index + 1])
                ):
                    pairs[index + 1] = XlibABI.NULL_ATOM
            self.put(window, prop, self.atoms.ATOM_PAIR, pairs, XlibABI.FORMAT_LONGS)
        else:
            converted = self.convert(target)
            if converted is self.ABSENT:
                return self.REJECTED
            kind, data = converted
            if len(data) > CHUNK:
                self.x.XSelectInput(self.display, window, PROPERTY_CHANGE)
                self.transfers[TransferKey(window, prop)] = IncrementalTransfer(
                    kind, data, self.START_OFFSET, time.monotonic()
                )
                self.put(
                    window, prop, self.atoms.INCR, [len(data)], XlibABI.FORMAT_LONGS
                )
            else:
                self.put(window, prop, kind, data)
        return self.ACCEPTED

    def copy(self, content: bytes) -> None:
        self.content = content
        prop = self.atoms._HUB_CLIPBOARD
        self.put(self.window, prop, self.atoms.INTEGER, self.EMPTY_BYTES)
        deadline = time.monotonic() + self.DEADLINE_SECONDS
        while True:
            event = self.event(deadline)
            if event.type == PROPERTY_NOTIFY and event.property.atom == prop:
                self.timestamp = event.property.time
                break
        self.x.XSetSelectionOwner(
            self.display, self.atoms.CLIPBOARD, self.window, self.timestamp
        )
        if self.x.XGetSelectionOwner(self.display, self.atoms.CLIPBOARD) != self.window:
            raise RuntimeError(self.OWNERSHIP_ERROR)
        sys.stdout.buffer.write(X11Wire.OWNED)
        sys.stdout.buffer.flush()
        while True:
            try:
                event = self.event(
                    time.monotonic() + self.PARENT_POLL_SECONDS,
                    watch_parent=self.WATCH_PARENT_ENABLED,
                )
            except TimeoutError:
                # A requestor can disappear during an INCR transfer.
                self.transfers = {
                    key: value
                    for key, value in self.transfers.items()
                    if time.monotonic() - value.updated_at < self.TRANSFER_TTL_SECONDS
                }
                continue
            if event.type == SELECTION_CLEAR:
                return
            if event.type == SELECTION_REQUEST:
                request = event.request
                prop = request.property or request.target
                accepted = request.selection == self.atoms.CLIPBOARD and self.answer(
                    request.requestor, request.target, prop
                )
                reply = Event()
                reply.selection = SelectionNotify(
                    SELECTION_NOTIFY,
                    XlibABI.EVENT_SERIAL,
                    XlibABI.TRUE,
                    self.display,
                    request.requestor,
                    request.selection,
                    request.target,
                    prop if accepted else XlibABI.NULL_ATOM,
                    request.time,
                )
                self.x.XSendEvent(
                    self.display,
                    request.requestor,
                    XlibABI.FALSE,
                    XlibABI.NO_EVENT_MASK,
                    C.byref(reply),
                )
            elif (
                event.type == PROPERTY_NOTIFY
                and event.property.state == XlibABI.PROPERTY_DELETED
            ):
                key = TransferKey(event.property.window, event.property.atom)
                transfer = self.transfers.get(key)
                if transfer:
                    kind, data, offset, _ = transfer
                    chunk = data[offset : offset + CHUNK]
                    self.put(*key, kind, chunk)
                    if chunk:
                        self.transfers[key] = IncrementalTransfer(
                            kind, data, offset + len(chunk), time.monotonic()
                        )
                    else:
                        del self.transfers[key]


def main() -> None:
    # A killed host cannot leave a selection owner (and its text) orphaned.
    parent = os.getppid()
    libc = C.CDLL(WorkerLifetime.PROCESS_LIBRARY, use_errno=WorkerLifetime.USE_ERRNO)
    if (
        libc.prctl(
            WorkerLifetime.PARENT_DEATH_SIGNAL,
            signal.SIGTERM,
            WorkerLifetime.UNUSED,
            WorkerLifetime.UNUSED,
            WorkerLifetime.UNUSED,
        )
        != WorkerLifetime.SUCCESS
    ):
        raise OSError(C.get_errno(), WorkerLifetime.ERROR)
    if parent == WorkerLifetime.INIT_PID or os.getppid() != parent:
        return
    try:
        locale.setlocale(locale.LC_CTYPE, WorkerLifetime.ENVIRONMENT_LOCALE)
    except locale.Error:
        # SSH can forward a locale that is not installed on the remote host.
        # UTF8_STRING does not depend on that locale being available.
        locale.setlocale(locale.LC_CTYPE, WorkerLifetime.FALLBACK_LOCALE)
    selection = Selection()
    try:
        if sys.argv[1] == X11Wire.READ:
            sys.stdout.buffer.write(selection.read())
        else:
            header = sys.stdin.buffer.read(X11Wire.HEADER_BYTES)
            if len(header) != X11Wire.HEADER_BYTES:
                raise EOFError
            length = X11Wire.length(header)
            if length > MAX_BYTES:
                raise ValueError(Selection.TOO_LARGE)
            content = sys.stdin.buffer.read(length)
            if len(content) != length:
                raise EOFError
            selection.copy(content)
    finally:
        selection.close()


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass
    except ImportError:
        sys.exit(X11Wire.LIBRARY_ERROR)
    except ConnectionError:
        sys.exit(X11Wire.CONNECTION_ERROR)
    except TimeoutError:
        sys.exit(X11Wire.TIMEOUT_ERROR)
    except (OSError, RuntimeError, ValueError):
        sys.exit(X11Wire.TRANSFER_ERROR)
