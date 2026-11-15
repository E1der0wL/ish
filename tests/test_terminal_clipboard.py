"""Check literal clipboard wire contracts and the single real PTK input reader."""

import asyncio
import base64
import os
import re
import unittest

from prompt_toolkit.input import create_pipe_input

from ish.terminal.clipboard import ClipboardBroker
from ish.terminal.host import HostBroker, HostInput, HostProfile

ST = "\x1b\\"


class ClipboardTests(unittest.TestCase):
    """No clipboard operation succeeds merely because an output was queued."""

    def setUp(self):
        """Capture independent child replies and normalized host output."""
        self.output, self.replies = [], []
        self.profile = HostProfile(clipboard_protocol=True)
        self.broker = ClipboardBroker(
            reply=self.replies.append,
            emit=self.output.append,
            profile=lambda: self.profile,
        )

    def identity(self):
        """Read the actual broker-assigned ID from its host wire request."""
        return re.search(r":id=([a-zA-Z0-9._+-]+)", self.output[-1])[1]

    def host(self, fields, *, identity=None, payload=None):
        """Inject a literal host reply with the observed wire identity."""
        identity = identity or self.identity()
        body = f"\x1b]5522;{fields}:id={identity}"
        if payload is not None:
            body += ";" + payload
        return self.broker.host_reply(body + ST)

    def test_text_and_iterm_copy_emit_normalized_osc52_without_fake_ack(self):
        """Legacy writes share normalization without inventing completion replies."""
        self.broker.child_osc(b"52;c;aMOpbGxv")
        self.broker.child_osc(b"1337;Copy=:5ZWK")
        self.assertEqual(
            self.output, ["\x1b]52;c;aMOpbGxv" + ST, "\x1b]52;c;5ZWK" + ST]
        )
        self.assertEqual(self.replies, [])
        self.broker.child_osc(b"52;c;invalid!")
        self.assertEqual(self.output[-1], "\x1b]52;c;" + ST)

    def test_iterm_capture_waits_for_endcopy_and_keeps_control_bytes_as_data(self):
        """Captured control-shaped bytes cannot issue nested clipboard commands."""
        self.assertTrue(self.broker.child_osc(b"1337;CopyToClipboard="))
        self.broker.capture("A한글".encode())
        self.assertFalse(self.broker.child_osc(b"52;c;aGlqYWNrcw=="))
        self.broker.capture(b"\x1b]52;c;aGlqYWNrcw==\x07")
        self.assertEqual(self.output, [])
        self.broker.child_osc(b"1337;EndCopy")
        expected = base64.b64encode(
            "A한글".encode() + b"\x1b]52;c;aGlqYWNrcw==\x07"
        ).decode()
        self.assertEqual(self.output, ["\x1b]52;c;" + expected + ST])
        self.assertFalse(self.broker.capturing)

    def test_iterm_unsupported_named_pasteboard_and_cancel_do_not_write(self):
        """Unknown destinations and abandoned captures leave the clipboard alone."""
        self.broker.child_osc(b"1337;CopyToClipboard=find")
        self.broker.capture(b"private")
        self.broker.child_osc(b"1337;EndCopy")
        self.broker.child_osc(b"1337;CopyToClipboard=")
        self.broker.capture(b"abandoned")
        self.broker.cancel()
        self.assertTrue(self.broker.capturing)
        self.assertTrue(self.broker.capture(b"still private"))
        self.broker.child_osc(b"1337;EndCopy")
        self.assertFalse(self.broker.capturing)
        self.assertEqual(self.output, [])

    def test_explicit_reset_and_close_release_cancelled_capture(self):
        """A full reset or shutdown releases discarded data without publishing it."""
        for finish in (self.broker.reset, self.broker.close):
            self.broker.child_osc(b"1337;CopyToClipboard=")
            self.broker.capture(b"private")
            self.broker.cancel()
            finish()
            self.assertFalse(self.broker.capturing)
            self.assertFalse(self.broker.capture(b"public"))
        self.assertEqual(self.output, [])

    def test_write_done_requires_correlated_host_completion_after_commit(self):
        """Only the matching host acknowledgement completes a committed write."""
        self.broker.child_osc(b"5522;type=write:id=client+1")
        identity = self.identity()
        self.assertNotEqual(identity, "client+1")
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;Y")
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;Q==")
        self.assertEqual(self.replies, [])
        self.host("type=write:status=DONE", identity=identity)
        self.assertEqual(self.replies, [])
        self.broker.child_osc(b"5522;type=wdata")
        self.assertEqual(self.replies, [])
        self.assertIn("mime=dGV4dC9wbGFpbg==;YQ==", self.output[1])
        self.host("type=write:status=DONE", identity=identity)
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=write:status=DONE:id=client+1\x1b\\"]
        )
        self.host("type=write:status=DONE", identity=identity)
        self.assertEqual(len(self.replies), 1)

    def test_mime_chunks_aliases_and_primary_location_remain_explicit(self):
        """Binary representations, aliases and destination survive normalization."""
        self.broker.child_osc(b"5522;type=write:loc=primary:id=a")
        self.broker.child_osc(b"5522;type=wdata:mime=aW1hZ2UvcG5n;AAECAw==")
        self.broker.child_osc(
            b"5522;type=walias:mime=aW1hZ2UvcG5n;YXBwbGljYXRpb24vb2N0ZXQtc3RyZWFt"
        )
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;YWJj")
        self.broker.child_osc(b"5522;type=wdata")
        self.assertIn(":loc=primary", self.output[0])
        self.assertIn("type=walias", self.output[2])
        self.host("type=write:status=EPERM")
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=write:status=EPERM:loc=primary:id=a\x1b\\"]
        )

    def test_invalid_base64_aborts_without_committing_partial_host_data(self):
        """A malformed continuation never causes a partial clipboard commit."""
        self.broker.child_osc(b"5522;type=write:id=bad")
        identity = self.identity()
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;YWJj")
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;not valid")
        self.broker.child_osc(b"5522;type=wdata")
        self.assertFalse(
            any(
                value == f"\x1b]5522;type=wdata:id={identity}" + ST
                for value in self.output
            )
        )
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=write:status=EINVAL:id=bad\x1b\\"]
        )
        self.broker.child_osc(b"5522;type=write:id=new")
        next_identity = self.identity()
        self.assertNotEqual(identity, next_identity)
        self.host("type=write:status=DONE", identity=identity)
        self.assertEqual(len(self.replies), 1)

    def test_default_read_policy_does_not_expose_contents_but_allows_mime_listing(self):
        """Default policy distinguishes harmless type enumeration from contents."""
        self.broker.child_osc(b"52;c;?")
        self.broker.child_osc(b"5522;type=read:id=r;dGV4dC9wbGFpbg==")
        self.assertEqual(self.output, [])
        self.assertEqual(
            self.replies,
            [b"\x1b]52;c;\x1b\\", b"\x1b]5522;type=read:status=EPERM:id=r\x1b\\"],
        )
        self.broker.child_osc(b"5522;type=read:id=list;Lg==")
        self.assertIn(";Lg==", self.output[0])

    def test_allowed_osc52_read_uses_host_identity_and_translates_final_data(self):
        """An identity-bearing host read produces exactly one legacy child reply."""
        self.broker.allow_read = True
        self.broker.child_osc(b"52;p;?")
        identity = self.identity()
        self.assertIn("type=read", self.output[0])
        self.assertIn("loc=primary", self.output[0])
        self.host("type=read:status=OK", identity=identity)
        self.host(
            "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
            identity=identity,
            payload="aMOp",
        )
        self.host(
            "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
            identity=identity,
            payload="bGxv",
        )
        self.assertEqual(self.replies, [])
        self.host("type=read:status=DONE", identity=identity)
        self.assertEqual(self.replies, [b"\x1b]52;p;aMOpbGxv\x1b\\"])

    def test_allowed_mime_read_preserves_child_identity_and_validated_chunk_data(self):
        """Host IDs stay private while binary response chunks retain their MIME."""
        self.broker.allow_read = True
        self.broker.child_osc(b"5522;type=read:id=child!/1;aW1hZ2UvcG5n")
        identity = self.identity()
        self.host("type=read:status=OK", identity=identity)
        self.host(
            "type=read:status=DATA:mime=aW1hZ2UvcG5n", identity=identity, payload="AP8="
        )
        self.host("type=read:status=DONE", identity=identity)
        self.assertEqual(
            self.replies,
            [
                b"\x1b]5522;type=read:status=OK:id=child1\x1b\\",
                b"\x1b]5522;type=read:status=DATA:id=child1:mime=aW1hZ2UvcG5n;AP8=\x1b\\",
                b"\x1b]5522;type=read:status=DONE:id=child1\x1b\\",
            ],
        )

    def test_timeout_and_wrong_identity_cannot_complete_a_later_request(self):
        """Late packets and timer callbacks cannot terminate a subsequent request."""
        self.broker.allow_read = True
        self.broker.child_osc(b"5522;type=read:id=old;dGV4dC9wbGFpbg==")
        old = self.identity()
        self.broker.expire(old)
        self.broker.child_osc(b"5522;type=read:id=new;dGV4dC9wbGFpbg==")
        current = self.identity()
        self.host("type=read:status=OK", identity=old)
        self.host("type=read:status=DONE", identity=old)
        self.broker.expire(old)
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=read:status=EBUSY:id=old\x1b\\"]
        )
        self.host("type=read:status=EPERM", identity=current)
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=read:status=EPERM:id=new\x1b\\"
        )

    def test_unverified_host_and_disabled_policy_never_claim_success(self):
        """Unsupported or denied access never becomes an optimistic success."""
        self.profile = HostProfile()
        self.broker.allow_read = True
        self.broker.child_osc(b"5522;type=write:id=a")
        self.broker.child_osc(b"5522;type=read:id=b;dGV4dC9wbGFpbg==")
        self.assertEqual(self.output, [])
        self.assertTrue(all(b"status=ENOSYS" in reply for reply in self.replies))
        self.broker.allow_write = False
        self.broker.child_osc(b"52;c;YQ==")
        self.assertFalse(self.broker.write_text(b"selected"))
        self.assertEqual(self.output, [])

    def test_unrelated_malformed_host_reply_does_not_cancel_live_request(self):
        """Uncorrelated malformed control traffic cannot cancel the current owner."""
        self.broker.child_osc(b"5522;type=write:id=a")
        identity = self.identity()
        self.broker.host_reply("\x1b]5522;not-metadata\x1b\\")
        self.broker.child_osc(b"5522;type=wdata")
        self.host("type=write:status=DONE", identity=identity)
        self.assertEqual(self.replies, [b"\x1b]5522;type=write:status=DONE:id=a\x1b\\"])

    def test_explicit_selection_copy_ignores_child_policy_and_coalesces_while_busy(
        self,
    ):
        """Keep the most recent user copy without racing a pending child request."""
        self.broker.allow_write = False
        self.assertTrue(self.broker.copy_selection(b"user"))
        self.assertEqual(self.output, ["\x1b]52;cp;dXNlcg==" + ST])
        self.assertFalse(self.broker.write_text(b"child"))
        self.broker.allow_read = True
        self.broker.child_osc(b"5522;type=read:id=r;dGV4dC9wbGFpbg==")
        identity = self.identity()
        before = len(self.output)
        self.assertTrue(self.broker.copy_selection(b"first"))
        self.assertTrue(self.broker.copy_selection(b"latest"))
        self.assertEqual(len(self.output), before)
        self.host("type=read:status=EPERM", identity=identity)
        self.assertEqual(self.output[-1], "\x1b]52;cp;bGF0ZXN0" + ST)
        self.assertEqual(len(self.output), before + 1)

    def test_user_primary_read_is_correlated_and_independent_of_child_policy(self):
        """Middle-click consumes only completed system PRIMARY text, never child data."""
        self.broker.allow_read = self.broker.allow_write = False
        pasted = []
        identity = self.broker.read_primary(pasted.append)
        self.assertEqual(identity, self.identity())
        self.assertEqual(
            self.output,
            [f"\x1b]5522;type=read:id={identity}:loc=primary;dGV4dC9wbGFpbg==" + ST],
        )
        self.host("type=read:status=OK", identity=identity)
        self.host("type=read:status=OK", identity=identity)
        self.host(
            "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
            identity=identity,
            payload=base64.b64encode("한글\n".encode()).decode(),
        )
        self.assertEqual(pasted, [])
        self.host("type=read:status=DONE", identity=identity)
        self.host("type=read:status=DONE", identity=identity)
        self.assertEqual(pasted, ["한글\n"])
        self.assertEqual(self.replies, [])
        self.assertTrue(self.broker.idle.is_set())

    def test_cancelled_primary_read_cannot_paste_into_a_new_action(self):
        """A popup, focus change or new key cancels the old action without a child reply."""
        pasted = []
        old = self.broker.read_primary(pasted.append)
        self.host("type=read:status=OK", identity=old)
        self.host(
            "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
            identity=old,
            payload="b2xk",
        )
        self.assertTrue(self.broker.cancel_paste())
        self.assertFalse(self.broker.cancel_paste())
        current = self.broker.read_primary(pasted.append)
        self.assertNotEqual(current, old)
        self.host("type=read:status=DONE", identity=old)
        self.broker.expire(old)
        self.host("type=read:status=OK", identity=current)
        self.host(
            "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
            identity=current,
            payload="bmV3",
        )
        self.host("type=read:status=DONE", identity=current)
        self.assertEqual(pasted, ["new"])
        self.assertEqual(self.replies, [])

    def test_new_user_read_replaces_only_user_read_and_preserves_busy_child(self):
        """User reads are not delayed behind unrelated child clipboard transactions."""
        pasted = []
        first = self.broker.read_primary(pasted.append)
        second = self.broker.read_primary(pasted.append)
        self.assertNotEqual(first, second)
        self.host("type=read:status=OK", identity=first)
        self.host("type=read:status=DONE", identity=first)
        self.assertEqual(pasted, [])
        self.broker.cancel_paste()
        self.broker.child_osc(b"5522;type=write:id=child")
        child = self.identity()
        self.assertIsNone(self.broker.read_primary(pasted.append))
        self.assertFalse(self.broker.cancel_paste())
        self.broker.child_osc(b"5522;type=wdata")
        self.host("type=write:status=DONE", identity=child)
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=write:status=DONE:id=child\x1b\\"]
        )

    def test_primary_read_errors_timeout_and_close_never_paste_or_reply_to_child(self):
        """Unusable, unfinished and closed reads have no optimistic fallback text."""
        pasted = []
        for finish in ("EPERM", "timeout", "invalid_utf8", "close"):
            with self.subTest(finish=finish):
                identity = self.broker.read_primary(pasted.append)
                if finish == "EPERM":
                    self.host("type=read:status=EPERM", identity=identity)
                elif finish == "timeout":
                    self.broker.expire(identity)
                elif finish == "close":
                    self.broker.close()
                else:
                    self.host("type=read:status=OK", identity=identity)
                    self.host(
                        "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
                        identity=identity,
                        payload="/w==",
                    )
                    self.host("type=read:status=DONE", identity=identity)
                self.host("type=read:status=OK", identity=identity)
                self.host("type=read:status=DONE", identity=identity)
                self.assertIsNone(self.broker.pending)
        self.assertEqual(pasted, [])
        self.assertEqual(self.replies, [])
        self.assertIsNone(self.broker.read_primary(pasted.append))

    def test_primary_read_unsupported_host_has_no_internal_clipboard_fallback(self):
        """Only a verified identity-bearing host supplies system PRIMARY."""
        self.profile = HostProfile()
        pasted = []
        self.assertIsNone(self.broker.read_primary(pasted.append))
        self.assertEqual((self.output, self.replies, pasted), ([], [], []))

    def test_primary_text_limit_aborts_without_delivering_a_prefix(self):
        """Oversized system PRIMARY cannot consume unbounded memory or paste partly."""
        pasted = []
        identity = self.broker.read_primary(pasted.append)
        self.host("type=read:status=OK", identity=identity)
        payload = base64.b64encode(b"x" * 4096).decode()
        for _ in range(257):
            self.host(
                "type=read:status=DATA:mime=dGV4dC9wbGFpbg==",
                identity=identity,
                payload=payload,
            )
        self.host("type=read:status=DONE", identity=identity)
        self.assertEqual((pasted, self.replies), ([], []))
        self.assertIsNone(self.broker.pending)

    def test_user_callback_failure_is_not_hidden_as_a_host_protocol_error(self):
        """Unexpected application errors remain visible after transaction completion."""

        def broken(text):
            raise ValueError("application error")

        identity = self.broker.read_primary(broken)
        self.host("type=read:status=OK", identity=identity)
        with self.assertRaisesRegex(ValueError, "application error"):
            self.host("type=read:status=DONE", identity=identity)
        self.assertIsNone(self.broker.pending)
        self.assertEqual(self.replies, [])

    def test_queued_explicit_copy_retains_primary_selector(self):
        """Copy queue retains its destination when another transaction finishes."""
        self.broker.read_primary(lambda text: None)
        self.broker.write_text(b"primary", "p", explicit=True)
        self.broker.cancel_paste()
        self.assertEqual(self.output[-1], "\x1b]52;p;cHJpbWFyeQ==" + ST)

    def test_write_accepts_protocol_minimum_64_mib_before_enforcing_limit(self):
        """Stream the required 64 MiB without retaining the full payload in Python."""
        count = 0

        def emit(value):
            nonlocal count
            count += 1

        self.broker.emit = emit
        self.broker.child_osc(b"5522;type=write:id=large")
        packet = (
            b"5522;type=wdata:mime=YXBwbGljYXRpb24vb2N0ZXQtc3RyZWFt;"
            + base64.b64encode(b"a" * 4096)
        )
        for _ in range(16_384):
            self.broker.child_osc(packet)
        self.assertEqual(self.replies, [])
        self.assertGreaterEqual(count, 16_385)
        self.broker.child_osc(packet)
        self.assertEqual(
            self.replies, [b"\x1b]5522;type=write:status=EFBIG:id=large\x1b\\"]
        )

    def test_host_mime_data_is_one_strict_base64_stream_for_padded_child_chunks(self):
        """Independently padded child packets normalize to one strict host stream."""
        data = bytes(range(256)) * 257
        self.broker.child_osc(b"5522;type=write:id=binary")
        for offset in range(0, len(data), 4096):
            self.broker.child_osc(
                b"5522;type=wdata:mime=aW1hZ2UvcG5n;"
                + base64.b64encode(data[offset : offset + 4096])
            )
        self.broker.child_osc(b"5522;type=wdata")
        chunks = [
            value.split(";", 2)[2][:-2] for value in self.output if ":mime=" in value
        ]
        self.assertTrue(all("=" not in chunk for chunk in chunks[:-1]))
        self.assertEqual(base64.b64decode("".join(chunks), validate=True), data)

    def test_paste_event_mode_is_permanently_reset_instead_of_native_false_advertising(
        self,
    ):
        """Mode queries do not claim unsupported unsolicited MIME paste delivery."""
        self.assertTrue(self.broker.child_csi(b"?5522$p"))
        self.assertTrue(self.broker.child_csi(b"?5522h"))
        self.assertTrue(self.broker.child_csi(b"?5522l"))
        self.assertEqual(self.replies, [b"\x1b[?5522;4$y"])
        self.assertFalse(self.broker.child_csi(b"?2004h"))

    def test_stale_explicit_child_chunk_id_cannot_attach_to_a_new_write(self):
        """Explicit continuation IDs protect the new request before decoding data."""
        self.broker.child_osc(b"5522;type=write:id=old")
        self.broker.expire(self.identity())
        self.broker.child_osc(b"5522;type=write:id=new")
        start = len(self.output)
        self.broker.child_osc(b"5522;type=wdata:id=old:mime=dGV4dC9wbGFpbg==;YWJj")
        self.broker.child_osc(b"5522;type=walias:id=old:mime=dGV4dC9wbGFpbg==;invalid!")
        self.broker.child_osc(b"5522;type=wdata:id=old")
        self.assertEqual(len(self.output), start)
        self.broker.child_osc(b"5522;type=wdata:id=new:mime=dGV4dC9wbGFpbg==;bmV3")
        self.broker.child_osc(b"5522;type=wdata:id=new")
        self.host("type=write:status=DONE")
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=write:status=DONE:id=new\x1b\\"
        )
        self.assertIn(";bmV3", self.output[start])

    def test_idle_tracks_actual_completion_and_bounded_failure(self):
        """Shutdown can wait on protocol completion without polling shell state."""
        self.assertTrue(self.broker.idle.is_set())
        self.broker.child_osc(b"5522;type=write:id=a")
        identity = self.identity()
        self.assertFalse(self.broker.idle.is_set())
        self.broker.child_osc(b"5522;type=wdata")
        self.assertFalse(self.broker.idle.is_set())
        self.host("type=write:status=DONE", identity=identity)
        self.assertTrue(self.broker.idle.is_set())
        self.broker.child_osc(b"5522;type=write:id=b")
        self.assertFalse(self.broker.idle.is_set())
        self.broker.expire(self.identity())
        self.assertTrue(self.broker.idle.is_set())


@unittest.skipUnless(os.name == "posix", "The host adapter uses PTK's POSIX reader")
class ClipboardInputTests(unittest.IsolatedAsyncioTestCase):
    """Real host controls never enter the PTK keyboard queue or shell text path."""

    async def test_capture_abort_keeps_private_bytes_out_of_actual_terminal(self):
        """Framer cancellation discards capture data until its real EndCopy marker."""
        from ish.terminal.vt import Terminal

        for abort in (b"\x18", b"\x1a", b"\x1b[12\x1b[0m"):
            for fragmented in (False, True):
                with self.subTest(abort=abort, fragmented=fragmented):
                    output, replies = [], []
                    broker = ClipboardBroker(
                        reply=replies.append,
                        emit=output.append,
                        profile=HostProfile,
                    )
                    with Terminal(80, 24, replies.append) as terminal:
                        terminal.clipboard = broker
                        wire = (
                            b"\x1b]1337;CopyToClipboard=\x07A"
                            + abort
                            + b"PRIVATE\x1b]1337;EndCopy\x07PUBLIC"
                        )
                        if fragmented:
                            for byte in wire:
                                terminal.feed(bytes((byte,)))
                        else:
                            terminal.feed(wire)
                        text = "".join(
                            cell.text for cell in terminal.frame().cells[0]
                        ).rstrip()
                        self.assertEqual(text, "PUBLIC")
                        self.assertFalse(broker.capturing)
                    self.assertEqual(output, [])
                    self.assertEqual(replies, [])
                    broker.close()

    async def test_fragmented_host_response_and_paste_keep_distinct_consumers(self):
        """Framed host replies bypass keys while paste payloads remain literal."""
        output, replies = [], []
        broker = ClipboardBroker(
            reply=replies.append,
            emit=output.append,
            profile=lambda: HostProfile(clipboard_protocol=True),
            allow_read=True,
        )
        with create_pipe_input() as pipe:
            host = HostBroker(on_clipboard_reply=broker.host_reply)
            input = HostInput(pipe, host)
            broker.child_osc(b"52;c;?")
            identity = re.search(r":id=([a-zA-Z0-9._+-]+)", output[-1])[1]
            pipe.send_text("a\x1b]5522;type=read:status=OK:id=" + identity)
            keys = input.read_keys()
            self.assertEqual([key.data for key in keys], ["a"])
            self.assertEqual(input.flush_keys(), [])
            pipe.send_text(
                ST
                + "b\x1b]5522;type=read:status=DATA:mime=dGV4dC9wbGFpbg==:id="
                + identity
                + ";aGk="
                + ST
            )
            keys += input.read_keys()
            pipe.send_text("\x1b]5522;type=read:status=DONE:id=" + identity + ST + "c")
            keys += input.read_keys()
            self.assertEqual(replies, [])
            typed = []
            for key in keys:
                if not host.handle_control(key):
                    typed.append(key.data)
            self.assertEqual(typed, ["a", "b", "c"])
            self.assertEqual(replies, [b"\x1b]52;c;aGk=\x1b\\"])
            pipe.send_text("\x1b[200~\x1b]52;c;literal\x07\x1b[201~")
            paste = input.read_keys()
            self.assertEqual(paste[0].data, "\x1b]52;c;literal\x07")
            self.assertEqual(len(replies), 1)
        broker.close()
        await asyncio.sleep(0)


@unittest.skipUnless(os.name == "posix", "Real PTK/native input requires POSIX")
class PrimaryInputTests(unittest.IsolatedAsyncioTestCase):
    """Verify cancellation at actual PTK input and popup ownership boundaries."""

    async def asyncSetUp(self):
        """Reuse the actual PTK renderer and independently negotiate clipboard reads."""
        import test_terminal_composition as composition

        await composition.TerminalCompositionTests.asyncSetUp(self)
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.owner.host.control("\x1b[?5522;2$y")
        self.owner.host.finish_probe()
        self.assertTrue(self.owner.profile.clipboard_protocol)
        self.owner.clipboard.allow_read = self.owner.clipboard.allow_write = False
        self.started = asyncio.Event()
        emit = self.owner.clipboard.emit

        def observe(wire):
            emit(wire)
            if wire.startswith("\x1b]5522;type=read:"):
                self.started.set()

        self.owner.clipboard.emit = observe
        await composition.TerminalCompositionTests.start_input(self)

    def render(self):
        """Present the layout through the existing real renderer fixture."""
        import test_terminal_composition as composition

        return composition.TerminalCompositionTests.render(self)

    async def asyncTearDown(self):
        """Cancel transactions and timers before closing PTK and the native grid."""
        import test_terminal_composition as composition

        self.owner.clipboard.close()
        self.owner.selection.close()
        await composition.TerminalCompositionTests.asyncTearDown(self)

    async def middle(self):
        """Start the action using literal reports through PTK's sole input reader."""
        self.started.clear()
        self.pipe.send_text("\x1b[<1;3;3M\x1b[<1;3;3m")
        await asyncio.wait_for(self.started.wait(), 3)
        return self.owner.clipboard.pending.host_id

    @staticmethod
    def response(identity, text="PRIMARY"):
        """Build a literal, complete host response with the observed request ID."""
        payload = base64.b64encode(text.encode()).decode()
        return (
            f"\x1b]5522;type=read:status=OK:id={identity}{ST}"
            f"\x1b]5522;type=read:status=DATA:mime=dGV4dC9wbGFpbg==:id={identity};{payload}{ST}"
            f"\x1b]5522;type=read:status=DONE:id={identity}{ST}"
        )

    async def processed(self):
        """Wait for the explicit transaction to finish, not for quiet output."""
        async with asyncio.timeout(3):
            while self.owner.clipboard.pending is not None:
                await asyncio.sleep(0.001)
        await asyncio.sleep(0)

    async def test_middle_click_pastes_once_through_native_bracketed_paste_mode(self):
        """Duplicate completion cannot repeat a native bracketed paste."""
        self.owner.vt.feed(b"\x1b[?2004h")
        identity = await self.middle()
        wire = self.response(identity, "system 한글\nline")
        self.pipe.send_text(wire + wire)
        await self.processed()
        self.assertEqual(
            bytes(self.owner.session.pending_input),
            b"\x1b[200~" + "system 한글\nline".encode() + b"\x1b[201~",
        )

    async def test_new_key_before_host_completion_cancels_the_user_paste(self):
        """A later key owns input before any delayed clipboard data is delivered."""
        identity = await self.middle()
        self.pipe.send_text("K" + self.response(identity))
        await self.processed()
        self.assertEqual(bytes(self.owner.session.pending_input), b"K")

    async def test_popup_and_host_focus_loss_reject_late_primary_data(self):
        """Neither a popup nor a resumed terminal inherits an old read action."""
        from prompt_toolkit.widgets import TextArea

        identity = await self.middle()
        editor = TextArea(multiline=False)
        popup = self.owner.plugins.show_popup(editor, width=8, height=2)
        edited = asyncio.Event()
        editor.buffer.on_text_changed += lambda _: edited.set()
        self.pipe.send_text(self.response(identity) + "POPUP")
        await asyncio.wait_for(edited.wait(), 3)
        self.assertEqual(editor.text, "POPUP")
        self.assertEqual(bytes(self.owner.session.pending_input), b"")
        self.owner.plugins.close_popup(popup)
        identity = await self.middle()
        self.pipe.send_text("\x1b[O" + self.response(identity) + "\x1b[I")
        await self.processed()
        self.assertEqual(bytes(self.owner.session.pending_input), b"")

    async def test_plugin_key_is_new_input_even_without_changing_focus(self):
        """An opt-in plugin action also replaces the user's old paste intention."""
        seen = asyncio.Event()

        @self.owner.plugins.bind("f5")
        def action(event):
            seen.set()

        identity = await self.middle()
        self.pipe.send_text("\x1b[15~" + self.response(identity))
        await asyncio.wait_for(seen.wait(), 3)
        await self.processed()
        self.assertEqual(bytes(self.owner.session.pending_input), b"")
