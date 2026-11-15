"""Check direct-output wire preservation and private prompt record boundaries."""

import unittest

from ish.stream.protocol import Stream

NONCE = "test-session-42"
PRIMARY = b"\x1b]777;ish;test-session-42;1;P\x07"
HIDE = b"\x1b]777;ish;test-session-42;2;H\x1b\\"


class Recorder:
    """Record externally observable bytes, UI events and unsafe transitions."""

    def __init__(self, *, track_rewind=False, track_clear=False):
        """Construct a stream without borrowing any implementation validators."""
        self.timeline = []
        self.events = []
        self.chunks = []
        self.stream = Stream(
            NONCE,
            self.write,
            self.event,
            self.unsafe,
            self.rewind if track_rewind else None,
            self.cleared if track_clear else None,
        )

    def write(self, data):
        """Collect actual forwarded bytes in their original order."""
        self.chunks.append(data)
        self.timeline.append(("bytes", data))

    def event(self, kind, serial):
        """Collect prompt notifications in order relative to visible output."""
        self.events.append((kind, serial))
        self.timeline.append(("event", (kind, serial)))

    def unsafe(self):
        """Record when the owner must stop decorating the current prompt."""
        self.timeline.append(("unsafe", None))

    def rewind(self):
        """Record controls which invalidate a prior blank-area observation."""
        self.timeline.append(("rewind", None))

    def cleared(self):
        """Record completed host erasure, separately from primary eligibility."""
        self.timeline.append(("cleared", None))

    @property
    def data(self):
        """Return exactly the bytes offered to the outside terminal."""
        return b"".join(self.chunks)


class StreamTests(unittest.TestCase):
    """Use independent literal byte expectations for the public stream API."""

    def test_primary_and_hide_are_ordered_with_visible_bytes(self):
        """Private records replace only themselves, at their exact wire position."""
        recorder = Recorder()
        recorder.stream.feed(b"before" + PRIMARY + b"prompt" + HIDE + b"command")
        self.assertEqual(recorder.data, b"beforepromptcommand")
        self.assertEqual(recorder.events, [("P", 1), ("H", 2)])
        self.assertEqual(
            recorder.timeline,
            [
                ("bytes", b"before"),
                ("event", ("P", 1)),
                ("bytes", b"prompt"),
                ("event", ("H", 2)),
                ("bytes", b"command"),
            ],
        )

    def test_blank_evidence_follows_complete_host_erase(self):
        """Fragmented ED0/ED2 never publish evidence before their final byte."""
        for wire in (b"\x1b[J", b"\x1b[0J", b"\x1b[2J"):
            with self.subTest(wire=wire):
                recorder = Recorder(track_clear=True)
                recorder.stream.feed(wire[:-1])
                self.assertEqual(recorder.timeline, [])
                recorder.stream.feed(wire[-1:])
                self.assertEqual(
                    recorder.timeline,
                    [("unsafe", None), ("bytes", wire), ("cleared", None)],
                )
                self.assertEqual(recorder.events, [])

    def test_selective_or_upper_erase_does_not_claim_blank_rows(self):
        """ED1, ED3 and protected-cell erase leave no lower-screen guarantee."""
        for wire in (b"\x1b[1J", b"\x1b[3J", b"\x1b[?J", b"\x1b[?0J"):
            with self.subTest(wire=wire):
                recorder = Recorder(track_clear=True)
                recorder.stream.feed(wire)
                self.assertEqual(recorder.data, wire)
                self.assertNotIn(("cleared", None), recorder.timeline)

    def test_input_modes_preserve_existing_display_evidence(self):
        """Cursor/keypad encoding modes pass unchanged without erasing panels."""
        recorder = Recorder(track_clear=True, track_rewind=True)
        wire = b"\x1b[?1h\x1b=\x1b[?1l\x1b>"
        recorder.stream.feed(wire)
        self.assertEqual(recorder.data, wire)
        self.assertTrue(all(kind == "bytes" for kind, _ in recorder.timeline))

    def test_event_callback_runs_after_record_state_is_cleared(self):
        """A callback may inspect the boundary without seeing a pending marker."""
        seen = []
        output = []

        def observe(kind, serial):
            seen.append((kind, serial, stream.boundary))
            stream.finish()

        stream = Stream(NONCE, output.append, observe, lambda: None)
        stream.feed(PRIMARY + b"prompt")
        self.assertEqual(seen, [("P", 1, True)])
        self.assertEqual(output, [b"prompt"])

    def test_rewind_controls_notify_before_their_unchanged_wire_bytes(self):
        """Backward cursor, screen edits and saved cursors invalidate blank evidence."""
        for control in (
            b"\x1b[A",
            b"\x1b[2F",
            b"\x1b[1;1H",
            b"\x1b[2;3f",
            b"\x1b[3d",
            b"\x1b[2J",
            b"\x1b[r",
            b"\x1b[S",
            b"\x1b[T",
            b"\x1b[2L",
            b"\x1b[3M",
            b"\x1b[s",
            b"\x1b[u",
            b"\x1b7",
            b"\x1b8",
            b"\x1bM",
            b"\x8d",
        ):
            with self.subTest(control=control):
                recorder = Recorder(track_rewind=True)
                for byte in control:
                    recorder.stream.feed(bytes((byte,)))
                self.assertEqual(recorder.data, control)
                self.assertEqual(recorder.timeline[0], ("rewind", None))
                self.assertEqual(recorder.timeline.count(("rewind", None)), 1)

    def test_forward_text_and_horizontal_controls_do_not_invalidate_blank_evidence(
        self,
    ):
        """Append-only output is distinguishable without modeling terminal cells."""
        wire = b"text\r\n\b\t\x1b[B\x1b[2E\x1b[3C\x1b[4D\x1b[6G\x1b[32m\x1b[0m"
        recorder = Recorder(track_rewind=True)
        recorder.stream.feed(wire)
        self.assertEqual(recorder.data, wire)
        self.assertNotIn(("rewind", None), recorder.timeline)
        self.assertNotIn(("unsafe", None), recorder.timeline)

    def test_utf8_continuation_is_not_a_raw_reverse_index(self):
        """A Unicode continuation byte cannot be mistaken for a C1 cursor command."""
        wire = "Í".encode()
        recorder = Recorder(track_rewind=True)
        for byte in wire:
            recorder.stream.feed(bytes((byte,)))
        self.assertEqual(recorder.data, wire)
        self.assertNotIn(("rewind", None), recorder.timeline)
        self.assertNotIn(("unsafe", None), recorder.timeline)

    def test_all_record_split_positions_preserve_events_and_output(self):
        """Read boundaries must not change private event recognition."""
        wire = b"hello" + PRIMARY + b"user> " + HIDE + b"done\r\n"
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                recorder = Recorder()
                recorder.stream.feed(wire[:split])
                recorder.stream.feed(wire[split:])
                self.assertEqual(recorder.data, b"hellouser> done\r\n")
                self.assertEqual(recorder.events, [("P", 1), ("H", 2)])
                self.assertTrue(recorder.stream.boundary)

    def test_stale_duplicate_and_wrong_session_do_not_reenable_ui(self):
        """A hide event cannot be undone by an older or foreign primary."""
        foreign = b"\x1b]777;ish;different-session;99;P\x07"
        recorder = Recorder()
        recorder.stream.feed(PRIMARY + HIDE + PRIMARY + HIDE + foreign)
        self.assertEqual(recorder.events, [("P", 1), ("H", 2)])
        self.assertEqual(recorder.data, foreign)

    def test_malformed_private_lookalikes_are_forwarded(self):
        """Unknown fields and malformed serials are never UI notifications."""
        for body in (
            b"x;P",
            b"-1;P",
            b"3;X",
            b"4;P;extra",
            b";H",
            b"5;\xff",
            b"6;\xc3\xa9",
        ):
            with self.subTest(body=body):
                wire = b"\x1b]777;ish;test-session-42;" + body + b"\x07"
                recorder = Recorder()
                recorder.stream.feed(wire)
                self.assertEqual(recorder.data, wire)
                self.assertEqual(recorder.events, [])
                self.assertTrue(recorder.stream.boundary)

    def test_unknown_controls_and_images_are_byte_transparent(self):
        """Image, clipboard, title and ordinary VT sequences retain every byte."""
        controls = (
            b"\x1bPq#1;2;100;0;0~!40?\x1b\\",
            b"\x1b_Ga=T,f=24,s=1,v=1;AAAA\x1b\\",
            b"\x1b]1337;File=inline=1:YWJj\x07",
            b"\x1b]52;c;YWJj\x1b\\",
            b"\x1b]0;title\x07",
            b"\x1b^private string\x1b\\",
            b"\x1bXignored string\x1b\\",
            b"\x1b[38;2;1;2;3mcolor\x1b[0m",
            b"\x1b[?1049h\x1b[2J\x1b[?1049l",
        )
        for wire in controls:
            with self.subTest(wire=wire):
                recorder = Recorder()
                for byte in wire:
                    recorder.stream.feed(bytes((byte,)))
                self.assertEqual(recorder.data, wire)
                self.assertEqual(recorder.events, [])
                self.assertTrue(recorder.stream.boundary)

    def test_large_unfinished_image_streams_without_waiting_for_terminator(self):
        """Image payloads are forwarded incrementally, without a capture limit."""
        recorder = Recorder()
        prefix = b"\x1b_Ga=T,f=100;"
        chunk = b"A" * (64 * 1024)
        recorder.stream.feed(prefix)
        for count in range(4):
            recorder.stream.feed(chunk)
            self.assertEqual(recorder.data, prefix + chunk * (count + 1))
            self.assertFalse(recorder.stream.boundary)
        recorder.stream.feed(b"\x1b\\")
        self.assertEqual(recorder.data, prefix + chunk * 4 + b"\x1b\\")
        self.assertTrue(recorder.stream.boundary)

    def test_utf8_title_continuation_is_not_a_c1_string_terminator(self):
        """UTF-8 bytes resembling C1 controls cannot authorize a UI insertion."""
        recorder = Recorder()
        prefix = b"\x1b]0;title "
        recorder.stream.feed(prefix)
        for byte in "Ü界".encode():
            recorder.stream.feed(bytes((byte,)))
            self.assertFalse(recorder.stream.boundary)
        recorder.stream.feed(b"\x07")
        self.assertEqual(recorder.data, prefix + "Ü界".encode() + b"\x07")
        self.assertTrue(recorder.stream.boundary)

    def test_raw_c1_controls_are_distinct_from_utf8_text(self):
        """Raw C1 framing is preserved without mistaking Unicode for controls."""
        recorder = Recorder()
        text = "ÜÛÝÐ界".encode()
        for byte in text:
            recorder.stream.feed(bytes((byte,)))
        self.assertEqual(recorder.data, text)
        self.assertTrue(recorder.stream.boundary)
        self.assertEqual(
            recorder.timeline, [("bytes", bytes((byte,))) for byte in text]
        )
        recorder.stream.feed(b"\x9d0;title")
        self.assertFalse(recorder.stream.boundary)
        recorder.stream.feed(b"\x9c")
        self.assertTrue(recorder.stream.boundary)
        self.assertEqual(recorder.data, text + b"\x9d0;title\x9c")

    def test_cancelled_sequences_cannot_emit_old_events(self):
        """CAN and SUB end incomplete controls without granting prompt UI."""
        for cancelled in (
            b"\x1b]777;ish;test-session-42;9;P\x18",
            b"\x1b]0;title\x1a",
            b"\x1b[12;\x18",
            b"\x1bPqunfinished\x1a",
        ):
            with self.subTest(cancelled=cancelled):
                recorder = Recorder()
                recorder.stream.feed(cancelled)
                self.assertTrue(recorder.stream.boundary)
                recorder.stream.feed(PRIMARY)
                self.assertEqual(recorder.data, cancelled)
                self.assertEqual(recorder.events, [("P", 1)])

    def test_partial_utf8_and_controls_do_not_allow_ui_insertion(self):
        """Only complete character/control boundaries allow a panel frame."""
        for before, after in (
            (b"\xe7", b"\x95\x8c"),
            (b"\x1b", b"[0m"),
            (b"\x1b[12;", b"3H"),
            (PRIMARY[:-1], PRIMARY[-1:]),
            (b"\x1bPqpayload\x1b", b"\\"),
        ):
            with self.subTest(before=before):
                recorder = Recorder()
                recorder.stream.feed(before)
                self.assertFalse(recorder.stream.boundary)
                recorder.stream.feed(after)
                self.assertTrue(recorder.stream.boundary)

    def test_unfinished_prefixes_survive_eof_exactly(self):
        """A shell exiting mid-control must not lose its final output bytes."""
        for wire in (b"\x1b", b"\x1b[12;", PRIMARY[:-1], b"\x1b]52;c;abc"):
            with self.subTest(wire=wire):
                recorder = Recorder()
                recorder.stream.feed(wire)
                recorder.stream.finish()
                self.assertEqual(recorder.data, wire)
                self.assertEqual(recorder.events, [])

    def test_oversized_escape_prefix_streams_before_it_finishes(self):
        """An unbounded intermediate prefix must not accumulate in the relay."""
        recorder = Recorder()
        wire = b"\x1b" + b" " * 8192
        recorder.stream.feed(wire)
        self.assertGreater(len(recorder.data), 0)
        self.assertFalse(recorder.stream.boundary)
        recorder.stream.feed(b"F")
        recorder.stream.finish()
        self.assertEqual(recorder.data, wire + b"F")
        self.assertTrue(recorder.stream.boundary)

    def test_panel_destructive_controls_revoke_ui_before_forwarding(self):
        """Display erase and margin/buffer changes cannot leave decoration active."""
        for wire in (
            b"\x1b[J",
            b"\x1b[0J",
            b"\x1b[2J",
            b"\x1b[r",
            b"\x1b[?1049h",
            b"\x1bc",
        ):
            with self.subTest(wire=wire):
                recorder = Recorder()
                recorder.stream.feed(wire)
                self.assertEqual(recorder.data, wire)
                self.assertEqual(recorder.timeline[0], ("unsafe", None))


if __name__ == "__main__":
    unittest.main()
