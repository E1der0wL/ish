"""Independent notification wire and native semantic-output contracts."""

import asyncio
import base64
import io
import os
import struct
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from ish.terminal.notifications import Notification, Notifications
from ish.terminal.protocol import Control, ProtocolStream
from ish.terminal.vt import Terminal


class NotificationTests(unittest.TestCase):
    """Check byte framing, correlation, limits and host-control sanitization."""

    def setUp(self):
        """Observe both presentation and child reply outputs independently."""
        self.published = []
        self.replies = []
        self.broker = Notifications(
            self.published.append, self.replies.append, enabled=True
        )
        self.native = []
        self.stream = ProtocolStream(
            self.native.append, self.broker.handle, cancel=self.broker.cancel
        )

    def feed(self, wire):
        """Exercise every record through arbitrarily fragmented framing."""
        for byte in wire:
            self.stream.feed(bytes((byte,)))

    def test_fragmented_title_body_and_correlated_query(self):
        """Only completed text publishes; a query preserves its request ID."""
        self.feed(b"\x1b]99;i=build-1:d=0;Build\x1b\\")
        self.assertEqual(self.published, [])
        self.feed("\x1b]99;i=build-1:p=body;완료\x1b\\".encode())
        self.feed(b"\x1b]99;i=query-7:p=?;\x1b\\")
        self.assertEqual(self.published, [Notification("Build", "완료")])
        self.assertEqual(
            self.replies, [b"\x1b]99;i=query-7:p=?;p=title,body:o=always\x1b\\"]
        )
        self.assertEqual(self.native, [])

    def test_off_consumes_known_protocol_without_reply_or_notification(self):
        """Disabling effects cannot re-enable a different native fallback."""
        self.broker.enabled = False
        self.feed(b"\x1b]99;;hidden\x07\x1b]99;i=q:p=?;\x07")
        self.broker.native(struct.pack("<I", 0) + b"hidden")
        self.assertEqual((self.published, self.replies, self.native), ([], [], []))

    def test_encoded_chunks_preserve_utf8_and_strip_host_controls(self):
        """Neither decoded ESC nor C1 may create another host command."""
        encoded = base64.b64encode("한\x1b]52;c;BAD\x07\n글\x9c".encode())
        for index, chunk in enumerate((encoded[:5], encoded[5:])):
            self.feed(
                b"\x1b]99;i=e:e=1:d=" + str(index).encode() + b";" + chunk + b"\x1b\\"
            )
        self.assertEqual(self.published, [Notification("한]52;c;BAD 글", "")])
        self.assertEqual(self.published[0].osc9(), "\x1b]9;ish: 한]52;c;BAD 글\x1b\\")

    def test_notification_text_cannot_become_an_osc9_subcommand(self):
        """Numeric cwd/progress prefixes remain data on hosts sharing OSC 9."""
        for text in ("4;1;100", "9;C:/unexpected", "1;999"):
            self.assertEqual(Notification(text, "").osc9(), f"\x1b]9;ish: {text}\x1b\\")

    def test_native_payloads_have_one_bounded_text_contract(self):
        """Corrupt or oversized callback payloads never reach the host."""
        self.broker.native(struct.pack("<I", 5) + b"TitleBody\x00\x1b\x07")
        self.broker.native(b"\xff")
        self.broker.native(struct.pack("<I", 99) + b"short")
        self.broker.native(struct.pack("<I", 0) + b"x" * 16385)
        self.assertEqual(self.published, [Notification("Title", "Body")])
        self.assertEqual(self.broker.rejected, 3)

    def test_unknown_extensions_and_invalid_identity_cannot_inject_replies(self):
        """Advertised capabilities do not invent activation or close support."""
        self.feed(b"\x1b]99;i=q:p=icon;abc\x07\x1b]99;i=q:p=alive;\x07")
        self.feed(b"\x1b]99;i=bad/id:p=?;\x07")
        self.assertEqual((self.published, self.replies), ([], []))
        self.assertFalse(self.broker.handle(Control("osc", b"777;notify;x;y", b"")))

    def test_chunk_overflow_does_not_publish_truncated_tail(self):
        """One rejected chunk invalidates the entire identified transfer."""
        self.feed(b"\x1b]99;i=q:d=0;" + b"x" * 2049 + b"\x07")
        self.feed(b"\x1b]99;i=q:p=body;tail\x07")
        self.assertEqual(self.published, [])
        self.assertEqual(self.broker.rejected, 1)

    def test_pending_capacity_fails_closed_and_cancel_recovers(self):
        """A bounded adapter never evicts a head and publishes its orphan tail."""
        for number in range(17):
            self.feed(b"\x1b]99;i=" + str(number).encode() + b":d=0;start\x07")
        self.feed(b"\x1b]99;i=0:p=body;tail\x07")
        self.assertEqual(self.published, [])
        self.broker.cancel()
        self.feed(b"\x1b]99;;fresh\x07")
        self.assertEqual(self.published, [Notification("fresh", "")])

    def test_body_only_no_padding_and_close_release_incomplete_payload(self):
        """Unpadded base64 is valid; closed adapters cannot resume old text."""
        self.feed(b"\x1b]99;i=q:p=body:e=1;YWJjZA\x07")
        self.assertEqual(self.published, [Notification("", "abcd")])
        self.feed(b"\x1b]99;i=incomplete:d=0;old\x07")
        self.broker.close()
        self.feed(b"\x1b]99;i=incomplete:p=body;later\x07")
        self.assertEqual(len(self.published), 1)


@unittest.skipUnless(os.name == "posix", "Native bridge requires POSIX")
class SemanticTests(unittest.TestCase):
    """Navigate and select native metadata without interpreting shell strings."""

    def setUp(self):
        """Create one real engine, capturing only its child responses."""
        self.replies = []
        self.vt = Terminal(20, 5, self.replies.append)

    def tearDown(self):
        """Release tracked native references with the terminal."""
        self.vt.close()

    def command(self, output):
        """Supply literal OSC 133 boundaries around arbitrary output bytes."""
        self.vt.feed(b"\x1b]133;A\x07$ \x1b]133;B\x07run\r\n\x1b]133;C\x07")
        self.vt.feed(output)
        self.vt.feed(b"\x1b]133;D;0\x07\x1b]133;A\x07$ \x1b]133;B\x07")

    def test_last_output_uses_native_metadata_and_excludes_prompt_input(self):
        """Copy Unicode output without shell prompt or command-line content."""
        self.command("한글\r\ntwo\r\n".encode())
        self.assertTrue(self.vt.select_last_output())
        self.assertEqual(self.vt.selection_text(), "한글\ntwo")
        self.assertEqual(self.replies, [])

    def test_no_markers_and_empty_command_do_not_select_unrelated_text(self):
        """Missing output boundaries must not guess a command from screen text."""
        self.vt.feed(b"ordinary text\r\n")
        self.assertFalse(self.vt.select_last_output())
        self.command(b"")
        self.assertFalse(self.vt.select_last_output())
        self.assertEqual(self.vt.selection_text(), "")

    def test_viewport_selection_selects_output_but_not_prompt(self):
        """Select by native semantic extent at an actual viewport cell."""
        self.command(b"result\r\n")
        self.assertTrue(self.vt.select_output(2, 1))
        self.assertEqual(self.vt.selection_text(), "result")
        self.assertFalse(self.vt.select_output(0, 2))
        self.assertEqual(self.vt.selection_text(), "")

    def test_reflow_keeps_native_output_anchor(self):
        """A wrapped command output remains one copied logical line."""
        self.command(b"abcdefghijklmnopqr\r\n")
        self.vt.resize(8, 5)
        self.assertTrue(self.vt.select_last_output())
        self.assertEqual(self.vt.selection_text(), "abcdefghijklmnopqr")

    def test_alternate_screen_cannot_reuse_primary_output_anchor(self):
        """Primary metadata does not authorize a different screen's selection."""
        self.command(b"primary output\r\n")
        self.vt.feed(b"\x1b[?1049hOTHER")
        self.assertFalse(self.vt.select_last_output())
        self.vt.feed(b"\x1b[?1049l")
        self.assertTrue(self.vt.select_last_output())
        self.assertEqual(self.vt.selection_text(), "primary output")
        self.vt.feed(b"\x1bcnew")
        self.assertFalse(self.vt.select_last_output())

    def test_history_erasure_and_pruning_never_select_new_unmarked_text(self):
        """Discarded anchors must not point into later unrelated grid content."""
        self.command(b"old\r\n")
        self.vt.feed(b"\r\n" * 50)
        self.vt.set_scrollback_limit(0, 0)
        self.vt.feed(b"new unmarked\r\n")
        self.assertFalse(self.vt.select_last_output())
        self.assertEqual(self.vt.selection_text(), "")

    def test_prompt_navigation_and_missing_markers_are_native_noops(self):
        """Native prompt movement clamps without adding a frontend history."""
        self.vt.feed(b"plain\r\n" * 15)
        before = self.vt.frame().viewport_offset
        self.vt.scroll_prompts(-1)
        self.assertEqual(self.vt.frame().viewport_offset, before)
        for number in range(4):
            self.command(f"result{number}\r\n".encode())
        self.vt.scroll_prompts(-1)
        self.assertFalse(self.vt.viewport_active)
        self.vt.scroll_prompts(100)
        self.assertTrue(self.vt.viewport_active)

    def test_native_notification_events_remain_ordered_and_bounded(self):
        """Native callbacks copy title/body once and bound each drain batch."""
        self.vt.feed(b"\x1b]9;first\x07\x1b]777;notify;title;body\x1b\\")
        published = []
        broker = Notifications(published.append, self.replies.append, enabled=True)
        for event in self.vt.drain_events():
            self.assertEqual(event.kind, "notification")
            broker.native(event.data)
        self.assertEqual(
            published, [Notification("", "first"), Notification("title", "body")]
        )
        self.vt.feed(b"\x1b]9;x\x07" * 100)
        self.assertEqual(len(self.vt.drain_events()), 64)
        self.vt.feed(b"\x1b]9;next\x07")
        self.assertEqual(len(self.vt.drain_events()), 1)

    def test_mixed_native_adapter_notifications_keep_wire_order(self):
        """Flush earlier native callbacks before an adapter publishes its text."""
        published = []
        broker = Notifications(published.append, self.replies.append, enabled=True)

        def drain():
            for effect in self.vt.drain_events():
                if effect.kind == "notification":
                    broker.native(effect.data)

        self.vt.set_notifications(broker, before=drain)
        self.vt.feed(
            b"\x1b]9;first\x07\x1b]99;;second\x07\x1b]777;notify;third;body\x07\x1b]99;;fourth\x07"
        )
        drain()
        self.assertEqual(
            published,
            [
                Notification("", "first"),
                Notification("second", ""),
                Notification("third", "body"),
                Notification("fourth", ""),
            ],
        )


class _Clipboard:
    """Record provider completion independently of PTK's internal clipboard."""

    def __init__(self):
        """Own one observable user-copy result."""
        self.writes = []
        self.done = asyncio.Event()

    async def read(self):
        """Provide the provider interface without requesting any OS clipboard."""
        return ""

    async def write(self, text):
        """Complete the actual broker write transaction."""
        self.writes.append(text)
        self.done.set()

    def close(self):
        """Require no external process or display cleanup."""

    async def aclose(self):
        """Complete the provider's asynchronous shutdown contract."""
        self.close()


@unittest.skipUnless(os.name == "posix", "Native VT and PTY require POSIX")
class OptionalCompositionTests(unittest.IsolatedAsyncioTestCase):
    """Use real PTK input, real PTY output and independent clipboard results."""

    ROOT = Path(__file__).resolve().parents[1]

    @asynccontextmanager
    async def terminal(self, *, notifications=False):
        """Run one plain terminal frontend without changing the user's host."""
        from ish.terminal.app import TerminalApp

        stream = io.StringIO()
        with create_pipe_input() as pipe:
            output = Vt100_Output(
                stream,
                lambda: Size(rows=8, columns=40),
                term="xterm-256color",
                enable_cpr=False,
            )
            with (
                create_app_session(input=pipe, output=output),
                patch("ish.terminal.app.create_input", return_value=pipe),
            ):
                owner = TerminalApp(
                    "/bin/sh",
                    self.ROOT,
                    features={
                        "desktop_notifications": "on" if notifications else "off"
                    },
                )
            owner.presentation_ready = True
            owner.host.begin_probe(40, 8)
            owner.host.finish_probe()
            ready = asyncio.Event()
            owner.app.after_render += lambda _: ready.set()
            task = asyncio.create_task(owner.app.run_async(set_exception_handler=False))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                yield owner, pipe, stream
            finally:
                if owner.app.is_running and not owner.app.is_done:
                    owner.app.exit()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    await owner.plugins.close()
                    await owner.clipboard.aclose()
                    await owner.session.close()
                    owner.vt.close()

    async def test_opt_in_command_keys_use_native_output_and_existing_clipboard(self):
        """F8/F9 act through the sample plugin while ordinary input reaches PTY."""
        async with self.terminal() as (owner, pipe, _), asyncio.timeout(5):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                owner.plugins.load(
                    ("terminal_commands",),
                    directory=self.ROOT / "examples/terminal_plugins",
                    library_dir=root / "libraries",
                )
                clipboard = _Clipboard()
                owner.plugins.register_clipboard("optional-copy", lambda: clipboard)
                result = root / "input"
                wire = "\x1b]133;A\x07$ \x1b]133;B\x07run\r\n\x1b]133;C\x07RESULT 한글\r\n\x1b]133;D;0\x07\x1b]133;A\x07$ \x1b]133;B\x07".encode()
                script = (
                    "import os, pathlib; os.write(1,"
                    + repr(wire)
                    + "); pathlib.Path("
                    + repr(str(result))
                    + ").write_text(input()+'\\n')"
                )
                received = bytearray()
                ready = asyncio.Event()

                def output(data):
                    owner._output(data)
                    received.extend(data)
                    if bytes(received).endswith(b"\x1b]133;B\x07"):
                        ready.set()

                owner.session.output = output
                owner.session.argv = [sys.executable, "-c", script]
                owner.session.start(*owner.size)
                await ready.wait()
                selected = asyncio.Event()
                owner.app.after_render += lambda _: (
                    selected.set()
                    if owner.vt.selection_text() == "RESULT 한글"
                    else None
                )
                pipe.send_bytes(b"\x1b[19~")
                await selected.wait()
                pipe.send_bytes(b"\x1b[20~")
                await clipboard.done.wait()
                self.assertEqual(clipboard.writes, ["RESULT 한글"])
                self.assertEqual(owner.app.clipboard.get_data().text, "RESULT 한글")
                pipe.send_bytes(b"typed\r")
                while not result.exists():
                    await asyncio.sleep(0.01)
                self.assertEqual(result.read_text(), "typed\n")
                self.assertEqual(owner.plugins.errors, [])

    async def test_default_has_no_reserved_command_keys_or_notification_output(self):
        """Without the sample plugin, F9 remains native input and effects stay off."""
        async with self.terminal() as (owner, pipe, stream), asyncio.timeout(5):
            owner._output(b"\x1b]9;hidden\x07\x1b]99;;hidden2\x07\x1b]99;i=q:p=?;\x07")
            pipe.send_bytes(b"\x1b[20~")
            while not owner.session.pending_input:
                await asyncio.sleep(0.01)
            self.assertEqual(bytes(owner.session.pending_input), b"\x1b[20~")
            self.assertNotIn("\x1b]9;", stream.getvalue())
            self.assertIsNone(owner.plugins.manager)
            self.assertEqual(owner.plugins.popups, [])

    async def test_notification_limit_and_correlated_query_use_existing_writers(self):
        """Rendering a bounded batch never echoes the child's original OSC 99."""
        async with self.terminal(notifications=True) as (owner, _, stream):
            wire = b"\x1b]99;;visible\x07" * 70 + b"\x1b]99;i=unique:p=?;\x07"
            owner._output(wire)
            self.assertEqual(owner.notification_drops, 6)
            self.assertEqual(
                bytes(owner.session.pending_input),
                b"\x1b]99;i=unique:p=?;p=title,body:o=always\x1b\\",
            )
            rendered = asyncio.Event()
            owner.app.after_render += lambda _: rendered.set()
            owner.app.invalidate()
            await asyncio.wait_for(rendered.wait(), 5)
            self.assertEqual(stream.getvalue().count("\x1b]9;ish: visible\x1b\\"), 64)
            self.assertNotIn("\x1b]99;", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
