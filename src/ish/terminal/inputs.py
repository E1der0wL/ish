"""Choose one input implementation without duplicating the native VT state.

Engine support is verified before this boundary is created. Routes are fixed
for its lifetime; a failed or partially consumed transfer is never replayed in
another implementation. Host output support is a separate presentation fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ish.lang import Message, i18n

from .extensions import MAX_PREFIX_BYTES, ProtocolRegistry, ProtocolResult
from .protocol import ProtocolStream

if TYPE_CHECKING:
    from .engine import EngineInfo
    from .vt import Terminal

Provider = Literal["native", "adapter", "unsupported"]


@dataclass(frozen=True, slots=True)
class InputRoutes:
    """Implementation ownership, not permission to display on a particular host."""

    kitty: Provider
    iterm: Provider
    sixel: Provider


def select_routes(engine: EngineInfo) -> InputRoutes:
    """Prefer verified native input; the iTerm shim needs native Kitty storage.

    Retire the adapter branch and its lazy import when a supported engine
    release passes the same image lifetime, cursor and cancellation tests.
    No unknown engine version is promoted here merely because it is newer.
    """
    native = engine.native_inputs
    return InputRoutes(
        kitty="native" if "kitty" in native else "unsupported",
        iterm="native"
        if "iterm" in native
        else "adapter"
        if "kitty" in native
        else "unsupported",
        sixel="native"
        if "sixel" in native
        else "adapter"
        if "kitty" in native
        else "unsupported",
    )


def _iterm_adapter(terminal):
    # Native routes never import the supplemental decoder. Its transfer state
    # and dependency can be removed without changing Terminal or Application.
    from .image_protocol import Images

    return Images(terminal)


def _sixel_adapter(terminal):
    from .image_protocol import SixelImages

    return SixelImages(terminal)


class _ImageAdapter:
    """Keep the existing image codec behind the shared lifecycle contract."""

    def __init__(self, images):
        self.images = images

    def handle(self, control):
        self.images.handle(control.body)
        return ProtocolResult()

    def cancel(self):
        self.images.cancel()

    def close(self):
        self.images.cancel()


class InputPipeline:
    """Own bounded framing and transfer adapters around one native writer.

    Clipboard is an asynchronous policy broker, not an engine fallback. Keep
    its capture priority and acknowledgement ordering even if the engine gains
    a synchronous clipboard callback in a future release.
    """

    ITERM_IMAGES = (
        b"1337;File=",
        b"1337;MultipartFile=",
        b"1337;FilePart=",
        b"1337;FileEnd",
    )
    KITTY_IMAGE = b"G"
    # libghostty's legacy default encodes some modified controls even before an
    # application opts in. Its public keyboard flags cover Kitty negotiation only.
    MODIFY_OTHER_KEYS = re.compile(rb">0*4(?:;0*([012])?)?m")
    RESET_OTHER_KEYS = re.compile(rb">(?:0*4n|m)")

    def __init__(self, terminal: Terminal, engine: EngineInfo, *, reply=None):
        """Select image ownership once, independently of host negotiations."""
        self._write = terminal.feed_raw
        self._terminal = terminal
        self._reply = reply
        self._routes = select_routes(engine)
        self._images = (
            _iterm_adapter(terminal) if self._routes.iterm == "adapter" else None
        )
        self._sixel = (
            _sixel_adapter(terminal) if self._routes.sixel == "adapter" else None
        )
        self._unknown_osc = getattr(terminal, "is_unknown_osc", None)
        self._native_images = frozenset(engine.native_inputs)
        self._native = {
            name: (kind, prefixes) for name, kind, prefixes in engine.native_protocols
        }
        self._protocols = ProtocolRegistry(
            native=self._native, write=self._write, reply=reply
        )
        self._protocols.register(
            "iterm",
            kind="osc",
            prefixes=self.ITERM_IMAGES,
            provider=self._routes.iterm,
            factory=lambda: _ImageAdapter(self._images),
        )
        self._clipboard = None
        self._notifications = None
        self._before_notification = None
        self._started = False
        self.modified_keys_requested = False
        self._closed = False
        self._cancelled_image_record = None
        self._stream = ProtocolStream(
            self._text,
            self._control,
            cancel=self.cancel,
            capture=self._capture,
            opaque=lambda: self._clipboard is not None and self._clipboard.capturing,
        )

    @property
    def routes(self) -> InputRoutes:
        """Read the immutable session selection."""
        return self._routes

    @property
    def image_errors(self) -> tuple[str, ...]:
        """Return diagnostics without exposing an adapter instance to the UI."""
        return tuple(
            error
            for adapter in (self._images, self._sixel)
            if adapter is not None
            for error in adapter.errors
        )

    @property
    def image_failure_count(self) -> int:
        """Count rejected shim images without retaining input or decoder messages."""
        return sum(
            adapter.error_count
            for adapter in (self._images, self._sixel)
            if adapter is not None
        )

    def graphics_changed(self, enabled):
        """Advertise the adapter only while native storage/output are enabled."""
        self._terminal.set_sixel_supported(
            bool(enabled and self._routes.sixel != "unsupported")
        )

    @property
    def protocol_errors(self):
        """Read failures without exposing adapters or their buffered payloads."""
        return self._protocols.errors

    @property
    def protocol_failures(self):
        """Read payload-free failure counts for normal session diagnostics."""
        return self._protocols.failures

    @property
    def framing_drops(self):
        """Count over-limit records without retaining their payloads."""
        return self._stream.dropped

    def register(self, name, *, kind, prefixes, factory):
        """Add a supplemental OSC namespace, never a native command override.

        The public scope is deliberately narrower than the internal registry:
        only complete numeric OSC command prefixes unimplemented by the pinned
        engine are accepted. APC/DCS/CSI need their own native ownership proof.
        """
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_PROTOCOL_LATE))
        if kind != "osc" or not isinstance(prefixes, tuple) or not prefixes:
            raise ValueError(i18n.get(Message.VT_PROTOCOL_OSC))
        if not isinstance(name, str):
            raise ValueError(i18n.get(Message.VT_PROTOCOL_NAME))
        if name in self._native:
            if self._native[name] != (kind, prefixes):
                raise ValueError(i18n.get(Message.VT_PROTOCOL_NATIVE))
        elif name in self._native_images:
            raise ValueError(i18n.get(Message.VT_PROTOCOL_NATIVE))
        for prefix in prefixes:
            if (
                not isinstance(prefix, bytes)
                or not 1 < len(prefix) <= MAX_PREFIX_BYTES
                or not prefix.endswith(b";")
                or not prefix[:-1].isdigit()
                or str(int(prefix[:-1])).encode() != prefix[:-1]
            ):
                raise ValueError(i18n.get(Message.VT_PROTOCOL_NUMBER))
            if name not in self._native and (
                self._unknown_osc is None or not self._unknown_osc(prefix)
            ):
                raise ValueError(i18n.get(Message.VT_PROTOCOL_NATIVE))
        return self._protocols.register(
            name, kind=kind, prefixes=prefixes, factory=factory
        )

    @property
    def clipboard(self):
        """Read the separately owned clipboard policy broker."""
        return self._clipboard

    @clipboard.setter
    def clipboard(self, broker):
        if broker is self._clipboard:
            return
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_CLIPBOARD_LATE))
        self._clipboard = broker

    def feed(self, data: bytes) -> None:
        """Preserve the exact wire order across fragmented PTY reads."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_INPUT_CLOSED))
        if len(data) == 0:
            return
        self._started = True
        self._protocols.start()
        try:
            self._stream.feed(data)
        except Exception:
            self.close()
            raise

    def set_notifications(self, broker, *, before):
        """Attach optional effects without changing the shared native writer."""
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_PROTOCOL_LATE))
        self._notifications = broker
        self._before_notification = before

    def _text(self, data):
        if self._clipboard is None or not self._clipboard.capture(data):
            self._write(data)

    def _capture(self, control):
        """Preserve an active clipboard capture before executing embedded C0 bytes."""
        clipboard = self._clipboard
        if clipboard is None or not clipboard.capturing:
            return False
        if control.kind == "osc" and clipboard.child_osc(control.body):
            return True
        return clipboard.capture(control.raw)

    def _control(self, control):
        cancelled_image = (
            self._cancelled_image_record is not None
            and self._cancelled_image_record == self._stream.record_id
        )
        self._cancelled_image_record = None
        clipboard = self._clipboard
        if clipboard is not None:
            if control.kind == "osc" and clipboard.child_osc(control.body):
                return True
            if clipboard.capture(control.raw):
                return True
            if control.kind == "csi" and clipboard.child_csi(control.body):
                return True
        if cancelled_image and self._is_image(control):
            return True
        if control.kind == "csi":
            if control.body.startswith(b">"):
                setting = self.MODIFY_OTHER_KEYS.fullmatch(control.body)
                if setting:
                    self.modified_keys_requested = setting[1] in (b"1", b"2")
                elif self.RESET_OTHER_KEYS.fullmatch(control.body):
                    self.modified_keys_requested = False
            elif control.body == b"!p":
                self.modified_keys_requested = False
        if (
            self._notifications is not None
            and control.kind == "osc"
            and control.body.startswith(b"99;")
        ):
            # Preserve native 9/777 -> supplemental 99 order within one read.
            self._before_notification()
            return self._notifications.handle(control)
        if control.kind == "esc" and control.body == b"c":
            self.modified_keys_requested = False
            self._protocols.cancel()
            if self._sixel is not None:
                self._sixel.reset()
            if self._notifications is not None:
                self._notifications.cancel()
            if clipboard is not None:
                clipboard.reset()
        if self._sixel is not None:
            if control.kind == "dcs":
                from .sixel import Decoder

                if Decoder.matches(control.body):
                    self._sixel.handle(control.body)
                    return True
            elif control.kind == "csi" and self._reply is not None:
                if self._sixel.csi(control.body, self._reply):
                    return True
        return self._protocols.handle(control)

    def _is_image(self, control):
        """Recognize image namespaces without consuming unrelated records."""
        if control.kind == "osc":
            return control.body.startswith(self.ITERM_IMAGES)
        if control.kind == "apc":
            return control.body.startswith(self.KITTY_IMAGE)
        if control.kind == "dcs":
            from .sixel import Decoder

            return Decoder.matches(control.body)
        return False

    def cancel_images(self) -> None:
        """Cancel the current image record and incomplete adapter transfers.

        Framing remains active so a partial title, clipboard record or query
        can still finish. Only this identity is suppressed if it proves to be
        an image; a later graphics enable cannot resurrect that old record.
        """
        self._cancelled_image_record = self._stream.record_id
        if self._images is not None:
            self._images.cancel()
        if self._sixel is not None:
            self._sixel.cancel()

    def cancel(self) -> None:
        """Keep existing capture cancellation semantics at framing boundaries."""
        self._protocols.cancel()
        if self._notifications is not None:
            self._notifications.cancel()
        if self._clipboard is not None:
            self._clipboard.cancel()

    def close(self) -> None:
        """Discard incomplete framing once before the native owner is freed."""
        if not self._closed:
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._protocols.close()
                if self._notifications is not None:
                    self._notifications.close()
