"""Verify exact host key adapters and rejection without inventing input bytes."""

import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress, KeyProcessor
from prompt_toolkit.keys import Keys
from prompt_toolkit.widgets import TextArea

import test_terminal_composition as composition
from ish.terminal.app import InputProcessor
from ish.terminal.host import HostBroker, HostParser, StructuredKey

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 5
WIRE = "\x1b[9001~"
F13 = StructuredKey("f13", 57376)
READER = r"""import os, sys, tty
from pathlib import Path
tty.setraw(0)
root = Path(sys.argv[1])
(root / 'ready').write_text('ready')
data = b''
while not data.endswith(b'|done|'):
    data += os.read(0, 1024)
    (root / 'received').write_bytes(data)
(root / 'finished').write_text('done')
os.read(0, 1)
"""


class KeyMappingTests(unittest.TestCase):
    """Registration has one owner and cannot replace the built-in input contract."""

    def parser(self, broker=None):
        """Use the real bounded framer and unchanged PTK fallback parser."""
        broker = broker or HostBroker()
        keys = []
        return broker, HostParser(Vt100Parser(keys.append), broker), keys

    def test_registration_copies_values_and_does_not_add_ui_or_callbacks(self):
        """A caller cannot change the active map by mutating its source dictionary."""
        broker, parser, keys = self.parser()
        mappings = {WIRE: F13}
        broker.register_keys("sample", mappings)
        mappings[WIRE] = StructuredKey("f14", 57377)
        broker.key_mappings.clear()
        parser.feed(WIRE)
        self.assertEqual([key.structured_key for key in keys], [F13])
        self.assertEqual(broker.key_mappings, {"sample": 1})
        with self.assertRaises(FrozenInstanceError):
            F13.key = "f14"

    def test_reserved_keys_responses_and_protocols_cannot_be_mapped(self):
        """CPR/F3 ambiguity, native keys and negotiated protocols stay core-owned."""
        for sequence in (
            "\x1b[13;2~",
            "\x1b[25~",
            "\x1b[1;2R",
            "\x1b[9;9R",
            "\x1b[?2026;1$y",
            "\x1b[<0;3;5M",
            "\x1b[M !!",
            "\x1b[I",
            "\x1b[200~",
            "\x1b[201~",
            "\x1b[200;2~",
            "\x1b[57376u",
            "\x1b]52;c;abc\x07",
            "\x1bP1$r0m\x1b\\",
            "\x1bOQ",
            "x",
            "\x1b",
        ):
            with self.subTest(sequence=repr(sequence)):
                broker = HostBroker()
                with self.assertRaises(ValueError):
                    broker.register_keys("bad", {sequence: F13})
                self.assertEqual(broker.key_mappings, {})

    def test_invalid_target_and_collision_leave_registration_atomic(self):
        """Invalid later entries cannot partially claim a previously free sequence."""
        for event in (
            None,
            "f13",
            replace(F13, codepoint=57377),
            replace(F13, key="unknown"),
            replace(F13, modifiers=8),
            replace(F13, modifiers=True),
            replace(F13, action="repeat"),
            replace(F13, action="release"),
            replace(F13, text="injected"),
            replace(F13, shifted=65),
            replace(F13, base=65),
            replace(F13, release_events=True),
            StructuredKey("\x1b", 27),
            StructuredKey("x", 121),
        ):
            with self.subTest(event=event):
                broker = HostBroker()
                with self.assertRaises(ValueError):
                    broker.register_keys("bad", {WIRE: F13, "\x1b[9002~": event})
                broker.register_keys("good", {WIRE: F13})
                self.assertEqual(broker.key_mappings, {"good": 1})
                with self.assertRaises(ValueError):
                    broker.register_keys("collision", {WIRE: F13})
                with self.assertRaises(ValueError):
                    broker.register_keys("good", {"\x1b[9002~": F13})

    def test_registration_limits_and_first_input_seal(self):
        """Any first host input freezes registration permanently for this session."""
        for first in ("x", "\x1b", "\x1b[?2026;1$y"):
            broker, parser, _ = self.parser()
            parser.feed(first)
            parser.reset()
            with self.assertRaises(RuntimeError):
                broker.register_keys("late", {WIRE: F13})
        broker = HostBroker()
        for name, mappings in (("UPPER", {WIRE: F13}), ("empty", {}), ("bad", [])):
            with self.assertRaises(ValueError):
                broker.register_keys(name, mappings)
        with self.assertRaises(ValueError):
            broker.register_keys("many", {f"\x1b[{n}~": F13 for n in range(9000, 9257)})
        for number in range(16):
            broker.register_keys(f"key_{number}", {f"\x1b[{9000 + number}~": F13})
        with self.assertRaises(ValueError):
            broker.register_keys("extra", {"\x1b[9999~": F13})

    def test_fragmented_mapping_is_one_event_and_alt_is_delivered_once(self):
        """An incomplete adapter key survives flush; its Alt fallback shares a route."""
        event = StructuredKey("x", 120, 4)
        for split in range(2, len(WIRE)):
            broker, parser, keys = self.parser()
            broker.register_keys("alt", {WIRE: event})
            parser.feed(WIRE[:split])
            parser.flush()
            self.assertEqual(keys, [])
            parser.feed(WIRE[split:])
            parser.flush()
            self.assertEqual([key.key for key in keys], [Keys.Escape, "x"])
            consumed = []
            broker.on_key = lambda value, received=consumed: (
                received.append(value) or True
            )
            for key in keys:
                self.assertTrue(broker.handle_key(key))
            self.assertEqual(consumed, [event])
            self.assertFalse(consumed[0].release_events)

    def test_unknown_controls_cpr_text_paste_and_existing_keys_remain_distinct(self):
        """No unclaimed control parameters enter text; paste is intentionally opaque."""
        broker, parser, keys = self.parser()
        broker.register_keys("sample", {WIRE: F13})
        payload = WIRE + "\x1b[9999~한글"
        parser.feed("A\x1b[9999~\x1b[?66x\x1b[1;2R\x1b[97;5uB")
        parser.feed("\x1b[200~" + payload + "\x1b[201~")
        parser.feed("\x1b[[A\x1b[M !!\x1b[M !n\x1b[A\x1bx")
        parser.flush()
        self.assertEqual(
            [key.key for key in keys],
            [
                "A",
                Keys.CPRResponse,
                "B",
                Keys.BracketedPaste,
                Keys.F1,
                Keys.Vt100MouseEvent,
                Keys.Vt100MouseEvent,
                Keys.Up,
                Keys.Escape,
                "x",
            ],
        )
        self.assertEqual(keys[3].data, payload)
        self.assertEqual(broker.dropped_controls, 3)
        self.assertFalse(hasattr(keys[1], "structured_key"))

    def test_cancelled_or_restarted_controls_cannot_claim_a_mapping(self):
        """Explicit CAN/SUB/new Escape end the partial control without losing later text."""
        for ending in ("\x18", "\x1a"):
            broker, parser, keys = self.parser()
            broker.register_keys("sample", {WIRE: F13})
            parser.feed("\x1b[9001" + ending + "~A\x1b[9999\x1b[9001~B")
            parser.flush()
            self.assertEqual([key.key for key in keys], ["~", "A", Keys.F13, "B"])
            self.assertEqual(broker.dropped_controls, 2)

    def test_incomplete_and_oversized_csi_preserve_control_keys_and_following_text(
        self,
    ):
        """Ctrl+C/D, Enter and editing keys end an invalid prefix without becoming it."""
        for control in ("\x03", "\x04", "\r", "\n", "\x7f", "\x00", "\t"):
            for prefix in ("\x1b[9001", "\x1b[" + "9" * 65537):
                with self.subTest(control=repr(control), oversized=len(prefix) > 100):
                    broker, parser, keys = self.parser()
                    broker.register_keys("sample", {WIRE: F13})
                    parser.feed(prefix)
                    parser.flush()
                    self.assertEqual(keys, [])
                    parser.feed(control + "x")
                    parser.flush()
                    self.assertEqual([key.data for key in keys], [control, "x"])
                    self.assertTrue(
                        all(not hasattr(key, "structured_key") for key in keys)
                    )
                    self.assertEqual(broker.dropped_controls, 1)


@unittest.skipUnless(os.name == "posix", "Real native PTY/PTK input requires POSIX")
class KeyMappingPTYTests(unittest.IsolatedAsyncioTestCase):
    """Selected adapters share the existing focus routing and final PTY consumer."""

    async def asyncSetUp(self):
        """Reuse the actual native grid, PTK pipe and output fixture."""
        await composition.TerminalCompositionTests.asyncSetUp(self)

    async def asyncTearDown(self):
        """Stop selection timers and close the same input/application lifetime."""
        self.owner.selection.close()
        await composition.TerminalCompositionTests.asyncTearDown(self)

    def render(self):
        """Render through the existing PTK path, without a second output owner."""
        return composition.TerminalCompositionTests.render(self)

    async def start_input(self):
        """Wait for PTK to install its actual input reader."""
        await composition.TerminalCompositionTests.start_input(self)

    async def wait_for(self, predicate):
        """Wait for actual file/editor results; time is only the failure limit."""
        async with asyncio.timeout(DEADLINE):
            while not predicate():
                await asyncio.sleep(0.001)

    async def test_sample_plugin_keys_follow_popup_focus_and_actual_pty_consumer(self):
        """The same mapped X edits a popup or reaches stdin, never both."""
        self.owner.plugins.load(
            ("terminal_keys",), directory=ROOT / "examples/terminal_plugins"
        )
        self.owner.plugins.register_keys(
            "letter", {"\x1b[9002~": StructuredKey("x", 120)}
        )
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertEqual(self.owner.plugins.bindings.bindings, [])
        popup = None
        editor = TextArea(multiline=False)
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept(event):
            self.owner.plugins.close_popup(popup, editor.text)

        editor.control.key_bindings = bindings

        @self.owner.plugins.bind("f13")
        def open_popup(event):
            nonlocal popup
            popup = self.owner.plugins.show_popup(editor, width=8, height=2)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.owner.session.argv = [sys.executable, "-c", READER, str(root)]
            await self.start_input()
            self.owner.session.start(*self.owner.size)
            await self.wait_for(lambda: (root / "ready").exists())
            self.pipe.send_text("\x1b[9002~")
            await self.wait_for(lambda: (root / "received").exists())
            self.assertEqual((root / "received").read_bytes(), b"x")
            self.pipe.send_text(WIRE + "\x1b[1;2R\x1b[9002~한글")
            await self.wait_for(lambda: editor.text == "x한글")
            self.assertIs(self.owner.app.layout.current_control, editor.control)
            self.pipe.send_text("\r\x1b[9002~|done|")
            await self.wait_for(lambda: (root / "finished").exists())
            self.assertEqual(await popup.result, "x한글")
            self.assertEqual((root / "received").read_bytes(), b"xx|done|")
            self.assertEqual(self.owner.pressed_keys, {})
            self.assertEqual(self.owner.plugins.popups, [])

    async def test_terminal_shortcuts_do_not_claim_partial_modified_keys(self):
        """Enter/Escape bindings consume exact presses; modified input goes to stdin."""
        composition.TerminalCompositionTests.rich_host(self)
        handled = []
        for binding in ("enter", "escape"):
            self.owner.plugins.bind(binding)(
                lambda event, name=binding: handled.append(name)
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.owner.session.argv = [sys.executable, "-c", READER, str(root)]
            await self.start_input()
            self.owner.app.renderer.enter_input_modes()
            self.owner.session.start(*self.owner.size)
            await self.wait_for(lambda: (root / "ready").exists())
            self.pipe.send_text("\x1b[13;2u\x1b[120;3u\x1b[27u\r|done|")
            await self.wait_for(lambda: (root / "finished").exists())
            self.assertEqual((root / "received").read_bytes(), b"\r\x1bx|done|")
            self.assertEqual(handled, ["escape", "enter"])

    async def test_unknown_responses_never_leak_to_stdin_and_paste_keeps_native_policy(
        self,
    ):
        """Adapters ignore pasted controls; native paste keeps its existing sanitization."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.owner.session.argv = [sys.executable, "-c", READER, str(root)]
            await self.start_input()
            self.owner.session.start(*self.owner.size)
            await self.wait_for(lambda: (root / "ready").exists())
            self.pipe.send_text(
                "A\x1b[9001~\x1b[1;2R\x1b[97;5uB\x1b[200~\x1b[9001~한글\x1b[201~"
                "\x1b[9001\x03x\x04\r|done|"
            )
            await self.wait_for(lambda: (root / "finished").exists())
            # libghostty's existing paste policy replaces unsafe ESC with a
            # space. It must not become the adapter's F13 or ordinary typing.
            self.assertEqual(
                (root / "received").read_bytes(),
                "AB [9001~한글\x03x\x04\r|done|".encode(),
            )
            self.assertNotIn("\x1b[6n", self.stream.getvalue())


class InputProcessorResponseTests(unittest.IsolatedAsyncioTestCase):
    """Host cursor reports cannot consume real keys in a pending PTK prefix."""

    def processor(self):
        """Observe PTK's matched input sequence independently of response filtering."""
        observed = []
        bindings = KeyBindings()

        @bindings.add("x", Keys.Any)
        def pair(event):
            observed.append(tuple(key.data for key in event.key_sequence))

        @bindings.add(Keys.Any)
        def single(event):
            observed.append(tuple(key.data for key in event.key_sequence))

        owner = SimpleNamespace(
            mouse_captured=False,
            host=SimpleNamespace(
                key_consumed=lambda _: False, consume_key=lambda _: False
            ),
            clipboard=SimpleNamespace(cancel_paste=lambda: None),
            app=SimpleNamespace(layout=SimpleNamespace(has_focus=lambda _: True)),
            window=object(),
        )
        return InputProcessor(owner, KeyProcessor(bindings)), observed

    async def test_batched_cursor_report_preserves_adjacent_prefix_keys(self):
        """A response between X and Z leaves the complete X,Z binding intact."""
        processor, observed = self.processor()
        processor.feed_multiple(
            [
                KeyPress("x", "x"),
                KeyPress(Keys.CPRResponse, "\x1b[1;2R"),
                KeyPress("z", "z"),
            ]
        )
        processor.process_keys()
        self.assertEqual(observed, [("x", "z")])

    async def test_individual_cursor_report_preserves_pending_prefix(self):
        """A later report cannot terminate an earlier partially matched binding."""
        processor, observed = self.processor()
        for key in (
            KeyPress("x", "x"),
            KeyPress(Keys.CPRResponse, "\x1b[2;3R"),
            KeyPress("z", "z"),
        ):
            processor.feed(key)
            processor.process_keys()
        self.assertEqual(observed, [("x", "z")])

    async def test_reinserted_batch_filters_only_report_and_preserves_order(self):
        """First-insertion keeps the binding before previously queued ordinary input."""
        processor, observed = self.processor()
        processor.feed(KeyPress("q", "q"))
        processor.feed_multiple(
            [
                KeyPress("x", "x"),
                KeyPress(Keys.CPRResponse, "\x1b[3;4R"),
                KeyPress("z", "z"),
            ],
            first=True,
        )
        processor.feed(KeyPress(Keys.CPRResponse, "\x1b[5;6R"), first=True)
        processor.process_keys()
        self.assertEqual(observed, [("x", "z"), ("q",)])


if __name__ == "__main__":
    unittest.main()
