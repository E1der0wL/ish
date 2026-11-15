"""Check child wire framing against independent literal byte records."""

import unittest

from ish.terminal.protocol import ProtocolStream


class ProtocolStreamTests(unittest.TestCase):
    """Framing must neither alter native traffic nor expose hidden payloads."""

    def record(self, data, *, splits=(), limit=4096):
        """Capture complete controls and text in the actual callback order."""
        events, forwarded, cancelled = [], [], []

        def handle(control):
            events.append((control.kind, control.body, control.raw))
            return False

        def write(text):
            forwarded.append(text)
            events.append(("text", text))

        stream = ProtocolStream(
            write, handle, cancel=lambda: cancelled.append(True), max_string_bytes=limit
        )
        offset = 0
        for end in (*splits, len(data)):
            stream.feed(data[offset:end])
            offset = end
        return stream, events, b"".join(forwarded), cancelled

    def test_every_fragment_boundary_preserves_native_wire(self):
        """All unhandled controls and Unicode bytes are forwarded exactly once."""
        data = (
            "앞\x1b[31m\x1b]2;ЯÜ글\x07뒤".encode()
            + b"\x1bP0;1q~\x1b\\\x1b_Gi=4;AAAA\x1b\\\x1b(B!"
        )
        expected = [
            ("csi", b"31m"),
            ("osc", "2;ЯÜ글".encode()),
            ("dcs", b"0;1q~"),
            ("apc", b"Gi=4;AAAA"),
            ("esc", b"(B"),
        ]
        for split in range(len(data) + 1):
            _, events, wire, _ = self.record(data, splits=(split,))
            self.assertEqual(wire, data)
            self.assertEqual([(e[0], e[1]) for e in events if e[0] != "text"], expected)

    def test_consumed_record_has_no_native_payload_and_keeps_context_order(self):
        """Previous text reaches the VT before an image adapter observes cursor state."""
        events = []
        stream = ProtocolStream(
            lambda text: events.append(text),
            lambda record: events.append(record.body) is None,
        )
        stream.feed(b"before\x1b]1337;File=inline=1:AAAA\x07after")
        self.assertEqual(events, [b"before", b"1337;File=inline=1:AAAA", b"after"])

    def test_utf8_continuations_are_not_c1_controls(self):
        """Raw C1 works while the same bytes inside encoded characters stay data."""
        text = "ÝÜЯП".encode()
        data = text + b"\x9d2;" + text + b"\x9c" + text
        for split in range(len(data) + 1):
            _, events, wire, _ = self.record(data, splits=(split,))
            self.assertEqual(wire, data)
            controls = [event for event in events if event[0] != "text"]
            self.assertEqual(
                controls, [("osc", b"2;" + text, b"\x9d2;" + text + b"\x9c")]
            )

    def test_oversized_payload_is_discarded_through_fragmented_terminator(self):
        """A bounded parser never reinterprets the end of a large image as text."""
        data = b"before\x1b]1337;" + b"x" * 100 + b"\x1b\\after"
        _, events, wire, cancelled = self.record(
            data, splits=tuple(range(len(data))), limit=24
        )
        self.assertEqual(wire, b"beforeafter")
        self.assertFalse([event for event in events if event[0] != "text"])
        self.assertEqual(cancelled, [True])

    def test_cancel_and_replacement_escape_recover_without_payload_leak(self):
        """CAN, SUB and a new escape abort only the unfinished control string."""
        _, events, wire, cancelled = self.record(
            b"\x1b]52;c;secret\x18ok\x1bPqbad\x1b[2Jnext"
        )
        self.assertEqual(wire, b"\x18ok\x1b[2Jnext")
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(
            [(e[0], e[1]) for e in events if e[0] != "text"], [("csi", b"2J")]
        )

    def test_close_discards_incomplete_control(self):
        """EOF does not print a truncated base64 clipboard or image body."""
        stream, _, wire, cancelled = self.record(b"visible\x1b]1337;File=secret")
        stream.close()
        self.assertEqual(wire, b"visible")
        self.assertEqual(cancelled, [True])


if __name__ == "__main__":
    unittest.main()
