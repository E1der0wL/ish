"""Verify wire admission independently of shell templates and live PTK tests."""

import unittest
from dataclasses import replace

from ish.shell.handoff import (
    Handoff,
    InputKind,
    InputOwner,
    InputRequest,
    decode_mode,
    decode_request,
)


class WireTests(unittest.TestCase):
    """Use literal wire messages rather than the producer's encoder as an oracle."""

    def test_request_identity_and_kind(self):
        """Only the bounded versioned message for this session is meaningful."""
        request = decode_request(b"1;session;7;2;C", "session")
        self.assertEqual(
            (request.submission, request.sequence, request.kind),
            (7, 2, InputKind.SYNTAX),
        )
        for body in (
            b"1;foreign;7;2;C",
            b"2;session;7;2;C",
            b"1;session;0;2;C",
            b"1;session;7;0;R",
            b"1;session;7;2;P",
            b"1;session;7;2;C;extra",
            b"1;session;-1;2;R",
            b"1;session;999999999999999999999999;2;R",
        ):
            with self.subTest(body=body):
                self.assertIsNone(decode_request(body, "session"))

    def test_mode_is_tied_to_a_primary_generation(self):
        """An advertised mode carries its own prompt identity and explicit bit."""
        self.assertEqual(decode_mode(b"1;session;8;1", "session"), (8, True))
        self.assertEqual(decode_mode(b"1;session;9;0", "session"), (9, False))
        for body in (
            b"1;foreign;8;1",
            b"1;session;0;1",
            b"1;session;8;yes",
            b"1;session;8;1;extra",
        ):
            self.assertIsNone(decode_mode(body, "session"))


class HandoffTests(unittest.TestCase):
    """Assert permissions and rejected transitions through the public contract."""

    def test_multiple_syntax_requests_then_native_permission(self):
        """Every physical syntax reply needs its own one-shot correlated request."""
        state = Handoff()
        submission = state.begin()
        for sequence in (1, 2):
            request = InputRequest(submission, sequence, InputKind.SYNTAX)
            self.assertTrue(state.receive(request))
            self.assertEqual(state.owner, InputOwner.EDITOR)
            self.assertFalse(state.receive(request))
            self.assertFalse(state.reply(replace(request)))
            self.assertTrue(state.reply(request))
            self.assertFalse(state.reply(request))
            self.assertEqual(state.owner, InputOwner.BARRIER)
        raw = InputRequest(submission, 3, InputKind.RAW)
        self.assertTrue(state.receive(raw))
        self.assertEqual(state.owner, InputOwner.RELEASE)
        self.assertFalse(state.reply(raw))
        self.assertTrue(state.release(raw))
        self.assertEqual(state.owner, InputOwner.RAW)
        self.assertFalse(state.release(raw))
        self.assertFalse(state.receive(InputRequest(submission, 4, InputKind.SYNTAX)))

    def test_cancelled_or_reordered_events_cannot_open_input(self):
        """Old submissions and skipped sequence numbers cannot authorize a new owner."""
        state = Handoff()
        old = state.begin()
        with self.assertRaises(RuntimeError):
            state.begin()
        state.reclaim()
        self.assertFalse(state.receive(InputRequest(old, 1, InputKind.RAW)))
        state.primary()
        current = state.begin()
        self.assertGreater(current, old)
        self.assertFalse(state.receive(InputRequest(old, 1, InputKind.SYNTAX)))
        self.assertFalse(state.receive(InputRequest(current, 2, InputKind.RAW)))
        self.assertEqual(state.owner, InputOwner.BARRIER)
        self.assertTrue(state.receive(InputRequest(current, 1, InputKind.SYNTAX)))

    def test_reclaim_supersedes_a_delayed_raw_release(self):
        """A queued native grant cannot reopen forwarding after primary recovery starts."""
        state = Handoff()
        submission = state.begin()
        raw = InputRequest(submission, 1, InputKind.RAW)
        self.assertTrue(state.receive(raw))
        state.reclaim()
        self.assertFalse(state.release(raw))
        self.assertEqual(state.owner, InputOwner.RECLAIM)
        state.primary()
        self.assertEqual(state.owner, InputOwner.PRIMARY)


if __name__ == "__main__":
    unittest.main()
