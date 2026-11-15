"""Verify exclusive input ownership separately from live engine support.

Simulated native iTerm routes test replacement mechanics only. Actual image,
clipboard and host-disable tests below use the pinned native engine.
"""

import base64
import unittest
from unittest.mock import patch

from ish.terminal.clipboard import ClipboardBroker
from ish.terminal.engine import EngineInfo
from ish.terminal.host import HostProfile
from ish.terminal.inputs import InputPipeline
from ish.terminal.vt import Terminal
from test_terminal_image_protocol import RED, iterm, png


class Sink:
    """Observe native wire bytes without pretending to implement an engine."""

    def __init__(self):
        """Start with an empty observed wire stream."""
        self.data = bytearray()

    def feed_raw(self, data):
        """Collect exactly what the input boundary sends to its native writer."""
        self.data.extend(data)


class RouteTests(unittest.TestCase):
    """Replacement selection must not create or replay an unused adapter."""

    def test_native_replacement_bypasses_adapter_with_exact_fragmented_wire(self):
        """A future verified native route receives every image exactly once."""
        sink = Sink()
        engine = EngineInfo("test", "test", 1, ("kitty", "iterm"))
        wire = b"A" + iterm(png(1, 1)) + b"B\x1b[31mC"
        with patch("ish.terminal.inputs._iterm_adapter") as adapter:
            pipeline = InputPipeline(sink, engine)
            for byte in wire:
                pipeline.feed(bytes((byte,)))
            pipeline.close()
            adapter.assert_not_called()
        self.assertEqual(sink.data, wire)
        self.assertEqual(pipeline.routes.iterm, "native")
        self.assertEqual(pipeline.image_errors, ())

    def test_unsupported_image_is_consumed_without_adapter_or_payload_leak(self):
        """Unknown capabilities neither import a shim nor expose its payload."""
        sink = Sink()
        with patch("ish.terminal.inputs._iterm_adapter") as adapter:
            pipeline = InputPipeline(sink, EngineInfo("test", "test", 1, ()))
            pipeline.feed(b"A" + iterm(png(1, 1)) + b"B")
            pipeline.close()
            adapter.assert_not_called()
        self.assertEqual(sink.data, b"AB")
        self.assertEqual(pipeline.routes.iterm, "unsupported")

    def test_close_discards_partial_native_control_and_rejects_late_input(self):
        """Closing cannot flush unfinished encoded payload into the native grid."""
        sink = Sink()
        pipeline = InputPipeline(sink, EngineInfo("test", "test", 1, ("iterm",)))
        pipeline.feed(b"A\x1b]1337;File=inline=1:unfinished")
        pipeline.close()
        pipeline.close()
        self.assertEqual(sink.data, b"A")
        with self.assertRaises(RuntimeError):
            pipeline.feed(b"late\x07")


class LiveInputTests(unittest.TestCase):
    """Exercise the selected adapter and native storage through Terminal.feed."""

    def setUp(self):
        """Use measured cell geometry and the real reviewed native library."""
        self.replies = []
        self.terminal = Terminal(8, 4, self.replies.append)
        self.addCleanup(self.terminal.close)
        self.terminal.resize(8, 4, 2, 3)
        self.terminal.set_graphics(True)

    def test_current_routes_are_independent_of_host_image_disable(self):
        """Losing output support cancels a transfer without switching decoders."""
        routes = self.terminal.input_routes
        self.assertEqual(
            (routes.kitty, routes.iterm, routes.sixel),
            ("native", "adapter", "unsupported"),
        )
        self.terminal.feed(b"\x1b]1337;MultipartFile=inline=1\x07")
        self.terminal.feed(
            b"\x1b]1337;FilePart=" + base64.b64encode(png(2, 3)) + b"\x07"
        )
        self.terminal.set_graphics(False)
        self.terminal.set_graphics(True)
        self.terminal.feed(b"\x1b]1337;FileEnd\x07OK")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.terminal.frame().cells[0][0].text, "O")
        self.assertIs(self.terminal.input_routes, routes)
        self.terminal.feed(iterm(png(2, 3)))
        placement = self.terminal.frame().images[0]
        self.assertEqual(self.terminal.image(placement.image_id).rgba, RED * 6)

    def test_clipboard_capture_owns_embedded_image_controls(self):
        """A clipboard payload containing image escapes cannot paint the grid."""
        emitted = []
        broker = ClipboardBroker(
            reply=self.replies.append,
            emit=emitted.append,
            profile=lambda: HostProfile(),
        )
        self.addCleanup(broker.close)
        self.terminal.clipboard = broker
        image = iterm(png(2, 3))
        self.terminal.feed(b"\x1b]1337;CopyToClipboard=\x07" + image)
        self.assertEqual(self.terminal.frame().images, ())
        self.terminal.feed(b"\x1b]1337;EndCopy\x07OK")
        self.assertEqual(self.terminal.frame().cells[0][0].text, "O")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(
            emitted, ["\x1b]52;c;" + base64.b64encode(image).decode() + "\x1b\\"]
        )
        self.assertEqual(self.replies, [])

    def test_clipboard_owner_cannot_change_mid_capture(self):
        """Reject replacing policy while bytes belong to its current capture."""
        broker = ClipboardBroker(
            reply=self.replies.append,
            emit=lambda _: None,
            profile=lambda: HostProfile(),
        )
        self.addCleanup(broker.close)
        self.terminal.clipboard = broker
        self.terminal.feed(b"\x1b]1337;CopyToClipboard=\x07secret")
        with self.assertRaises(RuntimeError):
            self.terminal.clipboard = None
        self.terminal.feed(b"\x18discard\x1b]1337;EndCopy\x07OK")
        self.assertEqual(
            "".join(c.text for c in self.terminal.frame().cells[0])[:2], "OK"
        )
        self.assertEqual(self.replies, [])


if __name__ == "__main__":
    unittest.main()
