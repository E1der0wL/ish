"""Exercise popup grants over real Unix sockets and literal shell wire records."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from ish.stream.app import App
from ish.stream.panel import Panel
from ish.stream.protocol import Keys, Stream

NONCE = "socket-test-session-0123456789abcdef"
F2 = b"\x1bOQ"
DEADLINE = 3


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
        self.app = App(["tcsh"], Path(self.directory.name), demo=True)
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
            self.app._paint, on_focus=focus, on_result=self.results.append
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
        self.assertEqual(self.app.session.data, F2)
        self.assertEqual(self.focus, [])
        self.wire("H", 2)
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
        reader, _ = await self.client({"nonce": NONCE, "serial": 2})
        self.assertEqual(self.focus, [])
        self.wire("B", 2)
        await self.focused(True)
        self.app._native_input(b" popup\r")
        self.assertEqual(await self.reply(reader), b"done\n")
        self.assertEqual(self.results, ["inside popup"])
        self.assertEqual(self.app.session.data, F2)
        self.app._native_input(b"after popup")
        self.assertEqual(self.app.session.data, F2)
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
        self.app._native_input(b"\x03")
        self.assertEqual(await self.reply(reader), b"done\n")
        self.assertEqual(self.results, [None])

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
        self.wire("B", 2)
        _, writer = await self.client({"nonce": NONCE, "serial": 2})
        await self.focused(True)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.finished.get(), DEADLINE)
        self.assertEqual(self.focus, [True, False])
        self.assertEqual(self.results, [None])
        self.app._native_input(b"after disconnect")
        self.assertEqual(self.app.session.data, F2)
        self.wire("P", 3)
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


if __name__ == "__main__":
    unittest.main()
