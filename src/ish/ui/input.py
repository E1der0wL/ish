"""Keep prompt-toolkit input ordered across foreground and primary prompt handoffs."""

import asyncio
import codecs
import re
from contextlib import contextmanager
from itertools import groupby, takewhile

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import (
    emacs_insert_mode,
    emacs_mode,
    is_searching,
    vi_insert_mode,
)
from prompt_toolkit.input.base import Input
from prompt_toolkit.input.typeahead import get_typeahead
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.key_binding.key_processor import KeyProcessor
from prompt_toolkit.keys import Keys

from ish.runtime.observer import InputObserver
from ish.shell.input import PASTE_END, PASTE_START
from ish.shell.limits import TYPEAHEAD_LIMIT_BYTES

_PASTE_START = PASTE_START.decode("ascii")
_PASTE_END = PASTE_END.decode("ascii")
_ASCII_TEXT = re.compile(rb"[ -~]+")


def wire_text(key: KeyPress) -> str:
    """Recover a parsed key's terminal input, including bracketed-paste framing."""
    if key.key == Keys.BracketedPaste:
        return _PASTE_START + key.data + _PASTE_END
    return key.data


class HandoffKeyProcessor(KeyProcessor):
    """Leave PTK's lookahead on its input queue when a shorter binding accepts."""

    def __init__(self, editor_bindings, native_bindings):
        """Keep separate bindings for a live chord and one begun on the foreground PTY."""
        self.editor_bindings = editor_bindings
        self.native_bindings = native_bindings
        super().__init__(editor_bindings)

    def _get_matches(self, key_presses):
        # PTK looks up exact matches before longer prefixes, including on retry.
        # The first key owns the chord even when its last key arrived live.
        self._bindings = (
            self.native_bindings
            if isinstance(key_presses[0], NativeKey)
            else self.editor_bindings
        )
        return super()._get_matches(key_presses)

    def reset(self):
        """End any returned-key search when a new prompt starts."""
        self.native_search = False
        super().reset()

    def can_insert_run(self, buffer):
        """Batch returned text only when every character is an ordinary self-insert."""
        app = get_app()
        return (
            not self.key_buffer
            and not self.input_queue
            and self.arg is None
            and not app.quoted_insert
            and app.current_buffer is buffer
            and (emacs_insert_mode() or vi_insert_mode())
            and not app.emacs_state.is_recording
            and not app.vi_state.recording_register
        )

    def process_replay(self, keys, buffer):
        """Apply editing controls normally and batch consecutive ordinary native text."""

        def plain(key):
            return (
                isinstance(key, NativeKey)
                and not isinstance(key.key, Keys)
                and key.key == key.data
            )

        for printable, group in groupby(keys, key=plain):
            if not printable:
                self.feed_multiple(group)
                self.process_keys()
                continue
            run = list(group)
            for index, key in enumerate(run):
                if get_app().is_done:
                    self.feed_multiple(run[index:])
                    break
                # A pending Meta chord, repeat count, search or vi operation may
                # consume the first printable before normal insertion resumes.
                if self.can_insert_run(buffer):
                    self.feed(
                        NativeKey(Keys.Any, "".join(item.data for item in run[index:]))
                    )
                    self.process_keys()
                    break
                self.feed(key)
                self.process_keys()

    def _call_handler(self, handler, key_sequence):
        first = key_sequence[0]
        may_start_search = (
            isinstance(first, NativeKey)
            and first.key in (Keys.ControlR, Keys.ControlS)
            and emacs_mode()
        )
        super()._call_handler(handler, key_sequence)
        if self.native_search or may_start_search:
            self.native_search = is_searching()
        if get_app().is_done and len(self.key_buffer) > len(key_sequence):
            # PTK normally retries the unmatched chord tail immediately, even
            # if the shorter binding just transferred ownership of stdin.
            self.feed_multiple(self.key_buffer[len(key_sequence) :], first=True)
            del self.key_buffer[len(key_sequence) :]

    def accepted_offset(self, keys, buffered, encoding):
        """Unqueue replay's remaining events and report their original byte boundary.

        Key bindings may also enqueue synthetic input. Keep those events for
        the next prompt. A shorter chord may leave keys from an earlier batch.
        """
        unread = {id(key) for key in self.input_queue}
        replayed = {id(key) for key in (*buffered, *keys)}
        retained = [key for key in self.input_queue if id(key) not in replayed]
        self.input_queue.clear()
        self.feed_multiple(retained)
        handled = takewhile(lambda key: id(key) not in unread, keys)
        consumed = len(
            "".join(map(wire_text, handled)).encode(encoding, "surrogateescape")
        )
        previous = "".join(wire_text(key) for key in buffered if id(key) in unread)
        return consumed, previous.encode(encoding, "surrogateescape")


class NativeKey(KeyPress):
    """A key whose first wire byte came back from the foreground PTY."""


class ReplayInput:
    """Decode returned bytes with VT100 semantics, retaining the unfinished wire suffix.

    The live input keeps its own parser so that a renderer CPR arriving between
    halves of a character or escape sequence can be handled independently.
    """

    def __init__(self, encoding: str):
        """Keep incomplete characters, protocol bytes and decoded events together."""
        self.encoding = encoding
        self.ascii_text = codecs.lookup(encoding).name in ("ascii", "utf-8")
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="surrogateescape")
        self.keys = []
        self.parser = Vt100Parser(self.keys.append)
        self.pending = bytearray()
        self.pending_native = False

    def ascii_run(self, data, start, stop):
        """Match bytes guaranteed to decode as ordinary text at a VT boundary."""
        if self.ascii_text and not self.pending:
            return _ASCII_TEXT.match(data, start, stop)
        return None

    def read(self, data: bytes, *, native=False, flush: bool = False) -> list[KeyPress]:
        """Return decoded events and retain bytes for the next partial key or paste."""
        older = len(self.pending)
        self.pending.extend(data)
        self.parser.feed(self.decoder.decode(data))
        if flush:
            self.parser.flush()
        keys = self.keys[:]
        self.keys.clear()
        consumed = 0
        owner = self.pending_native if older else native
        for index, key in enumerate(keys):
            wire = wire_text(key)
            if wire:
                owner = self.pending_native if consumed < older else native
                consumed += len(wire.encode(self.encoding, "surrogateescape"))
            if owner:
                # Linux ICANON may have mapped the original Enter (CR) to LF.
                keys[index] = NativeKey(
                    Keys.Enter if key.key == Keys.ControlJ else key.key, key.data
                )
        if consumed:
            self.pending = self.pending[consumed:]
        if consumed >= older:
            self.pending_native = native
        if len(self.pending) > TYPEAHEAD_LIMIT_BYTES:
            raise BufferError("Incomplete terminal input exceeds the typeahead limit")
        return keys

    def take(self) -> bytes:
        """Transfer an unfinished character, escape or paste verbatim."""
        pending = bytes(self.pending)
        self.pending.clear()
        self.decoder.reset()
        self.parser.reset()
        self.pending_native = False
        return pending


class ObservedInput(Input):
    """Observe real input ownership and join returned prefixes to subsequent keys."""

    def __init__(
        self, source: Input, observer: InputObserver, encoding: str = "utf-8"
    ) -> None:
        """Attach tracing and replay state to the existing terminal input."""
        self.source = source
        self.observer = observer
        self._tokens: list[int | None] = []
        self.hold_keys = None
        self.replay = ReplayInput(encoding)
        self.flush_task = None

    def fileno(self):
        """Return the original descriptor."""
        return self.source.fileno()

    def typeahead_hash(self):
        """Associate saved typeahead with the original prompt-toolkit input."""
        return self.source.typeahead_hash()

    def read_keys(self):
        """Read live keys and join an older unfinished sequence if one is pending."""
        if self.flush_task is not None:
            self.flush_task.cancel()
            self.flush_task = None
        keys = self.source.read_keys()
        token = self._tokens[-1] if self._tokens else None
        self.observer.read("EDITOR", token, len(keys), unit="keys")
        return self.prepare_keys(keys)

    def schedule_flush(self, app):
        """Start PTK's escape timeout when replay happened before the first live read."""
        if self.flush_task is not None:
            self.flush_task.cancel()

        async def flush():
            await asyncio.sleep(app.ttimeoutlen)
            if not app.is_done:
                app.key_processor.feed_multiple(self.replay.read(b"", flush=True))
                app.key_processor.process_keys()

        self.flush_task = app.create_background_task(flush())

    def flush_keys(self):
        """Flush ambiguous live and returned VT sequences on PTK's normal timeout."""
        keys = self.source.flush_keys()
        self.observer.record("parser_flush", "EDITOR", keys=len(keys))
        keys = self.prepare_keys(keys)
        if self.hold_keys is None and self.replay.pending:
            keys.extend(self.replay.read(b"", flush=True))
        return keys

    def prepare_keys(self, keys):
        """Preserve live key identities except while finishing an older VT sequence."""
        if self.hold_keys is not None:
            keys = self.hold_keys(keys)
        if not self.replay.pending:
            return keys
        joined = []
        for reply, group in groupby(keys, key=lambda key: key.key == Keys.CPRResponse):
            if reply or not self.replay.pending:
                joined.extend(group)
            else:
                # One physical VT sequence can emit two logical keys. Reparse
                # the wire data as a group so its second event isn't duplicated.
                data = "".join(map(wire_text, group)).encode(
                    self.replay.encoding, "surrogateescape"
                )
                joined.extend(self.replay.read(data))
        return joined

    def take_pending(self) -> bytes:
        """Transfer unread keys and unfinished protocol/decoder bytes exactly once."""
        encoding = self.replay.encoding
        data = self.replay.take()
        keys = get_typeahead(self)
        keys.extend(self.source.flush_keys())
        data += "".join(
            wire_text(key) for key in keys if key.key != Keys.CPRResponse
        ).encode(encoding, "surrogateescape")
        parser = getattr(self.source, "vt100_parser", None)
        if parser is not None and parser._in_bracketed_paste:
            # PTK's flush waits for a closing marker. A different stdin owner
            # must instead receive all the bytes already read from the terminal.
            data += (_PASTE_START + parser._paste_buffer).encode(
                encoding, "surrogateescape"
            )
            parser.reset()
        reader = getattr(self.source, "stdin_reader", None)
        if reader is not None:
            decoder = reader._stdin_decoder
            partial, state = decoder.getstate()
            data += partial
            decoder.setstate((b"", state))
        return data

    def flush(self):
        """Delegate the input's original flush behavior."""
        return self.source.flush()

    @property
    def closed(self):
        """Expose the underlying EOF state."""
        return self.source.closed

    def raw_mode(self):
        """Use the underlying terminal-mode context unchanged."""
        return self.source.raw_mode()

    def cooked_mode(self):
        """Use the underlying cooked-mode context unchanged."""
        return self.source.cooked_mode()

    @contextmanager
    def attach(self, input_ready_callback):
        """Observe actual attachment, including cleanup after an exception."""
        self._tokens.append(self.observer.begin("EDITOR"))
        try:
            with self.source.attach(input_ready_callback):
                if self.observer.enabled:
                    try:
                        fd = self.fileno()
                    except (OSError, NotImplementedError):
                        fd = None
                    self.observer.sample("EDITOR", fd, "attach")
                yield
        finally:
            self.observer.end(self._tokens.pop())

    @contextmanager
    def detach(self):
        """Observe temporary detachment used by run-in-terminal without rerouting keys."""
        suspended = False
        try:
            with self.source.detach():
                if self._tokens and self._tokens[-1] is not None:
                    self.observer.end(self._tokens[-1])
                    self._tokens[-1] = None
                    suspended = True
                yield
        finally:
            if suspended:
                self._tokens[-1] = self.observer.begin("EDITOR")

    def close(self):
        """Delegate explicit closure to the original input."""
        return self.source.close()
