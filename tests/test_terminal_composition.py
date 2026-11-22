"""Exercise the native grid through PTK's real layout, diff and input paths."""

import asyncio
import inspect
import io
import os
import re
import signal
import socket
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 5


@unittest.skipUnless(os.name == "posix", "Native VT and controlling PTY require POSIX")
class TerminalCompositionTests(unittest.IsolatedAsyncioTestCase):
    """Check emitted terminal bytes and final consumers, not renderer internals."""

    async def asyncSetUp(self, *, features=None):
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
                self.owner = TerminalApp("/bin/sh", ROOT, features=features)
        # This fixture exercises composition after optional startup observation.
        self.owner.presentation_ready = True
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

    async def test_termination_during_suspend_handoff_does_not_stop_the_process(self):
        """A queued stop cannot overtake termination while PTK hands off input."""
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def delayed_handoff():
            entered.set()
            await release.wait()
            yield

        with (
            patch("ish.terminal.app.in_terminal", delayed_handoff),
            patch("ish.terminal.app.os.kill") as kill,
        ):
            task = asyncio.create_task(self.owner._suspend())
            await asyncio.wait_for(entered.wait(), DEADLINE)
            self.owner.stop(signal.SIGTERM)
            release.set()
            await asyncio.wait_for(task, DEADLINE)
            kill.assert_not_called()

    async def test_repeated_reset_does_not_overwrite_restored_cursor_modes(self):
        """The first cleanup restores hidden/blinking state; later resets are inert."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1b[?25;2$y")
        self.owner.host.control("\x1b[?12;1$y")
        self.owner.host.finish_probe()
        self.render()
        self.owner.app.renderer.reset()
        first = self.stream.getvalue()
        self.assertIn("\x1b[?25l", first)
        self.assertIn("\x1b[?12h", first)
        self.owner.app.renderer.reset()
        self.output.flush()
        self.assertEqual(self.stream.getvalue(), first)

    def render(self):
        """Run PTK's public renderer and return only this presentation's bytes."""
        before = self.stream.tell()
        with set_app(self.owner.app):
            self.owner.app.renderer.render(self.owner.app, self.owner.app.layout)
        return self.stream.getvalue()[before:]

    async def test_changing_rgb_cells_bound_style_history_and_preserve_output(self):
        """A stream of fresh foregrounds and backgrounds can revisit an evicted style."""
        from prompt_toolkit.output import vt100

        renderer = self.owner.app.renderer
        for depth, red in (
            (ColorDepth.DEPTH_4_BIT, "91"),
            (ColorDepth.DEPTH_8_BIT, "38;5;196"),
            (ColorDepth.DEPTH_24_BIT, "38;2;255;0;0"),
        ):
            with (
                self.subTest(depth=depth),
                patch.object(type(self.owner.app), "color_depth", depth),
                patch.object(renderer, "MIN_STYLE_CACHE_SIZE", 8),
            ):
                first = b"\x1b[H\x1b[0;38;2;255;0;0mR\x1b[0mZ"
                self.owner.vt.feed(first)
                self.render()
                for frame in range(32):
                    body = bytearray(b"\x1b[H")
                    for col in range(10):
                        value = frame * 10 + col
                        lo, hi = value % 256, value // 256
                        body.extend(
                            f"\x1b[38;2;67;{hi};{lo};48;2;{lo};{hi};31m".encode()
                        )
                        body.extend(b" " if col % 2 else b"X")
                    self.owner.vt.feed(bytes(body))
                    self.render()
                limit = self.outer_size.rows * self.outer_size.columns * 2
                for cache in (
                    renderer._attrs_for_style,
                    renderer._style_string_has_style,
                    self.output._escape_code_caches[depth],
                    vt100._16_fg_colors._cache,
                    vt100._16_bg_colors._cache,
                    vt100._256_colors,
                ):
                    self.assertLessEqual(len(cache), limit)
                self.owner.vt.feed(first)
                wire = self.render()
                self.assertRegex(wire.split("R", 1)[0], rf"\x1b\[(?:0;)?{red}m")
                self.assertIn("\x1b[0m", wire.split("R", 1)[1].split("Z", 1)[0])
                self.assertEqual(self.screen().display[0][:2], "RZ")
        renderer.reset()
        self.assertIsNone(renderer._attrs_for_style)
        self.assertIsNone(renderer._style_string_has_style)

    async def test_indexed_output_preserves_observed_custom_palette_slots(self):
        """A theme red and a custom upper slot must reach the host unchanged."""
        host = self.owner.host
        palette = [None] * 256
        palette[1], palette[12], palette[201] = (
            (191, 97, 108),
            (18, 35, 52),
            (49, 67, 87),
        )
        appearance = replace(host.profile.appearance, palette=tuple(palette))
        host.profile = replace(host.profile, appearance=appearance)
        self.owner._appearance(appearance)
        for depth, background, expected in (
            (ColorDepth.DEPTH_4_BIT, 12, "\x1b[31;104m"),
            (ColorDepth.DEPTH_8_BIT, 201, "\x1b[31;48;5;201m"),
        ):
            with (
                self.subTest(depth=depth),
                patch.object(type(self.owner.app), "color_depth", depth),
            ):
                self.owner.vt.feed(f"\r\x1b[3;31;48;5;{background}mX\x1b[0mY".encode())
                self.owner.app.renderer.invalidate_screen()
                wire = self.render()
                before, after = wire.split("X", 1)
                self.assertIn(expected, before)
                self.assertIn("\x1b[0m", after.split("Y", 1)[0])

        # The standard xterm approximation for #6699cc is 68. Only a complete
        # host table can improve on this without guessing any missing slot.
        for complete, neighbor, expected in (
            (False, 67, "38;5;68m"),
            (True, 67, "38;5;67m"),
            (True, 72, "38;5;72m"),
        ):
            with (
                self.subTest(complete=complete, neighbor=neighbor),
                patch.object(
                    type(self.owner.app), "color_depth", ColorDepth.DEPTH_8_BIT
                ),
            ):
                colors = [(0, 0, 0) if complete else None] * 256
                colors[neighbor] = (102, 153, 205)
                colors[68] = (255, 0, 255)
                appearance = replace(host.profile.appearance, palette=tuple(colors))
                host.profile = replace(host.profile, appearance=appearance)
                self.owner._appearance(appearance)
                self.owner.vt.feed(b"\r\x1b[38;2;102;153;204mZ\x1b[0m")
                self.assertIn(expected, self.render().split("Z", 1)[0])

        # Both near-white colors would map to slot 1. Preserve a contrasting
        # second choice at 4 bits, including when the background is exact.
        colors = [(0, 0, 0)] * 16 + [None] * 240
        colors[1], colors[2] = (255, 255, 255), (180, 180, 180)
        appearance = replace(host.profile.appearance, palette=tuple(colors))
        host.profile = replace(host.profile, appearance=appearance)
        self.owner._appearance(appearance)
        for fg, bg, expected in (
            (253, 254, "31;42m"),
            (253, 255, "32;41m"),
            (255, 254, "31;42m"),
            (253, 253, "31;41m"),
        ):
            with (
                self.subTest(fg=fg, bg=bg),
                patch.object(
                    type(self.owner.app), "color_depth", ColorDepth.DEPTH_4_BIT
                ),
            ):
                self.owner.vt.feed(
                    f"\r\x1b[38;2;{fg};{fg};{fg};48;2;{bg};{bg};{bg}mZ\x1b[0m".encode()
                )
                self.owner.app.renderer.invalidate_screen()
                self.assertIn(expected, self.render().split("Z", 1)[0])

    async def test_truecolor_preserves_palette_identity_and_local_overrides(self):
        """Identical RGBs in separate slots and literal colors retain their meaning."""
        host = self.owner.host
        palette = [None] * 256
        palette[1] = palette[201] = (191, 97, 108)
        appearance = replace(host.profile.appearance, palette=tuple(palette))
        host.profile = replace(host.profile, appearance=appearance)
        self.owner._appearance(appearance)
        with patch.object(type(self.owner.app), "color_depth", ColorDepth.DEPTH_24_BIT):
            for setup, sgr, expected in (
                (b"", b"1;31;48;5;201", "\x1b[31;48;5;201m"),
                (b"", b"38;5;201;41", "\x1b[38;5;201;41m"),
                (b"", b"1;38;2;191;97;108", "38;2;191;97;108"),
                (b"\x1b]4;1;#123456\x1b\\", b"31", "38;2;18;52;86"),
                (b"\x1b]104;1\x1b\\", b"31", "\x1b[31m"),
            ):
                with self.subTest(sgr=sgr, setup=setup):
                    self.owner.vt.feed(setup + b"\r\x1b[0;" + sgr + b"mZ\x1b[0m")
                    self.owner.app.renderer.invalidate_screen()
                    prefix = self.render().split("Z", 1)[0]
                    self.assertIn(expected, prefix)
                    if sgr.startswith(b"1;"):
                        self.assertTrue(self.screen().buffer[0][0].bold)

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
        """Observe input protocol support without requiring a terminal identity."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1b[?0u")
        self.owner.host.finish_probe()

    async def test_default_is_full_native_grid_without_surfaces_or_reserved_keys(self):
        """A bare application has no bar, popup, plugin binding or lost rows."""
        self.owner.vt.feed(b"\x1b[6;1HLAST ROW")
        self.render()
        self.assertEqual(self.owner.size, (12, 6))
        self.assertEqual(self.screen().display[5], "LAST ROW    ")
        self.assertEqual(self.owner.plugins.popups, [])
        self.assertEqual(self.owner.plugins.bindings.bindings, [])

    async def test_resize_burst_repaints_at_origin_without_flushing_a_blank(self):
        """Even a shrink back to the old size requires one anchored, populated paint."""
        self.owner.vt.feed(b"\x1b[?1049h\x1b[HEDITOR\x1b[5;9HZ")
        await self.start_input()
        ready = asyncio.Event()
        self.owner.app.after_render += lambda _: ready.set()
        writes = []
        with patch.object(self.stream, "write", side_effect=lambda s: writes.append(s)):
            for final in (Size(rows=10, columns=40), Size(rows=10, columns=40)):
                writes.clear()
                ready.clear()
                self.outer_size = Size(rows=3, columns=7)
                self.owner.app._on_resize()
                # PTY output or an animation may request a paint during a drag.
                # It must not send the transient dimensions to the child.
                self.owner.app._redraw()
                self.assertEqual(writes, [])
                self.outer_size = final
                self.owner.app._on_resize()
                await asyncio.wait_for(ready.wait(), DEADLINE)
                paints = [chunk for chunk in writes if "\x1b[J" in chunk]
                self.assertEqual(len(paints), 1, writes)
                self.assertIn("EDITOR", paints[0])
                self.assertIn("\x1b[1;1H", paints[0][: paints[0].index("EDITOR")])
                for reset in ("\x1b[?1049", "\x1b[?1004l", "\x1b[?2004l"):
                    self.assertNotIn(reset, "".join(writes))

    async def test_silent_alternate_screen_resizes_with_its_bar_without_child_output(
        self,
    ):
        """A static full-screen program can keep its pixels and never answer SIGWINCH."""
        import termios

        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")), rows=1)
        self.owner.session.argv = [
            sys.executable,
            "-c",
            "import os; os.write(1, b'\\x1b[?1049h\\x1b[HSTATIC'); input()",
        ]
        await self.start_input()
        self.owner.session.start(*self.owner.size)
        painted = asyncio.Event()
        self.owner.app.after_render += lambda _: painted.set()
        async with asyncio.timeout(DEADLINE):
            while "STATIC" not in self.stream.getvalue():
                painted.clear()
                await painted.wait()
            start = self.stream.tell()
            self.outer_size = Size(rows=10, columns=40)
            self.owner.app._on_resize()
            while "BAR" not in self.stream.getvalue()[start:]:
                painted.clear()
                await painted.wait()
        self.assertEqual(termios.tcgetwinsize(self.owner.session.fd), (9, 40))
        self.assertTrue(self.screen().display[0].startswith("STATIC"))
        self.assertTrue(self.screen().display[-1].startswith("BAR"))

    async def test_drag_publishes_child_frame_before_advancing_to_next_size(self):
        """Observe an actual PTY reply when a newer host resize overtook it."""
        import termios

        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "go"
            self.owner.session.argv = [
                sys.executable,
                "-c",
                "import os,sys,time,pathlib\n"
                "os.write(1,b'\\x1b[?1049h\\x1b[HSTART')\n"
                "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.002)\n"
                "last = None\n"
                "while True:\n"
                " size = os.get_terminal_size(1)\n"
                " if size != last:\n"
                "  os.write(1, f'\\x1b[HFRAME{size.columns}x{size.lines}'.encode()); last = size\n"
                " time.sleep(.002)\n",
                str(gate),
            ]
            await self.start_input()
            frames = []
            self.owner.app.after_render += lambda _: frames.append(
                self.screen().display[0].rstrip()
            )
            self.owner.session.start(*self.owner.size)
            async with asyncio.timeout(DEADLINE):
                while "START" not in frames:
                    await asyncio.sleep(0.002)
                self.outer_size = Size(rows=10, columns=30)
                self.owner.app._on_resize()
                # The first loop turn delivers the resize even without a PTK invalidate.
                await asyncio.sleep(0)
                self.assertEqual(termios.tcgetwinsize(self.owner.session.fd), (10, 30))
                for columns in (34, 36, 40):
                    self.outer_size = Size(rows=12, columns=columns)
                    self.owner.app._on_resize()
                    await asyncio.sleep(0)
                    self.assertEqual(
                        termios.tcgetwinsize(self.owner.session.fd), (10, 30)
                    )
                gate.touch()
                while "FRAME40x12" not in frames:
                    await asyncio.sleep(0.002)
            self.assertLess(frames.index("FRAME30x10"), frames.index("FRAME40x12"))
            self.assertFalse({"FRAME34x12", "FRAME36x12"}.intersection(frames))
            self.assertTrue(self.screen().display[0].startswith("FRAME40x12"))

    async def test_resize_collects_split_pty_paints_and_honors_a_frame_end(self):
        """Inspect successive real PTK frames while the child waits between writes."""
        self.owner.session.argv = [
            sys.executable,
            "-c",
            "import os, time, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'\\x1b[?1049hREADY')\n"
            "for columns, rows in ((12, 9), (30, 10), (40, 10)):\n"
            " while os.get_terminal_size() != (columns, rows): time.sleep(.001)\n"
            " os.write(1, b'\\x1b[2J\\x1b[HPARTIAL')\n"
            " os.read(0, 1)\n"
            " final = f'\\x1b[HEDGE-{columns} '.encode()\n"
            " if columns == 40: final = b'\\x1b[?2026h'+final+b'\\x1b[?2026l'\n"
            " os.write(1, final)\n"
            "os.read(0, 1)\n",
        ]
        await self.start_input()
        frames = []
        self.owner.app.after_render += lambda _: frames.append(
            self.screen().display[0].strip()
        )
        partial = asyncio.Event()
        output = self.owner.session.output

        def receive(data):
            output(data)
            if b"PARTIAL" in data:
                partial.set()

        self.owner.session.output = receive
        self.owner.session.start(*self.owner.size)
        async with asyncio.timeout(DEADLINE):
            while "READY" not in frames:
                await asyncio.sleep(0.002)
            # The long last interval can only pass if the child's explicit
            # frame end presents immediately, even after an unframed prefix.
            for columns, rows, interval, expected in (
                (12, 9, 0.1, ["7;1;9;12"]),
                (30, 10, 0.1, ["1;13;9;30", "10;1;10;30"]),
                (40, 10, DEADLINE * 2, ["1;31;10;40"]),
            ):
                with patch.object(self.owner.app, "RESIZE_BURST_SECONDS", interval):
                    partial.clear()
                    start = self.stream.tell()
                    self.outer_size = Size(rows=rows, columns=columns)
                    self.owner.app._on_resize()
                    await partial.wait()
                    rectangles = re.findall(
                        r"\x1b\[([0-9;]+)\$z", self.stream.getvalue()[start:]
                    )
                    self.assertEqual(rectangles, expected)
                    self.owner.app.invalidate()
                    await asyncio.sleep(0.005)
                    self.owner.session.send(b"g")
                    while f"EDGE-{columns}" not in frames:
                        await asyncio.sleep(0.002)
        self.assertFalse(any("PARTIAL" in row for row in frames), frames)

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

    async def test_contiguous_text_shares_format_until_ptk_resets_it(self):
        """Keep adjacent glyphs together and restore decorations after SGR changes."""
        from ish.terminal.vt import Terminal

        self.rich_host()
        link = "\x1b]8;;https://example.test/run\x1b\\"
        close_link = "\x1b]8;;\x1b\\"
        data = f"\x1b[4:3;53m{link}A한B\x1b[1mCDE\r\nFGH{close_link}\x1b[0mJKL"
        self.owner.vt.feed(data.encode())
        wire = self.render()
        for run in ("A한B", "CDE", "FGH", "JKL"):
            self.assertIn(run, wire)
        # Read the emitted stream as a receiving terminal, including PTK's
        # attribute reset when switching to bold or moving to the second row.
        with Terminal(12, 6, lambda _: None) as host:
            host.feed(self.stream.getvalue().encode())
            first, second = host.frame().cells[:2]
            for cell in (*first[:6], *second[:3]):
                self.assertEqual(cell.hyperlink, "https://example.test/run")
                self.assertEqual(cell.underline, 3)
                self.assertTrue(cell.overline)
            for cell in second[3:6]:
                self.assertIsNone(cell.hyperlink)
                self.assertEqual(cell.underline, 0)
                self.assertFalse(cell.overline)

    async def test_widget_underline_replaces_native_curly_underline(self):
        """PTK's ordinary underline is explicit after a native curly cell."""
        self.rich_host()
        self.owner.vt.feed(b"\x1b[4:3mNN\x1b[0m")
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
        self.assertIn("\x1b[4:3mN", self.render())

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

    async def test_installed_bar_renders_its_configured_text(self):
        """An external package reads a typed setting and paints it in the public UI."""
        self.owner.plugin_settings = {"terminal_bar": {"label": "MY BAR"}}
        self.owner.plugins.load(
            ("terminal_bar",), directory=ROOT / "examples/terminal_plugins"
        )
        self.render()
        self.assertEqual(self.screen().display[-1].strip(), "MY BAR")

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

    async def test_retired_popup_task_cannot_close_a_new_popup(self):
        """A late task keeps its diagnostic without changing another editor's result."""
        await self.start_input()
        first = self.owner.plugins.show_popup(
            TextArea(multiline=False), width=8, height=2
        )
        gate = asyncio.Event()

        async def fail():
            await gate.wait()
            raise ValueError("retired lookup failed")

        task = self.owner.plugins.create_task(fail())
        self.owner.plugins.close_popup(first, "finished")
        editor = TextArea(multiline=False)
        second = self.owner.plugins.show_popup(editor, width=8, height=2)
        gate.set()
        await asyncio.wait_for(task, DEADLINE)
        self.assertEqual(await first.result, "finished")
        self.assertFalse(second.result.done())
        self.assertIs(self.owner.app.layout.current_control, editor.control)
        self.assertEqual(
            self.owner.plugins.errors, ["ValueError: retired lookup failed"]
        )
        self.owner.plugins.close_popup(second)

    async def test_async_shortcut_descendant_retains_its_original_popup(self):
        """Returning from a handler cannot let its descendant cancel a later editor."""
        await self.start_input()
        gate = asyncio.Event()
        opened = asyncio.get_running_loop().create_future()

        async def fail():
            await gate.wait()
            raise RuntimeError("retired descendant failed")

        @self.owner.plugins.bind("f2")
        async def open_popup(event):
            first = self.owner.plugins.show_popup(
                TextArea(multiline=False), width=8, height=2
            )
            task = self.owner.plugins.create_task(fail())
            opened.set_result((first, task))

        self.pipe.send_text("\x1bOQ")
        first, task = await asyncio.wait_for(opened, DEADLINE)
        self.owner.plugins.close_popup(first)
        editor = TextArea(multiline=False)
        second = self.owner.plugins.show_popup(editor, width=8, height=2)
        gate.set()
        await asyncio.wait_for(task, DEADLINE)
        self.assertFalse(second.result.done())
        self.assertIs(self.owner.app.layout.current_control, editor.control)
        self.assertEqual(
            self.owner.plugins.errors, ["RuntimeError: retired descendant failed"]
        )
        self.owner.plugins.close_popup(second)

    async def test_task_cancelled_before_first_turn_closes_its_coroutine(self):
        """Immediate shutdown must not leak a supplied, never-awaited coroutine."""

        async def operation():
            raise AssertionError("A cancelled operation must never start")

        coroutine = operation()
        task = self.owner.plugins.create_task(coroutine)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

    async def test_default_plugins_do_not_create_a_manager_or_directories(self):
        """Selecting no plugin leaves the existing shared manager completely unused."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ish.plugin.manager.PluginManager") as manager:
                self.owner.plugins.load(
                    [], directory=root / "scripts", library_dir=root / "libraries"
                )
            manager.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(self.owner.plugins.bindings.bindings, [])

    async def test_selected_plugins_load_once_and_await_cleanup(self):
        """Names select existing PluginManager packages; unselected code never executes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("selected", "unselected"):
                package = root / "scripts" / name
                package.mkdir(parents=True)
                source = (
                    'PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}\n'
                    "TERMINAL_API = 1\n"
                    "import asyncio\n"
                    "calls = []\n"
                    "def setup(api):\n"
                    '    calls.append("setup")\n'
                    "    async def cleanup():\n"
                    "        await asyncio.sleep(0)\n"
                    '        calls.append("cleanup")\n'
                    "    return cleanup\n"
                )
                if name == "unselected":
                    source += 'raise AssertionError("Unselected plugin was imported")\n'
                (package / (name + ".py")).write_text(source)
            self.owner.plugins.load(
                ["selected", "selected"],
                directory=root / "scripts",
                library_dir=root / "libraries",
            )
            selected = self.owner.plugins.get("selected")
            self.assertEqual(selected.calls, ["setup"])
            self.assertIsNone(self.owner.plugins.get("unselected"))
            await self.owner.plugins.close()
            self.assertEqual(selected.calls, ["setup", "cleanup"])
            await self.owner.plugins.close()
            self.assertEqual(selected.calls, ["setup", "cleanup"])

    async def test_wrong_plugin_api_is_rejected_before_setup(self):
        """An incompatible plugin cannot install keys or side effects in this UI."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "bad_api"
            package.mkdir()
            marker = root / "setup-ran"
            (package / "bad_api.py").write_text(
                'PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}\n'
                "TERMINAL_API = 999\n"
                "from pathlib import Path\n"
                f"def setup(api):\n    Path({str(marker)!r}).touch()\n"
            )
            with self.assertRaises(ValueError):
                self.owner.plugins.load(
                    ["bad_api"], directory=root, library_dir=root / "libraries"
                )
            self.assertFalse(marker.exists())
            self.assertEqual(self.owner.plugins.bindings.bindings, [])

    async def test_shipped_popup_accept_cancel_and_global_toggle(self):
        """The shipped plugins use real PTK input without leaking typed text to stdin."""
        with tempfile.TemporaryDirectory() as directory:
            self.owner.plugins.load(
                ["terminal_bar", "terminal_popup"],
                directory=ROOT / "examples" / "terminal_plugins",
                library_dir=Path(directory) / "libraries",
            )
            opened = asyncio.Event()

            def rendered(_):
                if self.owner.plugins.popups:
                    opened.set()

            self.owner.app.after_render += rendered
            await self.start_input()
            self.assertEqual(self.owner.size, (12, 5))
            for close_key, expected in (
                ("\r", "sample 한 글"),
                ("\x1b", None),
                ("\x1bOQ", None),
            ):
                with self.subTest(close_key=repr(close_key)):
                    opened.clear()
                    self.pipe.send_text("\x1bOQ")
                    await asyncio.wait_for(opened.wait(), DEADLINE)
                    popup = self.owner.plugins.popups[0]
                    self.pipe.send_text("sample 한 글" + close_key)
                    value = await asyncio.wait_for(popup.result, DEADLINE)
                    self.assertEqual(value, expected)
                    self.assertIs(
                        self.owner.app.layout.current_control, self.owner.control
                    )
                    self.assertEqual(bytes(self.owner.session.pending_input), b"")

    async def test_pixel_mouse_survives_real_ptk_input_and_popup_release(self):
        """PTK's cell routing cannot discard the child's precise 1016 coordinates."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1b[?1016;2$y")
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

    async def test_keypad_and_extended_functions_preserve_child_modes(self):
        """Physical enhanced keys use native keypad/cursor modes, not popup aliases."""
        self.rich_host()
        await self.start_input()
        self.owner.app.renderer.enter_input_modes()
        wire = "\x1b[57399u\x1b[57414u\x1b[57417u\x1b[57376u\x1b[57388u"
        cases = (
            (b"", b"0\r\x1b[D\x1b[25~\x1b[46~"),
            (b"\x1b=", b"0\r\x1b[D\x1b[25~\x1b[46~"),
            (
                b"\x1b[?1035l\x1b[?1h",
                b"\x1bOp\x1bOM\x1bOD\x1b[25~\x1b[46~",
            ),
            (b"\x1b[>3u", wire.encode()),
        )
        send = self.owner.session.send
        for modes, expected in cases:
            with self.subTest(modes=modes):
                self.owner._output(modes)
                self.owner.session.pending_input.clear()
                received = asyncio.Event()

                def observe(data, expected=expected, received=received):
                    send(data)
                    if bytes(self.owner.session.pending_input).endswith(expected[-7:]):
                        received.set()

                self.owner.session.send = observe
                self.pipe.send_text(wire)
                await asyncio.wait_for(received.wait(), DEADLINE)
                self.assertEqual(bytes(self.owner.session.pending_input), expected)
        self.owner.session.send = send

    async def key_then_popup(self, *, enhanced, before=b"", after=b""):
        """Inject a physical arrow through PTK, then inspect native popup handoff."""
        if enhanced:
            self.rich_host()
        await self.start_input()
        self.owner.app.renderer.enter_input_modes()
        self.owner.vt.feed(before)
        received = asyncio.Event()
        send = self.owner.session.send

        def observe(data):
            send(data)
            if data:
                received.set()

        self.owner.session.send = observe
        self.pipe.send_text("\x1b[A")
        await asyncio.wait_for(received.wait(), DEADLINE)
        self.owner.vt.feed(after)
        popup = self.owner.plugins.show_popup(
            TextArea(multiline=False), width=8, height=2
        )
        self.owner.plugins.close_popup(popup)
        return bytes(self.owner.session.pending_input)

    async def test_legacy_host_cannot_fabricate_release_when_child_requests_events(
        self,
    ):
        """A conventional host reports a keystroke, never a held physical key."""
        data = await self.key_then_popup(enhanced=False, before=b"\x1b[>3u")
        self.assertEqual(data, b"\x1b[1;1:1A")

    async def test_later_child_protocol_does_not_release_an_earlier_legacy_key(self):
        """Enabling child event reports cannot reinterpret an earlier keypress."""
        data = await self.key_then_popup(enhanced=True, after=b"\x1b[>3u")
        self.assertEqual(data, b"\x1b[A")

    async def test_enhanced_host_and_child_finish_a_delivered_press_before_popup(self):
        """Only matching source and consumer event contracts require a release."""
        data = await self.key_then_popup(enhanced=True, before=b"\x1b[>3u")
        self.assertEqual(data, b"\x1b[1;1:1A\x1b[1;1:3A")

    async def test_directory_effect_normalizes_absolute_paths_and_preserves_uris(self):
        """OSC9/1337 paths become OSC7 URIs without re-encoding existing URI escapes."""
        cases = (
            (
                "\x1b]1337;CurrentDir=/tmp/한 글%dir\x07".encode(),
                f"file://{socket.gethostname()}/tmp/%ED%95%9C%20%EA%B8%80%25dir",
            ),
            (
                b"\x1b]9;9;/tmp/other dir\x07",
                f"file://{socket.gethostname()}/tmp/other%20dir",
            ),
            (b"\x1b]7;file://remote/a%20b\x07", "file://remote/a%20b"),
        )
        for data, expected in cases:
            with self.subTest(data=data):
                self.owner._output(data)
                self.assertIn(f"\x1b]7;{expected}\x1b\\", self.render())
        self.owner._output(b"\x1b]1337;CurrentDir=relative\x07")
        self.assertNotIn("\x1b]7;relative", self.render())

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
        self.owner.host.control("\x1b[?1016;2$y")
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
        """A widget's consumed fallback cannot become input in a nested editor."""
        self.rich_host()
        await self.start_input()
        self.owner.app.renderer.enter_input_modes()
        parent = self.owner.plugins.show_popup(TextArea(), width=8, height=2)
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
        self.owner.plugins.close_popup(parent)

    async def test_plugin_escape_open_consumes_alt_remainder_before_popup_input(self):
        """Opening a popup on Alt's Escape keeps X out while accepting separate Y."""
        await self.check_alt_key_popup_open(mixed_prefix=False)

    async def test_consumed_alt_prefix_preserves_unrelated_buffered_keys_in_order(self):
        """Removing consumed X from an X,Y match must preserve subsequent Y,Z."""
        await self.check_alt_key_popup_open(mixed_prefix=True)

    async def test_failed_final_layout_still_restores_owned_host_metadata(self):
        """Final layout failure retains the title while restoring cwd and cursor."""
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
            "\x1b[?2031l",
            "\x1b]21;cursor=\x1b\\",
            restore_cwd,
        ):
            self.assertEqual(wire.count(sequence), 1, repr(sequence))
        self.assertEqual(wire.count("\x1b]2;CHILD-TITLE\x07"), 1)
        for sequence in ("\x1b[21t", "\x1b[22;2t", "\x1b[23;2t"):
            self.assertNotIn(sequence, wire)
        self.assertGreater(wire.index(restore_cwd), wire.index("file:///child"))
        self.assertTrue(self.owner.session.closed)

    async def test_titles_are_published_without_host_identification_and_retained(self):
        """Native OSC titles reach PTK's writer on unknown hosts and survive reset."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.finish_probe()
        for title in ("첫 제목", "LAST TITLE"):
            self.owner._output(f"\x1b]2;{title}\x07".encode())
            self.assertIn(f"\x1b]2;{title}\x07", self.render())
        self.owner.app.renderer.reset()
        self.output.flush()
        wire = self.stream.getvalue()
        self.assertEqual(
            re.findall(r"\x1b\]2;([^\x07\x1b]*)\x07", wire),
            ["첫 제목", "LAST TITLE"],
        )
        for sequence in ("\x1b[21t", "\x1b[22;2t", "\x1b[23;2t"):
            self.assertNotIn(sequence, wire)

    async def test_selected_search_popup_routes_real_keys_to_native_history(self):
        """Ctrl+F/Enter/Ctrl+P/Escape select history without sending editor input."""
        self.owner.plugins.load(
            ("terminal_search",), directory=ROOT / "examples/terminal_plugins"
        )
        self.owner.vt.feed(b"old needle\r\n" + b"filler\r\n" * 10 + b"new needle\r\n")
        self.render()
        selected = []
        select = self.owner.plugins.select_match

        def observe(*, previous=False):
            result = select(previous=previous)
            selected.append(result)
            return result

        changed = asyncio.Event()
        self.owner.app.after_render += lambda _: changed.set()

        async def require(predicate):
            async def ready():
                while not predicate():
                    changed.clear()
                    if not predicate():
                        await changed.wait()

            await asyncio.wait_for(ready(), DEADLINE)

        await self.start_input()
        with patch.object(self.owner.plugins, "select_match", side_effect=observe):
            self.pipe.send_bytes(b"\x06")
            await require(lambda: bool(self.owner.plugins.popups))
            self.pipe.send_text("needle\r")
            await require(lambda: len(selected) == 1)
            self.assertEqual(self.owner.vt.selection_text(), "needle")
            self.pipe.send_bytes(b"\r")
            await require(lambda: len(selected) == 2)
            self.pipe.send_bytes(b"\x10")
            await require(lambda: len(selected) == 3)
            self.assertEqual(selected, [0, 1, 0])
            self.pipe.send_bytes(b"\x1b")
            await require(lambda: not self.owner.plugins.popups)
        self.assertEqual(self.owner.vt.selection_text(), "")
        self.assertEqual(bytes(self.owner.session.pending_input), b"")
        self.assertEqual(self.owner.plugins.errors, [])
        self.assertIs(self.owner.app.layout.current_control, self.owner.control)


@unittest.skipUnless(os.name == "posix", "Native VT and PTK output require POSIX")
class FormattingPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Verify optional output choices through actual native cells and PTK wire."""

    async def open(self, features=None, *, replies=""):
        """Reuse the real renderer fixture with a fixed policy for this session."""
        from prompt_toolkit.input.vt100_parser import Vt100Parser

        from ish.terminal.host import HostParser

        await TerminalCompositionTests.asyncSetUp(self, features=features)
        self.owner.host.begin_probe(12, 6)
        keys = []
        HostParser(Vt100Parser(keys.append), self.owner.host).feed(replies)
        for key in keys:
            self.owner.host.handle_control(key)
        self.owner.host.finish_probe()

    async def asyncTearDown(self):
        """Close the same fixture even when a formatting assertion fails."""
        if hasattr(self, "owner"):
            await TerminalCompositionTests.asyncTearDown(self)

    def render(self):
        """Observe the bytes emitted by the existing PTK renderer."""
        return TerminalCompositionTests.render(self)

    def rich_cell(self):
        """Supply literal native link, underline color/style and overline requests."""
        self.owner.vt.feed(
            b"\x1b]8;;https://format.example/\x1b\\"
            b"\x1b[4:3m\x1b[58:2::7:8:9m\x1b[53mN\x1b[0m\x1b]8;;\x1b\\"
        )

    async def check_enabled(self, features):
        """A host name is unnecessary for explicitly selected formatting wire."""
        await self.open(features)
        self.rich_cell()
        popup = self.owner.plugins.show_popup(
            Window(FormattedTextControl([("underline", "P")], focusable=True)),
            left=1,
            top=0,
            width=1,
            height=1,
        )
        wire = self.render()
        for sequence in (
            "\x1b]8;;https://format.example/\x1b\\",
            "\x1b[4:3m",
            "\x1b[58:2::7:8:9m",
            "\x1b[53m",
            "\x1b[4:1mP",
        ):
            self.assertIn(sequence, wire)
        self.assertGreater(wire.rfind("\x1b]8;;\x1b\\"), wire.index("N"))
        self.assertIn("\x1b[55m", wire[wire.index("N") : wire.index("P")])
        self.assertEqual(self.owner.host.adapter.name, "unknown")
        self.owner.plugins.close_popup(popup)

    async def test_default_formats_reach_native_cells_without_host_identity(self):
        """The default choices preserve native formatting on an unidentified host."""
        await self.check_enabled(None)

    async def test_explicit_formats_reach_native_cells_without_host_identity(self):
        """The explicit on choices use the same output path as the defaults."""
        await self.check_enabled(
            {"hyperlinks": "on", "extended_styles": "on", "overline": "on"}
        )

    async def test_disabled_formats_emit_neither_content_nor_optional_cleanup(self):
        """Unused optional formats remain absent through first paint and final reset."""
        await self.open(
            {"hyperlinks": "off", "extended_styles": "off", "overline": "off"}
        )
        self.rich_cell()
        fragments = [("[ZeroWidthEscape]", "\x1b[53m"), ("", "P")]
        popup = self.owner.plugins.show_popup(
            Window(FormattedTextControl(fragments, focusable=True)),
            left=1,
            top=0,
            width=1,
            height=1,
        )
        self.render()
        self.owner.plugins.close_popup(popup)
        self.render()
        self.owner.app.renderer.reset()
        self.output.flush()
        wire = self.stream.getvalue()
        self.assertIn("N", wire)
        self.assertIn("P", wire)
        for sequence in (
            "\x1b]8;",
            "\x1b[4:",
            "\x1b[58:",
            "\x1b[59m",
            "\x1b[53m",
            "\x1b[55m",
        ):
            self.assertNotIn(sequence, wire)

    async def test_styles_do_not_enable_disabled_overline_or_links(self):
        """Independent underline selection cannot activate other optional formats."""
        await self.open(
            {"hyperlinks": "off", "extended_styles": "on", "overline": "off"}
        )
        self.rich_cell()
        wire = self.render()
        self.assertIn("\x1b[4:3m", wire)
        self.assertIn("\x1b[58:2::7:8:9m", wire)
        for sequence in ("\x1b]8;", "\x1b[53m", "\x1b[55m"):
            self.assertNotIn(sequence, wire)

    async def test_overline_does_not_enable_disabled_underline_extension(self):
        """Overline does not authorize underline color or colon-style output."""
        await self.open(
            {"hyperlinks": "off", "extended_styles": "off", "overline": "on"}
        )
        self.rich_cell()
        wire = self.render()
        self.assertIn("\x1b[53mN", wire)
        self.assertIn("\x1b[55m", wire)
        for sequence in ("\x1b]8;", "\x1b[4:", "\x1b[58:", "\x1b[59m"):
            self.assertNotIn(sequence, wire)

    async def test_cleanup_remembers_written_formats_when_profile_is_revoked(self):
        """Cleanup uses actual writer ownership even if an execution profile is lost."""
        await self.open()
        self.rich_cell()
        self.render()
        # Fault-inject a lost execution profile, not a supported live policy edit.
        self.owner.host.profile = replace(
            self.owner.profile, hyperlinks=False, extended_styles=False, overline=False
        )
        before = self.stream.tell()
        self.owner.app.renderer.reset()
        self.output.flush()
        wire = self.stream.getvalue()[before:]
        for sequence in ("\x1b]8;;\x1b\\", "\x1b[59m", "\x1b[55m"):
            self.assertIn(sequence, wire)
        self.assertNotIn("https://format.example/", wire)

    async def test_written_cursor_color_restores_even_when_execution_is_disabled(self):
        """The original observation, not the current output choice, owns cleanup."""
        await self.open(replies="\x1b]12;rgb:11/22/33\x1b\\")
        self.owner.vt.feed(b"\x1b]12;#aabbcc\x07N")
        self.assertIn("\x1b]12;rgb:aa/bb/cc\x1b\\", self.render())
        self.owner.host.profile = replace(
            self.owner.profile, cursor_color_control=False
        )
        before = self.stream.tell()
        self.owner.app.renderer.reset()
        self.output.flush()
        wire = self.stream.getvalue()[before:]
        self.assertEqual(wire.count("\x1b]12;rgb:11/22/33\x1b\\"), 1)
        self.assertNotIn("\x1b]12;rgb:aa/bb/cc\x1b\\", wire)


if __name__ == "__main__":
    unittest.main()
