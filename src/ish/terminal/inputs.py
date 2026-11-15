"""Choose one input implementation without duplicating the native VT state.

Engine support is verified before this boundary is created. Routes are fixed
for its lifetime; a failed or partially consumed transfer is never replayed in
another implementation. Host output support is a separate presentation fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .protocol import ProtocolStream

if TYPE_CHECKING:
    from .engine import EngineInfo
    from .vt import Terminal

Provider = Literal["native", "adapter", "unsupported"]
ITERM_IMAGES = (
    b"1337;File=",
    b"1337;MultipartFile=",
    b"1337;FilePart=",
    b"1337;FileEnd",
)


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
        sixel="native" if "sixel" in native else "unsupported",
    )


def _iterm_adapter(terminal):
    # Native routes never import the supplemental decoder. Its transfer state
    # and dependency can be removed without changing Terminal or Application.
    from .image_protocol import Images

    return Images(terminal)


class InputPipeline:
    """Own bounded framing and transfer adapters around one native writer.

    Clipboard is an asynchronous policy broker, not an engine fallback. Keep
    its capture priority and acknowledgement ordering even if the engine gains
    a synchronous clipboard callback in a future release.
    """

    def __init__(self, terminal: Terminal, engine: EngineInfo):
        """Select image ownership once, independently of host negotiations."""
        self._write = terminal.feed_raw
        self._routes = select_routes(engine)
        self._images = (
            _iterm_adapter(terminal) if self._routes.iterm == "adapter" else None
        )
        self._clipboard = None
        self._started = False
        self._closed = False
        self._stream = ProtocolStream(self._text, self._control, cancel=self.cancel)

    @property
    def routes(self) -> InputRoutes:
        """Read the immutable session selection."""
        return self._routes

    @property
    def image_errors(self) -> tuple[str, ...]:
        """Return diagnostics without exposing an adapter instance to the UI."""
        return tuple(self._images.errors) if self._images is not None else ()

    @property
    def clipboard(self):
        """Read the separately owned clipboard policy broker."""
        return self._clipboard

    @clipboard.setter
    def clipboard(self, broker):
        if broker is self._clipboard:
            return
        if self._started or self._closed:
            raise RuntimeError("Clipboard must be attached before terminal input")
        self._clipboard = broker

    def feed(self, data: bytes) -> None:
        """Preserve the exact wire order across fragmented PTY reads."""
        if self._closed:
            raise RuntimeError("Terminal input is closed")
        self._started = True
        self._stream.feed(data)

    def _text(self, data):
        if self._clipboard is None or not self._clipboard.capture(data):
            self._write(data)

    def _control(self, control):
        clipboard = self._clipboard
        if clipboard is not None:
            if control.kind == "osc" and clipboard.child_osc(control.body):
                return True
            if clipboard.capture(control.raw):
                return True
            if control.kind == "csi" and clipboard.child_csi(control.body):
                return True
        if control.kind == "esc" and control.body == b"c":
            self.cancel_images()
            if clipboard is not None:
                clipboard.reset()
        if control.kind == "osc" and control.body.startswith(ITERM_IMAGES):
            if self._routes.iterm == "native":
                return False
            if self._images is not None:
                self._images.handle(control.body)
            # Unsupported input is consumed without creating files or leaking
            # payload. Adapter failures never fall through to the native parser.
            return True
        return False

    def cancel_images(self) -> None:
        """Discard incomplete shim transfers when presentation is disabled."""
        if self._images is not None:
            self._images.cancel()

    def cancel(self) -> None:
        """Keep existing capture cancellation semantics at framing boundaries."""
        self.cancel_images()
        if self._clipboard is not None:
            self._clipboard.cancel()

    def close(self) -> None:
        """Discard incomplete framing once before the native owner is freed."""
        if not self._closed:
            self._closed = True
            self._stream.close()
