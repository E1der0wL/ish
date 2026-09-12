"""Observe prompt-toolkit attachments and preserve characters across native handoffs."""

import codecs
from contextlib import contextmanager

from prompt_toolkit.input.base import Input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from ish.runtime.observer import InputObserver


class ObservedInput(Input):
    """Preserve the underlying input and join partial characters returned by a tool."""

    def __init__(self, source: Input, observer: InputObserver) -> None:
        """Store the existing input and a shared, observational trace."""
        self.source = source
        self.observer = observer
        self._tokens: list[int | None] = []
        self._returned_prefix = b""
        self._prefix_encoding = "utf-8"

    def fileno(self):
        """Return the original descriptor."""
        return self.source.fileno()

    def typeahead_hash(self):
        """Keep prompt-toolkit's saved typeahead associated with the original input."""
        return self.source.typeahead_hash()

    def read_keys(self):
        """Read existing keys, joining a returned character prefix when one is pending."""
        keys = self.source.read_keys()
        token = self._tokens[-1] if self._tokens else None
        self.observer.read("EDITOR", token, len(keys), unit="keys")
        return self._complete_returned_prefix(keys)

    def flush_keys(self):
        """Delegate the existing parser flush and record only the number of keys."""
        keys = self.source.flush_keys()
        self.observer.record("parser_flush", "EDITOR", keys=len(keys))
        return self._complete_returned_prefix(keys)

    def hold_decoder_prefix(self, encoding: str) -> None:
        """Keep a returned partial character separate from terminal CPR replies."""
        reader = getattr(self.source, "stdin_reader", None)
        if reader is None:
            return
        partial, state = reader._stdin_decoder.getstate()
        if partial:
            self._returned_prefix += partial
            self._prefix_encoding = encoding
            reader._stdin_decoder.setstate((b"", state))

    def take_decoder_prefix(self) -> bytes:
        """Transfer the unfinished returned character to a native input consumer."""
        data = self._returned_prefix
        self._returned_prefix = b""
        return data

    def _complete_returned_prefix(self, keys):
        """Join user bytes to the prefix without inserting intervening CPR responses.

        A new editor requests cursor position after replaying returned input.
        Those ASCII protocol replies must not invalidate a partial UTF-8 value.
        Only this exceptional prefix path reconstructs parsed keys.
        """
        if not self._returned_prefix:
            return keys
        replies = [key for key in keys if key.key == Keys.CPRResponse]
        raw = "".join(key.data for key in keys if key.key != Keys.CPRResponse).encode(
            self._prefix_encoding, errors="surrogateescape"
        )
        decoder = codecs.getincrementaldecoder(self._prefix_encoding)(
            errors="surrogateescape"
        )
        text = decoder.decode(self._returned_prefix + raw)
        self._returned_prefix = decoder.getstate()[0]
        parser = Vt100Parser(replies.append)
        parser.feed(text)
        parser.flush()
        return replies

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
                    self.observer.terminal("EDITOR", fd, "attach")
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
