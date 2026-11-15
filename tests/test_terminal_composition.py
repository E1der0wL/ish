"""Exercise the native grid through PTK's real layout, diff and input paths."""

import asyncio
import io
import os
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 5


@unittest.skipUnless(os.name == "posix", "Native VT and controlling PTY require POSIX")
class TerminalCompositionTests(unittest.IsolatedAsyncioTestCase):
    """Check emitted terminal bytes and final consumers, not renderer internals."""

    async def asyncSetUp(self):
        """Capture actual Vt100 output and provide an actual PTK pipe input."""
        from ish.terminal.app import TerminalApp

        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.stream = io.StringIO()
        self.outer_size = Size(rows=6, columns=12)
        self.output = Vt100_Output(
            self.stream,
            lambda: self.outer_size,
            term="xterm-256color",
            enable_cpr=False,
        )
        with create_app_session(input=self.pipe, output=self.output):
            with patch("ish.terminal.app.create_input", return_value=self.pipe):
                self.owner = TerminalApp("/bin/sh", ROOT)
        self.task = None
        self.render()

    async def asyncTearDown(self):
        """Restore PTK and reap only the child explicitly created by a test."""
        if self.task is not None:
            if self.owner.app.is_running and not self.owner.app.is_done:
                self.owner.app.exit()
            await asyncio.wait_for(self.task, DEADLINE)
        await self.owner.plugins.close()
        await self.owner.session.close()
        self.owner.vt.close()
        self.pipe_context.__exit__(None, None, None)

    def render(self):
        """Run PTK's public renderer and return only this presentation's bytes."""
        before = self.stream.tell()
        with set_app(self.owner.app):
            self.owner.app.renderer.render(self.owner.app, self.owner.app.layout)
        return self.stream.getvalue()[before:]

    def screen(self):
        """Interpret emitted text with an independent VT implementation."""
        import pyte

        screen = pyte.Screen(self.outer_size.columns, self.outer_size.rows)
        pyte.Stream(screen).feed(self.stream.getvalue())
        return screen

    async def start_input(self):
        """Wait for an actual PTK render before injecting user input."""
        ready = asyncio.Event()
        self.owner.app.after_render += lambda _: ready.set()
        self.task = asyncio.create_task(
            self.owner.app.run_async(set_exception_handler=False)
        )
        await asyncio.wait_for(ready.wait(), DEADLINE)

    def rich_host(self):
        """Select supported styles through the broker's actual version reply."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.owner.host.finish_probe()

    async def test_default_is_full_native_grid_without_surfaces_or_reserved_keys(self):
        """A bare application has no bar, popup, plugin binding or lost rows."""
        self.owner.vt.feed(b"\x1b[6;1HLAST ROW")
        self.render()
        self.assertEqual(self.owner.size, (12, 6))
        self.assertEqual(self.screen().display[5], "LAST ROW    ")
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertEqual(self.owner.plugins.bindings.bindings, [])

    async def test_popup_covering_either_half_of_wide_grapheme_restores_it(self):
        """An opaque float must never expose half a glyph or hide its own text."""
        for left, expected in ((1, "AX B"), (2, "A XB")):
            with self.subTest(left=left):
                self.owner.vt.feed("\x1b[2J\x1b[HA界B".encode())
                self.render()
                self.assertEqual(self.screen().display[0][:3], "A界B")
                content = Window(FormattedTextControl("X", focusable=True))
                popup = self.owner.plugins.show_popup(
                    content, left=left, top=0, width=1, height=1
                )
                self.render()
                self.assertEqual(self.screen().display[0][:4], expected)
                self.owner.plugins.close_popup(popup)
                self.render()
                self.assertEqual(self.screen().display[0][:3], "A界B")

    async def test_native_nbsp_does_not_acquire_editor_underline(self):
        """A literal NBSP keeps its native terminal meaning through PTK."""
        self.owner.vt.feed("A\u00a0B".encode())
        self.render()
        screen = self.screen()
        self.assertEqual(screen.buffer[0][1].data, "\u00a0")
        self.assertFalse(screen.buffer[0][1].underscore)

    async def test_hyperlink_only_change_is_presented_and_scoped(self):
        """Identical text with a different OSC 8 URI must still be redrawn."""
        self.rich_host()
        self.owner.vt.feed(b"\x1b]8;;https://old.example/\x1b\\X\x1b]8;;\x1b\\")
        self.render()
        self.owner.vt.feed(b"\r\x1b]8;;https://new.example/\x1b\\X\x1b]8;;\x1b\\")
        wire = self.render()
        self.assertIn("\x1b]8;;https://new.example/\x1b\\", wire)
        self.assertIn("X", wire)
        self.assertNotIn("https://old.example/", wire)
        self.assertGreater(wire.rfind("\x1b]8;;\x1b\\"), wire.index("X"))

    async def test_widget_underline_replaces_native_curly_underline(self):
        """PTK's ordinary underline is explicit after a native curly cell."""
        self.rich_host()
        self.owner.vt.feed(b"\x1b[4:3mN\x1b[0m")
        popup = self.owner.plugins.show_popup(
            Window(FormattedTextControl([("underline", "P")], focusable=True)),
            left=1,
            top=0,
            width=1,
            height=1,
        )
        wire = self.render()
        self.assertRegex(wire, re.escape("\x1b[4:3mN"))
        self.assertRegex(wire, re.escape("\x1b[4:1mP"))
        self.owner.plugins.close_popup(popup)

    async def test_popup_input_focus_results_and_stale_completion(self):
        """Real pipe keystrokes reach only the focused live popup or native queue."""
        self.owner.vt.feed(b"\x1b[?1004h")
        await self.start_input()
        first_editor = TextArea(multiline=False)
        first = self.owner.plugins.show_popup(first_editor, width=8, height=2)
        self.assertEqual(bytes(self.owner.session.pending_input), b"\x1b[O")
        received = asyncio.Event()
        first_editor.buffer.on_text_changed += lambda _: received.set()
        self.pipe.send_text("POPUP")
        await asyncio.wait_for(received.wait(), DEADLINE)
        self.assertEqual(first_editor.text, "POPUP")
        self.assertEqual(bytes(self.owner.session.pending_input), b"\x1b[O")
        second_editor = TextArea(multiline=False)
        second = self.owner.plugins.show_popup(second_editor, width=8, height=2)
        self.assertGreater(second.id, first.id)
        self.assertTrue(self.owner.plugins.close_popup(first, "accepted"))
        self.assertEqual(await first.result, "accepted")
        self.assertTrue(second.result.cancelled())
        third_editor = TextArea(multiline=False)
        third = self.owner.plugins.show_popup(third_editor, width=8, height=2)
        self.assertGreater(third.id, second.id)
        self.assertFalse(self.owner.plugins.close_popup(second, "late"))
        self.assertFalse(self.owner.plugins.close_popup(first, "duplicate"))
        self.assertIs(self.owner.app.layout.current_control, third_editor.control)
        self.assertFalse(third.result.done())
        self.owner.plugins.close_popup(third)
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)
        self.assertEqual(
            bytes(self.owner.session.pending_input),
            b"\x1b[O\x1b[O\x1b[I\x1b[O\x1b[I",
        )
        sent = asyncio.Event()
        send = self.owner.session.send

        def observe(data):
            send(data)
            if bytes(self.owner.session.pending_input).endswith(b"NATIVE"):
                sent.set()

        self.owner.session.send = observe
        self.pipe.send_text("NATIVE")
        await asyncio.wait_for(sent.wait(), DEADLINE)
        self.assertTrue(bytes(self.owner.session.pending_input).endswith(b"NATIVE"))
        self.assertNotIn(b"POPUP", self.owner.session.pending_input)

    async def test_opt_in_bar_changes_actual_pty_size_and_restores_full_grid(self):
        """The kernel's PTY dimensions match the opt-in bar's actual allocation."""
        import termios

        self.owner.session.argv = [
            sys.executable,
            "-c",
            "import signal; signal.pause()",
        ]
        self.owner.session.start(*self.owner.size)
        self.assertEqual(termios.tcgetwinsize(self.owner.session.fd), (6, 12))
        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")), rows=2)
        self.render()
        self.assertEqual(self.owner.size, (12, 4))
        self.assertEqual(termios.tcgetwinsize(self.owner.session.fd), (4, 12))
        self.assertEqual(self.screen().display[4][:3], "BAR")
        self.owner.plugins.set_bar()
        self.render()
        self.assertEqual(self.owner.size, (12, 6))
        self.assertEqual(termios.tcgetwinsize(self.owner.session.fd), (6, 12))
        self.assertEqual(self.screen().display[4], " " * 12)

    async def test_bar_always_leaves_a_native_row_when_outer_terminal_shrinks(self):
        """An oversized opt-in bar cannot replace the last native terminal row."""
        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")), rows=8)
        for rows in (3, 1, 6):
            with self.subTest(rows=rows):
                self.outer_size = Size(rows=rows, columns=12)
                self.owner.vt.feed(b"\x1b[2J\x1b[HNATIVE")
                self.render()
                self.assertEqual(self.owner.size, (12, 1))
                self.assertEqual(self.screen().display[0][:6], "NATIVE")
                if rows > 1:
                    self.assertEqual(self.screen().display[1][:3], "BAR")
        self.owner.plugins.set_bar()
        self.render()
        self.assertEqual(self.owner.size, (12, 6))

    async def test_shortcut_exception_diagnoses_and_closes_nested_popups(self):
        """A failing plugin shortcut restores terminal focus without a PTK crash."""
        invoked = asyncio.Event()
        opened = []

        @self.owner.plugins.bind("f2")
        def failing_shortcut(event):
            for _ in range(2):
                opened.append(
                    self.owner.plugins.show_popup(
                        TextArea(multiline=False), width=8, height=2
                    )
                )
            invoked.set()
            raise ValueError("shortcut failed")

        await self.start_input()
        self.pipe.send_text("\x1b[12~")
        await asyncio.wait_for(invoked.wait(), DEADLINE)
        self.assertEqual(self.owner.plugins.errors, ["ValueError: shortcut failed"])
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertIsNone(await opened[0].result)
        self.assertTrue(opened[1].result.cancelled())
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)
        self.assertFalse(self.task.done())
        self.assertEqual(bytes(self.owner.session.pending_input), b"")

    async def test_async_plugin_failure_diagnoses_and_restores_focus(self):
        """A background failure closes UI and is consumed by the plugin boundary."""
        await self.start_input()
        popup = self.owner.plugins.show_popup(
            TextArea(multiline=False), width=8, height=2
        )
        gate = asyncio.Event()

        async def failing_task():
            await gate.wait()
            raise RuntimeError("background failed")

        task = self.owner.plugins.create_task(failing_task())
        gate.set()
        self.assertIsNone(await asyncio.wait_for(task, DEADLINE))
        self.assertEqual(self.owner.plugins.errors, ["RuntimeError: background failed"])
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertIsNone(await popup.result)
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)
        self.assertFalse(self.task.done())

    async def test_async_shortcut_exception_uses_the_same_plugin_boundary(self):
        """A failure after awaiting in a bound key handler remains contained."""
        opened = asyncio.get_running_loop().create_future()
        gate = asyncio.Event()

        @self.owner.plugins.bind("f2")
        async def failing_shortcut(event):
            popup = self.owner.plugins.show_popup(
                TextArea(multiline=False), width=8, height=2
            )
            opened.set_result(popup)
            await gate.wait()
            raise LookupError("async shortcut failed")

        await self.start_input()
        self.pipe.send_text("\x1b[12~")
        popup = await asyncio.wait_for(opened, DEADLINE)
        gate.set()
        self.assertIsNone(await asyncio.wait_for(popup.result, DEADLINE))
        self.assertEqual(
            self.owner.plugins.errors, ["LookupError: async shortcut failed"]
        )
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)
        self.assertFalse(self.task.done())
        self.assertEqual(bytes(self.owner.session.pending_input), b"")

    async def test_pixel_mouse_survives_real_ptk_input_and_popup_release(self):
        """PTK's cell routing cannot discard the child's precise 1016 coordinates."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.owner.host.control("\x1b[6;20;10t")
        self.owner.host.finish_probe()
        self.owner.vt.resize(12, 6, cell_width=10, cell_height=20)
        self.owner.vt.feed(b"\x1b[?1003h\x1b[?1016h")
        with patch.object(self.owner, "_geometry", return_value=(12, 6, 120, 120)):
            await self.start_input()
            self.owner.app.renderer.enter_input_modes()
            received = asyncio.Event()
            send = self.owner.session.send

            def observe(data):
                send(data)
                if bytes(self.owner.session.pending_input).endswith(b"\x1b[<32;58;95M"):
                    received.set()

            self.owner.session.send = observe
            self.pipe.send_text("\x1b[<0;47;93M\x1b[<32;58;95M")
            await asyncio.wait_for(received.wait(), DEADLINE)
            self.assertEqual(
                bytes(self.owner.session.pending_input),
                b"\x1b[<0;47;93M\x1b[<32;58;95M",
            )
            popup = self.owner.plugins.show_popup(
                TextArea(multiline=False), width=8, height=2
            )
            self.assertEqual(
                bytes(self.owner.session.pending_input),
                b"\x1b[<0;47;93M\x1b[<32;58;95M\x1b[<0;58;95m",
            )
            self.owner.plugins.close_popup(popup)

    async def test_closing_popup_while_host_unfocused_never_claims_native_focus(self):
        """A popup cannot fabricate focus-in after the outer terminal lost focus."""
        self.owner.vt.feed(b"\x1b[?1004h")
        await self.start_input()
        editor = TextArea(multiline=False)
        popup = self.owner.plugins.show_popup(editor, width=8, height=2)
        edited = asyncio.Event()
        editor.buffer.on_text_changed += lambda _: edited.set()
        self.pipe.send_text("\x1b[OPOPUP")
        await asyncio.wait_for(edited.wait(), DEADLINE)
        self.assertFalse(self.owner.host_focused)
        self.assertEqual(editor.text, "POPUP")
        self.owner.plugins.close_popup(popup)
        self.assertNotIn(b"\x1b[I", self.owner.session.pending_input)
        self.assertNotIn(b"POPUP", self.owner.session.pending_input)
        received = asyncio.Event()
        send = self.owner.session.send

        def observe(data):
            send(data)
            if bytes(self.owner.session.pending_input).endswith(b"NATIVE"):
                received.set()

        self.owner.session.send = observe
        self.pipe.send_text("\x1b[INATIVE")
        await asyncio.wait_for(received.wait(), DEADLINE)
        self.assertTrue(self.owner.host_focused)
        self.assertTrue(
            bytes(self.owner.session.pending_input).endswith(b"\x1b[INATIVE")
        )
        self.assertEqual(bytes(self.owner.session.pending_input).count(b"\x1b[I"), 1)

    async def test_lost_pixel_geometry_releases_capture_once_before_popup(self):
        """Resize finishes native capture before removing the needed pixel scale."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.owner.host.control("\x1b[6;20;10t")
        self.owner.host.finish_probe()
        self.owner.vt.resize(12, 6, cell_width=10, cell_height=20)
        self.owner.vt.feed(b"\x1b[?1003h\x1b[?1016h")
        geometry = (12, 6, 120, 120)
        with patch.object(self.owner, "_geometry", side_effect=lambda: geometry):
            await self.start_input()
            self.owner.app.renderer.enter_input_modes()
            pressed = asyncio.Event()
            send = self.owner.session.send

            def observe(data):
                send(data)
                if data == b"\x1b[<0;47;93M":
                    pressed.set()

            self.owner.session.send = observe
            self.pipe.send_text("\x1b[<0;47;93M")
            await asyncio.wait_for(pressed.wait(), DEADLINE)
            geometry = (12, 6, 0, 0)
            self.owner.resize(12, 6)
            self.assertEqual(self.owner.vt.frame().cell_width, 0)
            editor = TextArea(multiline=False)
            popup = self.owner.plugins.show_popup(editor, width=8, height=2)
            edited = asyncio.Event()
            editor.buffer.on_text_changed += lambda _: edited.set()
            self.pipe.send_text("\x1b[<0;47;93mLATE")
            await asyncio.wait_for(edited.wait(), DEADLINE)
            self.assertEqual(editor.text, "LATE")
            self.owner.plugins.close_popup(popup)
            self.assertEqual(
                bytes(self.owner.session.pending_input),
                b"\x1b[<0;47;93M\x1b[<0;47;93m",
            )
            self.assertFalse(self.task.done())

    async def check_alt_key_popup_close(self, *, plugin_binding):
        """A split enhanced key retains one consumer across a popup transition."""
        self.rich_host()
        await self.start_input()
        self.owner.app.renderer.enter_input_modes()
        editor = TextArea(multiline=False)
        popup = self.owner.plugins.show_popup(editor, width=8, height=2)
        invocations = []

        def close(event):
            invocations.append(event.key_sequence[-1].key)
            self.owner.plugins.close_popup(popup)

        if plugin_binding:
            self.owner.plugins.bind("escape")(close)
        else:
            editor.control.key_bindings = KeyBindings()
            editor.control.key_bindings.add("escape", eager=True)(close)
        received = asyncio.Event()
        send = self.owner.session.send

        def observe(data):
            send(data)
            if data == b"Y":
                received.set()

        self.owner.session.send = observe
        # Alt-X is one physical enhanced key, although PTK exposes Escape + X.
        # Y is a separate key in the same read batch and must retain its own route.
        self.pipe.send_text("\x1b[120;3uY")
        await asyncio.wait_for(received.wait(), DEADLINE)
        self.assertEqual(len(invocations), 1)
        self.assertIsNone(await popup.result)
        self.assertEqual(editor.text, "")
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)
        self.assertEqual(bytes(self.owner.session.pending_input), b"Y")

    async def test_plugin_escape_close_consumes_complete_enhanced_alt_key(self):
        """A plugin Escape binding cannot leak the remaining Alt-X into stdin."""
        await self.check_alt_key_popup_close(plugin_binding=True)

    async def test_widget_escape_close_consumes_complete_enhanced_alt_key(self):
        """An ordinary PTK widget binding has the same complete-key guarantee."""
        await self.check_alt_key_popup_close(plugin_binding=False)

    async def check_alt_key_popup_open(self, *, mixed_prefix):
        """A consumed fallback cannot become input in a newly opened editor."""
        self.rich_host()
        await self.start_input()
        self.owner.app.renderer.enter_input_modes()
        editor = TextArea(multiline=False)
        popup = None
        combined_matches = []
        if mixed_prefix:
            editor.control.key_bindings = KeyBindings()

            @editor.control.key_bindings.add("x", "Y")
            def combined(event):
                combined_matches.append(event.data)
                editor.buffer.insert_text("WRONG")

        @self.owner.plugins.bind("escape")
        def open_popup(event):
            nonlocal popup
            popup = self.owner.plugins.show_popup(editor, width=8, height=2)

        expected = "YZ" if mixed_prefix else "Y"
        edited = asyncio.Event()

        def changed(_):
            if editor.text.endswith(expected):
                edited.set()

        editor.buffer.on_text_changed += changed
        # With the explicit x,Y binding PTK buffers a consumed x together with
        # unrelated Y. Z is already queued, so replaying Y at the back loses order.
        sequences = []
        call_handler = self.owner.app.key_processor._call_handler

        def observe_handler(handler, key_sequence):
            sequences.append(tuple(key.data for key in key_sequence))
            return call_handler(handler, key_sequence)

        with patch.object(
            self.owner.app.key_processor, "_call_handler", side_effect=observe_handler
        ):
            self.pipe.send_text("\x1b[120;3u" + expected)
            await asyncio.wait_for(edited.wait(), DEADLINE)
        self.assertEqual(editor.text, expected)
        self.assertEqual(combined_matches, [])
        if mixed_prefix:
            self.assertIn(("x", "Y"), sequences)
        self.assertEqual(bytes(self.owner.session.pending_input), b"")
        self.assertIs(self.owner.app.layout.current_control, editor.control)
        self.assertIsNotNone(popup)
        self.owner.plugins.close_popup(popup)

    async def test_plugin_escape_open_consumes_alt_remainder_before_popup_input(self):
        """Opening a popup on Alt's Escape keeps X out while accepting separate Y."""
        await self.check_alt_key_popup_open(mixed_prefix=False)

    async def test_consumed_alt_prefix_preserves_unrelated_buffered_keys_in_order(self):
        """Removing consumed X from an X,Y match must preserve subsequent Y,Z."""
        await self.check_alt_key_popup_open(mixed_prefix=True)

    async def test_failed_final_layout_still_restores_owned_host_metadata(self):
        """Final PTK drawing failure cannot retain child title, cwd or cursor color."""
        from prompt_toolkit.input.vt100_parser import Vt100Parser

        from ish.terminal.host import HostParser

        keys = []
        self.owner.host.begin_probe(12, 6)
        parser = HostParser(Vt100Parser(keys.append), self.owner.host)
        parser.feed(
            "\x1bP>|kitty(0.50.0)\x1b\\\x1b]21;cursor=\x1b\\\x1b[?2031;2$y\x1b[?997;1n"
        )
        for key in keys:
            self.owner.host.handle_control(key)
        self.owner.host.finish_probe()
        self.owner.launch_cwd = Path("/launch dir")
        ready = asyncio.Event()
        failed = False
        # PTK shrinks the base terminal's unconstrained preferred height to zero
        # on exit. An explicitly installed one-row plugin remains in that final
        # layout and can therefore fail during its actual create_content call.
        bar = FormattedTextControl("PLUGIN")
        self.owner.plugins.set_bar(Window(bar), rows=1)
        create_content = bar.create_content

        def content(width, height):
            if self.owner.app.renderer.finishing:
                raise RuntimeError("final layout failed")
            return create_content(width, height)

        def rendered(_):
            wire = self.stream.getvalue()
            if "CHILD-TITLE" in wire and "\x1b]12;rgb:aa/bb/cc\x1b\\" in wire:
                ready.set()

        async def start():
            self.owner._appearance(self.owner.profile.appearance)
            self.owner.app.renderer.enter_input_modes()
            self.owner._output(
                b"\x1b]2;CHILD-TITLE\x07\x1b]7;file:///child\x07"
                b"\x1b]12;#aabbcc\x07VISIBLE"
            )

        self.owner.app.after_render += rendered
        with (
            patch.object(self.owner, "_start", side_effect=start),
            patch.object(bar, "create_content", side_effect=content),
        ):
            runner = asyncio.create_task(self.owner.run())
            try:
                await asyncio.wait_for(ready.wait(), DEADLINE)
                self.owner.app.exit(result=0)
                with self.assertRaisesRegex(RuntimeError, "final layout failed"):
                    await asyncio.wait_for(runner, DEADLINE)
                failed = True
            finally:
                if not runner.done():
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
        self.assertTrue(failed)
        wire = self.stream.getvalue()
        restore_cwd = f"\x1b]7;file://{socket.gethostname()}/launch%20dir\x1b\\"
        for sequence in (
            "\x1b[23;2t",
            "\x1b[?2031l",
            "\x1b]21;cursor=\x1b\\",
            restore_cwd,
        ):
            self.assertEqual(wire.count(sequence), 1, repr(sequence))
        self.assertGreater(wire.index("\x1b[23;2t"), wire.index("CHILD-TITLE"))
        self.assertGreater(wire.index(restore_cwd), wire.index("file:///child"))
        self.assertTrue(self.owner.session.closed)


if __name__ == "__main__":
    unittest.main()
