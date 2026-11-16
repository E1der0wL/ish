"""Exercise popup grants over real Unix sockets and literal shell wire records."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from examples.stream_plugins.stream_popup.stream_popup import create

from ish.stream.app import App
from ish.stream.panel import Panel
from ish.stream.protocol import Keys, Stream

NONCE = "socket-test-session-0123456789abcdef"
F2 = b"\x1bOQ"
DEADLINE = 3
BAR = "Request test bar"


class NativeSession:
    """Observe the input and pause operations a real PTY session would receive."""

    def __init__(self):
        """Keep input results and ownership effects separate."""
        self.writes = []
        self.output_enabled = []
        self.sizes = []

    def send(self, data):
        """Record bytes at the native input consumer boundary."""
        if data:
            self.writes.append(data)

    def resize(self, columns, rows):
        """Record native viewport notifications without changing test stdin."""
        self.sizes.append((columns, rows))

    def set_output_enabled(self, enabled):
        """Record whether the popup has paused native output presentation."""
        self.output_enabled.append(enabled)

    @property
    def data(self):
        """Return the exact byte stream delivered to the native consumer."""
        return b"".join(self.writes)


class Output:
    """Capture the actual host-bound stream, without interpreting UI state."""

    def __init__(self):
        """Start an empty host byte sink."""
        self.data = bytearray()

    def write(self, data):
        """Append bytes in the order the coordinator wrote them."""
        self.data.extend(data)


class InputWatch:
    """Keep production stdin registration away from the unittest runner."""

    def add_reader(self, fd, callback):
        """Input is driven explicitly by each test at the ingress boundary."""

    def remove_reader(self, fd):
        """There is no real stdin reader in this harness."""


class RequestTests(unittest.IsolatedAsyncioTestCase):
    """Check wire identity, live connections and native/popup input consumers."""

    async def asyncSetUp(self):
        """Use a real socket server and PTK panel with an observed PTY boundary."""
        self.directory = tempfile.TemporaryDirectory(prefix="ish-request-test-")
        self.path = str(Path(self.directory.name, "control"))
        self.connections = []
        self.finished = asyncio.Queue()
        self.errors = []
        self.focus = []
        self.results = []
        self.focus_changed = asyncio.Event()
        self.accepted = asyncio.Event()
        self.app = App(["tcsh"], Path(self.directory.name))
        self.app.nonce = NONCE
        self.app.loop = InputWatch()
        self.app.rows, self.app.cols = 24, 80
        self.app.writer = Output()
        self.app.session = NativeSession()
        self.app.stream = Stream(
            NONCE, self.app._write, self.app._event, self.app._hide
        )
        self.app.keys = Keys(self.app._native_input, self.app._open_popup)

        def focus(popup):
            self.focus.append(popup)
            self.app._focus(popup)
            self.focus_changed.set()

        self.app.panel = Panel(
            self.app._paint,
            on_focus=focus,
            on_result=self.results.append,
            bar=BAR,
            popup=create,
        )
        await self.app.panel.start(80, 5)

        async def connected(reader, writer):
            self.accepted.set()
            try:
                await self.app._popup_client(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors.append(error)
            finally:
                self.finished.put_nowait(None)

        self.server = await asyncio.start_unix_server(connected, self.path, limit=1024)
        self.wire("P", 1)

    async def asyncTearDown(self):
        """Close only this test's sockets, PTK task and temporary directory."""
        self.app.stopping = True
        self.app._release_request()
        await self.app.panel.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.connections:
            writer.close()
        for task in tuple(self.app.clients):
            task.cancel()
        await asyncio.gather(*self.app.clients, return_exceptions=True)
        for writer in self.connections:
            try:
                await writer.wait_closed()
            except OSError:
                pass
        self.directory.cleanup()

    def wire(self, kind, serial):
        """Send literal external shell protocol bytes through the real framer."""
        self.app._output(f"\x1b]777;ish;{NONCE};{serial};{kind}\x07".encode())

    async def client(self, payload):
        """Send a helper request through a real Unix stream socket."""
        reader, writer = await asyncio.open_unix_connection(self.path)
        self.connections.append(writer)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        writer.write(data + b"\n")
        await writer.drain()
        await asyncio.wait_for(self.accepted.wait(), DEADLINE)
        return reader, writer

    async def reply(self, reader):
        """Treat a missing helper response as a test failure, never readiness."""
        return await asyncio.wait_for(reader.readline(), DEADLINE)

    async def focused(self, value):
        """Wait for an externally published focus callback."""
        async with asyncio.timeout(DEADLINE):
            while not self.focus or self.focus[-1] is not value:
                self.focus_changed.clear()
                await self.focus_changed.wait()

    async def test_f2_suffix_returns_to_native_when_shell_executes(self):
        """A native H decision releases all held typeahead without opening UI."""
        suffix = b"next input\r\n"
        self.app.keys.feed(F2 + suffix)
        request = self.app.request
        self.assertEqual(self.app.session.data, F2)
        self.assertEqual(self.focus, [])
        self.wire("H", 2)
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.app.session.data, F2 + suffix)
        self.assertEqual(self.focus, [])

    async def test_boundary_without_forwarded_shortcut_cannot_open_popup(self):
        """An unsolicited B and socket request do not acquire user input."""
        self.wire("B", 2)
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(await self.reply(reader), b"cancel\n")
        self.assertEqual(self.focus, [])
        self.assertEqual(self.app.session.data, b"")

    async def test_matching_request_routes_only_popup_input_then_waits_for_primary(
        self,
    ):
        """Input goes to its final consumer exactly once across the full exchange."""
        self.app.keys.feed(F2 + b"inside")
        request = self.app.request
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(self.focus, [])
        self.wire("B", 2)
        await self.focused(True)
        self.app._native_input(b" popup\r")
        self.assertEqual(await self.reply(reader), b"done\n")
        self.assertEqual(request.stage, "resuming")
        self.assertEqual(self.results, ["inside popup"])
        self.assertEqual(self.app.session.data, F2)
        self.app._native_input(b"after popup")
        self.assertEqual(self.app.session.data, F2)
        self.wire("P", 3)
        self.assertEqual(request.stage, "complete")
        self.assertIsNone(self.app.request)
        self.assertEqual(self.app.session.data, F2 + b"after popup")
        self.wire("P", 3)
        self.assertEqual(self.app.session.data, F2 + b"after popup")
        self.assertEqual(self.focus, [True, False])
        self.assertIn(False, self.app.session.output_enabled)
        self.assertTrue(self.app.session.output_enabled[-1])

    async def test_duplicate_connection_cannot_claim_current_popup(self):
        """The same valid wire identity grants popup focus at most once."""
        self.app.keys.feed(F2)
        self.wire("B", 2)
        first, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        duplicate, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(await self.reply(duplicate), b"cancel\n")
        self.assertEqual(self.focus, [True])
        self.app._native_input(b"once\r")
        self.assertEqual(await self.reply(first), b"done\n")
        self.assertEqual(self.results, ["once"])

    async def test_child_exit_revokes_popup_but_preserves_host_backpressure(self):
        """A stopped child cannot keep UI focus or bypass a paused host writer."""
        self.app.keys.feed(F2 + b"popup text")
        self.wire("B", 2)
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        request = self.app.request
        self.app._pause("host", True)
        self.app._child_exited(0)
        self.assertEqual(await self.reply(reader), b"cancel\n")
        self.assertEqual(self.focus, [True, False])
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.app.session.data, F2)
        self.assertFalse(self.app.session.output_enabled[-1])
        self.app._pause("host", False)
        self.assertTrue(self.app.session.output_enabled[-1])
        self.wire("P", 3)
        self.assertEqual(self.focus, [True, False])
        self.assertFalse(self.app.active)

    async def test_first_boundary_identity_cannot_be_replaced(self):
        """A later B cannot transfer held input to an unrelated helper identity."""
        self.app.keys.feed(F2 + b"held")
        self.wire("B", 2)
        self.wire("B", 3)
        unrelated, _ = await self.client({"nonce": NONCE, "serial": 3})
        self.assertEqual(await self.reply(unrelated), b"cancel\n")
        self.assertEqual(self.focus, [])
        original, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        self.app._native_input(b"\r")
        self.assertEqual(await self.reply(original), b"done\n")
        self.assertEqual(self.results, ["held"])
        self.assertEqual(self.app.session.data, F2)

    async def test_waiting_duplicate_clients_claim_once(self):
        """Two live requests before B still grant only one popup and one reply."""
        self.app.keys.feed(F2)
        first, _ = await self.client({"nonce": NONCE, "serial": 2})
        duplicate, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.wire("B", 2)
        await self.focused(True)
        self.app._native_input(b"once\r")
        replies = await asyncio.gather(self.reply(first), self.reply(duplicate))
        self.assertEqual(sorted(replies), [b"cancel\n", b"done\n"])
        self.assertEqual(self.focus, [True, False])
        self.assertEqual(self.results, ["once"])
        self.assertEqual(self.app.session.data, F2)

    async def test_bound_waiter_cancellation_discards_popup_typeahead(self):
        """A revoked B-bound request cannot turn unsubmitted popup text into shell input."""
        self.app.keys.feed(F2 + b"unsubmitted popup\r")
        request = self.app.request
        self.wire("B", 2)
        self.wire("H", 3)
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.app.session.data, F2)
        self.wire("P", 4)
        late, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(await self.reply(late), b"cancel\n")
        self.app._native_input(b"next native")
        self.assertEqual(self.app.session.data, F2 + b"next native")
        self.assertEqual(self.focus, [])

    async def test_primary_while_popup_is_open_cancels_instead_of_completing(self):
        """A P cannot manufacture successful return just by hiding an open popup."""
        self.app.keys.feed(F2)
        request = self.app.request
        self.wire("B", 2)
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        self.app._native_input(b"unfinished")
        self.wire("P", 3)
        self.assertEqual(await self.reply(reader), b"cancel\n")
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.results, [None])
        self.app._native_input(b"native")
        self.assertEqual(self.app.session.data, F2 + b"native")

    async def test_primary_before_return_reply_cancels_instead_of_completing(self):
        """Closing PTK alone is insufficient without queuing the matching reply."""
        self.app.keys.feed(F2)
        request = self.app.request
        self.wire("B", 2)
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        self.app._native_input(b"chosen\r")
        # Do not yield: P precedes the socket task's chance to queue its reply.
        self.wire("P", 3)
        self.assertEqual(await self.reply(reader), b"cancel\n")
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.results, ["chosen"])
        self.assertEqual(self.app.session.data, F2)

    async def test_fast_primary_survives_late_drain_failure(self):
        """Actual reply receipt and P complete return before a delayed drain fails."""
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()
        original_client = self.app._popup_client

        class DelayedWriter:
            """Keep real socket writes while controlling only drain completion."""

            def __init__(self, writer):
                """Wrap the server's real socket without replacing its bytes."""
                self.writer = writer

            def write(self, data):
                """Deliver the actual response before blocking drain."""
                self.writer.write(data)

            async def drain(self):
                """Fail only after the test observes the real response and sends P."""
                drain_started.set()
                await release_drain.wait()
                raise ConnectionResetError("late drain failure")

            def close(self):
                """Close the real transport after its final lifecycle observation."""
                self.writer.close()

        async def delayed_client(reader, writer):
            await original_client(reader, DelayedWriter(writer))

        self.app._popup_client = delayed_client
        self.app.keys.feed(F2)
        request = self.app.request
        self.wire("B", 2)
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        self.app._native_input(b"chosen\r")
        self.assertEqual(await self.reply(reader), b"done\n")
        await asyncio.wait_for(drain_started.wait(), DEADLINE)
        self.app._native_input(b"native suffix")
        self.assertEqual(self.app.session.data, F2)
        self.wire("P", 3)
        self.assertEqual(request.stage, "complete")
        self.assertEqual(self.app.session.data, F2 + b"native suffix")
        release_drain.set()
        await asyncio.wait_for(self.finished.get(), DEADLINE)
        self.assertEqual(request.stage, "complete")
        self.assertEqual(self.app.session.data, F2 + b"native suffix")
        self.assertEqual(self.errors, [])

    async def test_wrong_nonce_serial_and_numeric_type_do_not_claim_request(self):
        """Invalid identities leave the valid live widget able to acquire focus."""
        self.app.keys.feed(F2)
        self.wire("B", 2)
        for payload in (
            {"nonce": "foreign", "serial": 2},
            {"nonce": NONCE, "serial": 99},
            {"nonce": NONCE, "serial": "2"},
            {"nonce": NONCE, "serial": 2.0},
            {"nonce": NONCE, "serial": True},
        ):
            with self.subTest(payload=payload):
                self.errors.clear()
                reader, _ = await self.client(payload)
                self.assertEqual(await self.reply(reader), b"cancel\n")
                self.assertEqual(self.focus, [])
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        request = self.app.request
        self.app._native_input(b"\x03")
        self.assertEqual(await self.reply(reader), b"done\n")
        self.assertEqual(self.results, [None])
        self.wire("P", 3)
        self.assertEqual(request.stage, "complete")

    async def test_cancelled_waiter_cannot_open_current_or_next_popup(self):
        """A delayed old helper is rejected even when a newer F2 is pending."""
        self.app.keys.feed(F2 + b"native")
        waiting, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.wire("H", 2)
        self.assertEqual(await self.reply(waiting), b"cancel\n")
        self.assertEqual(self.app.session.data, F2 + b"native")
        self.wire("P", 3)
        self.app.keys.feed(F2)
        self.wire("B", 4)
        stale, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(await self.reply(stale), b"cancel\n")
        self.assertEqual(self.focus, [])
        current, _ = await self.client({"nonce": NONCE, "serial": 4})
        await self.focused(True)
        self.app._native_input(b"\r")
        self.assertEqual(await self.reply(current), b"done\n")
        self.assertEqual(self.results, [""])

    async def test_non_object_and_malformed_json_are_cleanly_rejected(self):
        """Malformed clients must not escape into the host's exception output."""
        self.app.keys.feed(F2)
        self.wire("B", 2)
        for payload in (b"null", b"[]", b'"text"', b"123", b"{bad json}"):
            with self.subTest(payload=payload):
                self.errors.clear()
                reader, _ = await self.client(payload)
                self.assertEqual(await self.reply(reader), b"cancel\n")
                await asyncio.wait_for(self.finished.get(), DEADLINE)
                self.assertEqual(self.errors, [])
                self.assertEqual(self.focus, [])

    async def test_oversized_request_does_not_open_popup(self):
        """A request exceeding the transport limit is rejected without UI output."""
        self.app.keys.feed(F2)
        self.wire("B", 2)
        reader, _ = await self.client(b'{"padding":"' + b"x" * 4096 + b'"}')
        self.assertEqual(await self.reply(reader), b"cancel\n")
        self.assertEqual(self.focus, [])
        self.assertEqual(self.errors, [])

    async def test_popup_disconnect_cancels_focus_but_waits_for_primary(self):
        """Losing the native helper cancels its popup without early shell input."""
        self.app.keys.feed(F2)
        request = self.app.request
        self.wire("B", 2)
        _, writer = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.finished.get(), DEADLINE)
        self.assertEqual(self.focus, [True, False])
        self.assertEqual(self.results, [None])
        self.assertEqual(request.stage, "cancelled")
        self.app._native_input(b"after disconnect")
        self.assertEqual(self.app.session.data, F2)
        self.wire("P", 3)
        self.assertEqual(request.stage, "cancelled")
        self.assertEqual(self.app.session.data, F2 + b"after disconnect")
        self.assertEqual(self.errors, [])

    async def test_disconnected_waiter_cannot_later_acquire_focus(self):
        """A helper that exited before its B boundary is no longer a live grant."""
        self.app.keys.feed(F2)
        _, writer = await self.client({"nonce": NONCE, "serial": 2})
        writer.close()
        await writer.wait_closed()
        # Let the loop deliver the peer EOF; this is scheduling, not readiness.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.wire("B", 2)
        await asyncio.wait_for(self.finished.get(), DEADLINE)
        self.assertEqual(self.focus, [])
        self.assertEqual(self.errors, [])

    async def test_ui_failure_preserves_unsubmitted_native_escape_prefix(self):
        """A key prefix outside a popup remains native input when UI is retired."""
        self.app.keys.feed(b"\x1b[")
        self.assertEqual(self.app.session.data, b"")
        self.app.ui_enabled = False
        self.app._disable_ui()
        self.assertEqual(self.app.session.data, b"\x1b[")
        self.assertEqual(self.focus, [])


if __name__ == "__main__":
    unittest.main()
