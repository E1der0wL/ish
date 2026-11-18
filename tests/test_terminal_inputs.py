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

    def test_verified_native_sixel_retires_adapter_without_losing_advertisement(self):
        """A native replacement receives exact wire and retains enabled DA1 facts."""
        sink = Sink()
        capability = []
        sink.set_sixel_supported = capability.append
        wire = b'\x1bP0;1q"1;1;1;1#1;2;100;0;0@\x1b\\'
        with patch("ish.terminal.inputs._sixel_adapter") as adapter:
            pipeline = InputPipeline(
                sink, EngineInfo("test", "test", 1, ("kitty", "iterm", "sixel"))
            )
            pipeline.graphics_changed(True)
            pipeline.feed(wire)
            pipeline.graphics_changed(False)
            pipeline.close()
            adapter.assert_not_called()
        self.assertEqual(sink.data, wire)
        self.assertEqual(capability, [True, False])

    def test_cancelled_fragment_never_reaches_any_image_native_route(self):
        """Re-enabling graphics cannot complete an image started before disable."""
        images = (
            b'\x1bP0;1q"1;1;1;1#1;2;100;0;0@\x1b\\',
            iterm(png(1, 1)),
            b"\x1b_Ga=T,f=32,s=1,v=1;" + base64.b64encode(RED) + b"\x1b\\",
            b"\x1b_Ga=T,f=32,s=1,v=1;" + base64.b64encode(RED) + b"\x9c",
        )
        for wire in images:
            for split in range(1, len(wire)):
                with self.subTest(wire=wire[:12], split=split):
                    sink = Sink()
                    pipeline = InputPipeline(
                        sink, EngineInfo("test", "test", 1, ("kitty", "iterm", "sixel"))
                    )
                    pipeline.feed(wire[:split])
                    pipeline.cancel_images()
                    pipeline.cancel_images()
                    pipeline.feed(wire[split:] + b"Z")
                    pipeline.feed(wire)
                    pipeline.close()
                    self.assertEqual(sink.data, b"Z" + wire)

    def test_image_cancel_preserves_unfinished_non_image_records(self):
        """Graphics policy cannot swallow clipboard, title or terminal queries."""
        records = (
            b"\x1b]2;title\x07",
            b"\x1b]52;c;SGVsbG8=\x07",
            b"\x1bP$qm\x1b\\",
            b"\x1b[6n",
            b"\x1b_plain\x1b\\",
        )
        for wire in records:
            for split in range(1, len(wire)):
                with self.subTest(wire=wire, split=split):
                    sink = Sink()
                    pipeline = InputPipeline(
                        sink, EngineInfo("test", "test", 1, ("kitty", "iterm", "sixel"))
                    )
                    pipeline.feed(wire[:split])
                    pipeline.cancel_images()
                    pipeline.feed(wire[split:])
                    pipeline.close()
                    self.assertEqual(sink.data, wire)

    def test_replacement_record_is_not_cancelled_with_previous_image(self):
        """CAN and replacement introducers cannot transfer an old cancellation."""
        image = b"\x1b_Ga=T,f=32,s=1,v=1;" + base64.b64encode(RED) + b"\x1b\\"
        for boundary in (b"\x18", b"\x1a", b"\x1b[0m"):
            with self.subTest(boundary=boundary):
                sink = Sink()
                pipeline = InputPipeline(
                    sink, EngineInfo("test", "test", 1, ("kitty", "iterm", "sixel"))
                )
                pipeline.feed(b"\x1b_Gincomplete")
                pipeline.cancel_images()
                pipeline.feed(boundary + image)
                pipeline.close()
                self.assertTrue(sink.data.endswith(image))


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
            ("native", "adapter", "adapter"),
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

    def test_disabled_fragment_cannot_reappear_when_each_image_input_resumes(self):
        """Real SIXEL/iTerm/Kitty paths reject old records and accept fresh ones."""
        images = (
            b'\x1bP0;1q"1;1;1;1#1;2;100;0;0@\x1b\\',
            iterm(png(1, 1)),
            b"\x1b_Ga=T,f=32,s=1,v=1,q=2;" + base64.b64encode(RED) + b"\x1b\\",
            b"\x1b_Ga=T,f=32,s=1,v=1,q=2;" + base64.b64encode(RED) + b"\x9c",
        )
        for wire in images:
            for split in (1, 2, len(wire) // 2, len(wire) - 2, len(wire) - 1):
                with (
                    self.subTest(wire=wire[:12], split=split),
                    Terminal(8, 4, lambda _: None) as term,
                ):
                    term.resize(8, 4, 1, 1)
                    term.set_graphics(True)
                    term.feed(wire[:split])
                    term.set_graphics(False)
                    term.set_graphics(True)
                    term.feed(wire[split:] + b"Z")
                    self.assertEqual(term.frame().images, ())
                    self.assertEqual(term.frame().cells[0][0].text, "Z")
                    term.feed(wire)
                    placements = term.frame().images
                    self.assertEqual(len(placements), 1)
                    self.assertEqual(term.image(placements[0].image_id).rgba, RED)

    def test_graphics_cancel_preserves_partial_terminal_query_reply(self):
        """An unrelated DECRQSS request still reaches its native owner."""
        self.terminal.feed(b"\x1bP$qm")
        self.terminal.set_graphics(False)
        self.terminal.set_graphics(True)
        self.terminal.feed(b"\x1b\\")
        self.assertEqual(b"".join(self.replies), b"\x1bP1$r0m\x1b\\")

    def test_graphics_cancel_keeps_clipboard_capture_bytes(self):
        """Graphics changes cannot discard image-looking opaque copy contents."""
        emitted = []
        broker = ClipboardBroker(
            reply=self.replies.append,
            emit=emitted.append,
            profile=lambda: HostProfile(),
        )
        self.addCleanup(broker.close)
        self.terminal.clipboard = broker
        wire = iterm(png(1, 1))
        self.terminal.feed(b"\x1b]1337;CopyToClipboard=\x07" + wire[:-1])
        self.terminal.set_graphics(False)
        self.terminal.set_graphics(True)
        self.terminal.feed(wire[-1:] + b"\x1b]1337;EndCopy\x07")
        self.assertEqual(
            emitted, ["\x1b]52;c;" + base64.b64encode(wire).decode() + "\x1b\\"]
        )
        self.assertEqual(self.terminal.frame().images, ())

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
