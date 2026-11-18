"""Check child wire framing against independent literal byte records."""

import base64
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
        """Ground is UTF-8-only, including standalone C1 bytes, like the native engine."""
        text = "ÝÜЯП".encode()
        data = text + b"\x9d2;" + text + b"\x9c" + text
        for split in range(len(data) + 1):
            _, events, wire, _ = self.record(data, splits=(split,))
            self.assertEqual(wire, data)
            controls = [event for event in events if event[0] != "text"]
            self.assertEqual(controls, [])

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
        """Native cancellation sees buffered headers; adapters never receive them."""
        _, events, wire, cancelled = self.record(
            b"\x1b]52;c;secret\x18ok\x1bPqbad\x1b[2Jnext"
        )
        self.assertEqual(wire, b"\x1b]52;c;secret\x18ok\x1bPqbad\x1b\x18\x1b[2Jnext")
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

    def test_c1_restart_retains_exact_original_record_for_adapter(self):
        """Native-equivalent re-entry never changes the codec's original wire."""
        records = []
        stream = ProtocolStream(
            lambda _: None, lambda control: records.append(control) or True
        )
        stream.feed(b"\x1b[1\x9d9911;HIT\x07")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].raw, b"\x9d9911;HIT\x07")
        self.assertEqual(records[0].body, b"9911;HIT")

    def test_limit_rejects_record_but_keeps_independent_c0_and_counts_once(self):
        """Oversize diagnostics contain no payload and recovery cannot emit its tail."""
        output = []
        stream = ProtocolStream(output.append, lambda _: False, max_string_bytes=12)
        stream.feed(b"\x1b\x07]9911;" + b"secret" * 20 + b"\x1b\\Z")
        self.assertEqual(b"".join(output), b"\x07Z")
        self.assertEqual(stream.dropped, 1)
        stream.feed(b"\x1b_" + b"\x9f" * 100 + b"\x1b\\Q")
        self.assertEqual(stream.dropped, 2)
        self.assertTrue(b"".join(output).endswith(b"ZQ"))

    def test_record_identity_spans_fragments_and_completion_callbacks(self):
        """One record keeps its identity while capture and handle see completion."""
        seen = []
        stream = ProtocolStream(
            lambda _: None,
            lambda control: (
                seen.append(("handle", stream.record_id, control.raw)) or True
            ),
            capture=lambda control: (
                seen.append(("capture", stream.record_id, control.raw)) or False
            ),
        )
        wire = b"\x1b]2;title\x1b\\"
        self.assertIsNone(stream.record_id)
        stream.feed(wire[:1])
        identity = stream.record_id
        self.assertIsNotNone(identity)
        for byte in wire[1:-1]:
            stream.feed(bytes((byte,)))
            self.assertEqual(stream.record_id, identity)
        stream.feed(wire[-1:])
        self.assertEqual(
            seen, [("capture", identity, wire), ("handle", identity, wire)]
        )
        self.assertIsNone(stream.record_id)
        with self.assertRaises(AttributeError):
            stream.record_id = identity

    def test_abort_and_replacement_get_fresh_record_identities(self):
        """A previous record cannot lend its cancellation to a new introducer."""
        seen = []
        stream = ProtocolStream(
            lambda _: None, lambda _: seen.append(stream.record_id) or True
        )
        stream.feed(b"\x1b]2;partial")
        first = stream.record_id
        stream.feed(b"\x18")
        self.assertIsNone(stream.record_id)
        stream.feed(b"\x1b]2;another")
        second = stream.record_id
        stream.feed(b"\x1b[")
        third = stream.record_id
        stream.feed(b"1\x9d")
        fourth = stream.record_id
        stream.feed(b"2;title\x07")
        self.assertEqual(len({first, second, third, fourth}), 4)
        self.assertNotIn(None, (first, second, third, fourth))
        self.assertEqual(seen, [fourth])
        self.assertIsNone(stream.record_id)

    def test_failed_completion_releases_identity_without_replaying_record(self):
        """An adapter exception leaves no unfinished record available to cancel."""
        seen = []

        def fail(control):
            seen.append((stream.record_id, control.raw))
            raise RuntimeError("adapter failure")

        stream = ProtocolStream(lambda _: None, fail)
        stream.feed(b"\x1b]2;title")
        identity = stream.record_id
        with self.assertRaisesRegex(RuntimeError, "adapter failure"):
            stream.feed(b"\x07")
        self.assertEqual(seen, [(identity, b"\x1b]2;title\x07")])
        self.assertTrue(stream.complete)
        self.assertIsNone(stream.record_id)
        stream.close()
        self.assertEqual(len(seen), 1)

    def test_apc_pm_sos_c1_st_complete_at_every_fragment_boundary(self):
        """Native string terminators reach the same adapter boundary as ESC-ST."""
        for intro, kind in ((b"\x1b_", "apc"), (b"\x1b^", "pm"), (b"\x1bX", "sos")):
            for ending in (b"\x1b\\", b"\x9c"):
                wire = intro + b"payload" + ending
                for split in range(len(wire) + 1):
                    with self.subTest(kind=kind, ending=ending, split=split):
                        stream, events, forwarded, cancelled = self.record(
                            wire, splits=(split,)
                        )
                        self.assertEqual(
                            [event for event in events if event[0] != "text"],
                            [(kind, b"payload", wire)],
                        )
                        self.assertEqual(forwarded, wire)
                        self.assertEqual(cancelled, [])
                        self.assertTrue(stream.complete)
                        self.assertIsNone(stream.record_id)

    def test_apc_replacement_keeps_distinct_identity_and_original_capture_bytes(self):
        """A shared ESC boundary completes the old string without duplicating copy bytes."""
        seen = []
        stream = ProtocolStream(
            lambda _: self.fail("Captured controls must not reach the native writer"),
            lambda _: self.fail("Capture must own the controls"),
            capture=lambda control: (
                seen.append((stream.record_id, control.raw, control.body)) or True
            ),
        )
        stream.feed(b"\x1b_Gpayload")
        old = stream.record_id
        stream.feed(b"\x1b[")
        new = stream.record_id
        self.assertNotEqual(old, new)
        stream.feed(b"0m")
        self.assertEqual(
            seen, [(old, b"\x1b_Gpayload", b"Gpayload"), (new, b"\x1b[0m", b"0m")]
        )
        self.assertEqual(b"".join(raw for _, raw, _ in seen), b"\x1b_Gpayload\x1b[0m")


class NativeFramingTests(unittest.TestCase):
    """Assert native observable results and adapter boundaries at every read split."""

    @staticmethod
    def lines(terminal):
        """Read native text without deriving expected results from its codec."""
        return [
            "".join(value for _, value in row).rstrip()
            for row in terminal.frame().lines
        ]

    def test_cancellation_keeps_native_c0_effects_and_dcs_unhook_replies(self):
        """Every split preserves literal line positions, bells and DECRQSS replies."""
        from ish.terminal.vt import Terminal

        cases = (
            (b"A\x1b[\n12\x18Z", ["A", " Z", "", ""], b"", []),
            (b"A\x1b[\t12\x1b[31mZ", ["A       Z", "", "", ""], b"", []),
            (b"A\x1b[\x0712\x1aZ", ["AZ", "", "", ""], b"", ["bell"]),
            (b"\x1bP$qm\x18Z", ["Z", "", "", ""], b"\x1bP1$r0m\x1b\\", []),
            (b"\x1bP$qm\x1aZ", ["Z", "", "", ""], b"\x1bP1$r0m\x1b\\", []),
            (b"\x1bP$qm\x1b[31mZ", ["Z", "", "", ""], b"\x1bP1$r0m\x1b\\", []),
        )
        for wire, lines, reply, effects in cases:
            for split in range(len(wire) + 1):
                replies = []
                with (
                    self.subTest(wire=wire, split=split),
                    Terminal(20, 4, replies.append) as term,
                ):
                    term.feed(wire[:split])
                    term.feed(wire[split:])
                    self.assertEqual(self.lines(term), lines)
                    self.assertEqual(b"".join(replies), reply)
                    self.assertEqual(
                        [effect.kind for effect in term.drain_events()], effects
                    )

    def test_kitty_terminators_keep_completion_and_cancel_distinct_at_every_split(self):
        """Native ST/ESC completion and CAN/SUB cancellation keep distinct outcomes."""
        from ish.terminal.vt import Terminal

        red = b"\xff\0\0\xff"
        body = b"\x1b_Ga=T,f=32,s=1,v=1,q=2;" + base64.b64encode(red)
        endings = (
            (b"\x1b\\", True),
            (b"\x9c", True),
            (b"\x18", False),
            (b"\x1a", False),
            (b"\x1b[0m", True),
            (b"\x1b\x18", True),
            (b"\x1b\x1a", True),
            (b"\x1b\n[0m", True),
        )
        for ending, completes in endings:
            wire = body + ending + b"Z"
            for cancelled in (False, True):
                splits = range(1, len(wire) + 1) if cancelled else range(len(wire) + 1)
                for split in splits:
                    with (
                        self.subTest(ending=ending, cancelled=cancelled, split=split),
                        Terminal(8, 4, lambda _: None) as term,
                    ):
                        term.resize(8, 4, 1, 1)
                        term.set_graphics(True)
                        term.feed(wire[:split])
                        if cancelled:
                            term.set_graphics(False)
                            term.set_graphics(True)
                        term.feed(wire[split:])
                        images = term.frame().images
                        self.assertEqual(len(images), int(completes and not cancelled))
                        if images:
                            self.assertEqual(term.image(images[0].image_id).rgba, red)
                        self.assertIn("Z", "".join(self.lines(term)))
                        if b"\n" in ending:
                            self.assertEqual(term.frame().cursor_y, 1)

    def test_c1_st_preserves_opaque_clipboard_string_records_at_every_split(self):
        """Native string completion must preserve an active copy's exact original bytes."""
        from ish.terminal.clipboard import ClipboardBroker
        from ish.terminal.host import HostProfile
        from ish.terminal.vt import Terminal

        for intro in (b"\x1b_", b"\x1b^", b"\x1bX"):
            for ending in (b"\x9c", b"\x1b\\", b"\x1b[0m"):
                body = intro + b"Gimage-looking-data" + ending
                for split in range(len(body) + 1):
                    writes = []
                    with (
                        self.subTest(intro=intro, ending=ending, split=split),
                        Terminal(8, 4, lambda _: None) as term,
                    ):
                        broker = ClipboardBroker(
                            reply=lambda _: None,
                            emit=writes.append,
                            profile=lambda: HostProfile(),
                        )
                        self.addCleanup(broker.close)
                        term.clipboard = broker
                        term.feed(b"\x1b]1337;CopyToClipboard=\x07" + body[:split])
                        term.feed(body[split:] + b"\x1b]1337;EndCopy\x07Z")
                        self.assertEqual(
                            writes,
                            ["\x1b]52;c;" + base64.b64encode(body).decode() + "\x1b\\"],
                        )
                        self.assertEqual(self.lines(term), ["Z", "", "", ""])
                        self.assertEqual(term.frame().images, ())

    def test_independent_c0_executes_before_an_unfinished_control_completes(self):
        """A client need not finish or cancel CSI before LF and BEL take effect."""
        from ish.terminal.vt import Terminal

        with Terminal(20, 4, lambda _: None) as term:
            term.feed(b"A\x1b[\n\x07")
            self.assertEqual((term.frame().cursor_x, term.frame().cursor_y), (1, 1))
            self.assertEqual([effect.kind for effect in term.drain_events()], ["bell"])
            term.feed(b"12\x18Z")
            self.assertEqual(self.lines(term), ["A", " Z", "", ""])
            self.assertEqual(term.drain_events(), ())

    def test_c1_payload_is_never_promoted_to_an_osc_extension(self):
        """Only actual OSC boundaries may call a registered converter."""
        from ish.terminal.extensions import ProtocolResult
        from ish.terminal.vt import Terminal

        cases = (
            (b"\x1bPqBODY\x9c\x9d9911;HIT\x07\x1b\\Z", [], ["Z", "", "", ""]),
            (b"\x1bP:BODY\x9c\x9d9911;HIT\x07\x1b\\Z", [], ["Z", "", "", ""]),
            (b"\x9d9911;HIT\x07Z", [], ["\ufffd9911;HITZ", "", "", ""]),
            (b"\x1bP\x9d9911;HIT\x07Z", [b"9911;HIT"], ["XZ", "", "", ""]),
            (b"\x1b_G\x1b\x9fabc\x1b\\Z", [], ["Z", "", "", ""]),
            (b"A\x1b\n]9911;HIT\x07Z", [b"9911;HIT"], ["A", " XZ", "", ""]),
        )
        for wire, expected, lines in cases:
            for split in range(len(wire) + 1):
                seen = []

                class Adapter:
                    def handle(self, control, seen=seen):
                        seen.append(control.body)
                        return ProtocolResult(native=b"X")

                    def cancel(self):
                        pass

                    def close(self):
                        pass

                with (
                    self.subTest(wire=wire, split=split),
                    Terminal(20, 4, lambda _: None) as term,
                ):
                    term.register_protocol(
                        "probe", kind="osc", prefixes=(b"9911;",), factory=Adapter
                    )
                    term.feed(wire[:split])
                    term.feed(wire[split:])
                    self.assertEqual(seen, expected)
                    self.assertEqual(self.lines(term), lines)

    def test_consumed_osc_interrupts_native_utf8_prefix_at_its_original_byte(self):
        """A hidden control cannot join UTF-8 bytes that the child kept separate."""
        from ish.terminal.extensions import ProtocolResult
        from ish.terminal.vt import Terminal

        class Adapter:
            def handle(self, control):
                return ProtocolResult()

            def cancel(self):
                pass

            def close(self):
                pass

        for prefix, tail in (
            (b"\xc2", b"\x80"),
            (b"\xe7\x95", b"\x8c"),
            (b"\xf0\x9f", b"\x99\x82"),
        ):
            wire = prefix + b"\x1b]9911;HIT\x07" + tail
            for split in range(len(wire) + 1):
                with (
                    self.subTest(prefix=prefix, split=split),
                    Terminal(20, 4, lambda _: None) as term,
                ):
                    term.register_protocol(
                        "probe", kind="osc", prefixes=(b"9911;",), factory=Adapter
                    )
                    term.feed(wire[:split])
                    term.feed(wire[split:])
                    self.assertEqual(self.lines(term)[0], "\ufffd" * (len(tail) + 1))
            with Terminal(20, 4, lambda _: None) as term:
                term.feed(prefix)
                self.assertEqual(self.lines(term)[0], "")
                term.feed(b"")
                term.feed(b"\x1b")
                self.assertEqual(self.lines(term)[0], "\ufffd")

    def test_clipboard_capture_keeps_original_controls_and_cancel_discards_transfer(
        self,
    ):
        """Opaque copy data keeps C0 byte order and never executes as native output."""
        from ish.terminal.clipboard import ClipboardBroker
        from ish.terminal.host import HostProfile
        from ish.terminal.vt import Terminal

        start = b"\x1b]1337;CopyToClipboard=\x07"
        finish = b"\x1b]1337;EndCopy\x07Z"
        body = b"left\x1b\n[31mright"
        for cancelled in (False, True):
            wire = start + body + (b"\x1b[12\x18hidden" if cancelled else b"") + finish
            for split in range(len(wire) + 1):
                writes = []
                with (
                    self.subTest(cancelled=cancelled, split=split),
                    Terminal(20, 4, lambda _: None) as term,
                ):
                    broker = ClipboardBroker(
                        reply=lambda _: None,
                        emit=writes.append,
                        profile=lambda: HostProfile(),
                    )
                    term.clipboard = broker
                    term.feed(wire[:split])
                    term.feed(wire[split:])
                    self.assertEqual(self.lines(term), ["Z", "", "", ""])
                    self.assertEqual(
                        writes,
                        []
                        if cancelled
                        else [
                            "\x1b]52;c;" + base64.b64encode(body).decode() + "\x1b\\"
                        ],
                    )
                    broker.close()


if __name__ == "__main__":
    unittest.main()
