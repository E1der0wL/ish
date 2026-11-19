"""Observe and restore management host state through a real PTK/PTY reader."""

from __future__ import annotations

import asyncio
import io
import os
import re
import termios
import unittest
from unittest.mock import patch

from prompt_toolkit.application import in_terminal
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_input, create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

from ish.manage.app import Screen
from ish.manage.host import Presentation
from ish.terminal.host import HostBroker


class WireHost(io.StringIO):
    """Answer literal startup queries while recording independently emitted wire."""

    MODES = {
        1: 2,
        6: 1,
        7: 2,
        12: 1,
        25: 2,
        80: 2,
        1000: 1,
        1002: 2,
        1003: 2,
        1005: 2,
        1006: 1,
        1015: 2,
        1016: 2,
        2004: 1,
    }

    def __init__(self, master, *, reply=True, keys=False):
        """Keep synthetic protocol evidence separate from the product broker."""
        super().__init__()
        self.master, self.reply, self.keys = master, reply, keys
        self.queried = False
        self.writes = []
        self.sync_mode = None

    def write(self, value):
        """Interleave ordinary keys with replies on the same real PTY input."""
        result = super().write(value)
        self.writes.append(value)
        if "\x1bP$q q\x1b\\" in value and not self.queried:
            self.queried = True
            if self.reply:
                parts = ["a" if self.keys else "", "\x1bP1$r3 q\x1b\\"]
                if self.sync_mode is not None and "\x1b[?2026$p" in value:
                    parts.append(f"\x1b[?2026;{self.sync_mode}$y")
                if "\x1bP+q524742\x1b\\" in value:
                    parts.append("\x1bP1+r524742=382F382F38\x1b\\")
                parts.extend(
                    f"\x1b[?{mode};{state}$y" for mode, state in self.MODES.items()
                )
                parts.extend(["b" if self.keys else "", "\x1bP1$r0;1;3;31;44m\x1b\\"])
                os.write(self.master, "".join(parts).encode())
        return result


class ManagementRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Verify output state and final key consumers without loading a VT engine."""

    def fixture(self, *, reply=True, keys=False, palette=lambda: {}, sizes=None):
        """Use PTK's genuine descriptor parser with a responding outer terminal."""
        master, slave = os.openpty()
        self.addCleanup(os.close, slave)
        self.addCleanup(os.close, master)
        stream = self.enterContext(os.fdopen(os.dup(slave), "r"))
        source = create_input(stdin=stream)
        self.addCleanup(source.close)
        original_parser = source.vt100_parser
        host = WireHost(master, reply=reply, keys=keys)
        output = Vt100_Output(
            host,
            lambda: sizes[-1] if sizes else Size(rows=12, columns=60),
            term="xterm-256color",
            enable_cpr=False,
        )
        screen = Screen(
            "Management",
            [("Page", TextArea(text="Body", read_only=True))],
            palette=palette,
            input=source,
            output=output,
        )
        self.assertEqual(host.getvalue(), "")
        return screen, host, source, original_parser, slave

    def assert_restored(self, wire):
        """Compare final wire values to literal startup reports, not broker state."""
        exit_screen = wire.rindex("\x1b[?1049l")
        origin = wire.rindex("\x1b[?6h")
        self.assertLess(origin, exit_screen)
        values = {}
        for mode, action in re.findall(r"\x1b\[\?(\d+)([hl])", wire):
            values[int(mode)] = 1 if action == "h" else 2
        for mode, value in WireHost.MODES.items():
            if mode == 80:
                # Management never draws images or acquires SIXEL display mode.
                self.assertNotIn(mode, values)
                continue
            self.assertEqual(values[mode], value, mode)
        self.assertEqual(re.findall(r"\x1b\[(\d+) q", wire)[-1], "3")
        self.assertTrue(wire.endswith("\x1b[0;1;3;31;44m"), repr(wire[-90:]))

    async def finish(self, screen, host, *, key=b"\x1b"):
        """Close only after an actual render, preserving the final lifecycle cleanup."""
        ready = asyncio.Event()
        screen.app.after_render += lambda _: ready.set()
        task = asyncio.create_task(screen.app.run_async())
        try:
            await asyncio.wait_for(ready.wait(), 3)
            os.write(host.master, key)
            return await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_keys_restore_observed_modes_cursor_and_attributes(self):
        """Both sidebar Escape and Ctrl+D restore the starting values after cleanup."""
        for key in (b"\x1b", b"\x04"):
            with self.subTest(key=key):
                screen, host, source, parser, slave = self.fixture()
                initial = termios.tcgetattr(slave)
                self.assertFalse(await self.finish(screen, host, key=key))
                self.assert_restored(host.getvalue())
                self.assertEqual(termios.tcgetattr(slave), initial)
                self.assertIs(source.vt100_parser, parser)
                self.assertFalse(source.closed)
                self.assertTrue(
                    all(
                        not state.active
                        for state in screen.app.presentation.host.feature_status.values()
                    )
                )

    async def test_probe_precedes_paint_and_keeps_interleaved_keys_in_order(self):
        """UI colors and restoration share the reader without requesting VT features."""
        with patch.dict(
            os.environ,
            {"NO_COLOR": "", "COLORTERM": "", "PROMPT_TOOLKIT_COLOR_DEPTH": ""},
        ):
            screen, host, _, _, _ = self.fixture(
                keys=True, palette=lambda: {"text": "blue-500"}
            )
        keys = []
        screen.bindings.add("a")(lambda event: keys.append("a"))
        screen.bindings.add("b")(lambda event: keys.append("b"))
        await self.finish(screen, host)
        wire = host.getvalue()
        probe = wire[: wire.index("\x1b[?1049h")]
        expected = (
            "\x1bP$q q\x1b\\\x1bP+q524742\x1b\\\x1bP+q436f\x1b\\\x1bP$qm\x1b\\\x1b[?2026$p"
            + "".join(f"\x1b[?{mode}$p" for mode in WireHost.MODES)
        )
        self.assertEqual(probe, expected)
        self.assertIn("38;2;59;130;246", wire[wire.index("\x1b[?1049h") :])
        self.assertEqual(keys, ["a", "b"])
        self.assert_restored(wire)

    async def test_explicit_ptk_color_depth_skips_the_optional_management_probe(self):
        """Respect the user's output policy without loading a terminal session."""
        with patch.dict(
            os.environ, {"NO_COLOR": "", "PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_8_BIT"}
        ):
            screen, host, _, _, _ = self.fixture(palette=lambda: {"text": "blue-500"})
            await self.finish(screen, host)
        self.assertEqual(screen.app.color_depth.value, "DEPTH_8_BIT")
        self.assertNotIn("\x1bP+q", host.getvalue())
        self.assertNotIn("38;2;59;130;246", host.getvalue())
        self.assert_restored(host.getvalue())

    async def test_external_editor_handoff_restores_then_reenters_the_screen(self):
        """PTK in_terminal sees original modes and termios before executing code."""
        screen, host, _, _, slave = self.fixture()
        initial = termios.tcgetattr(slave)
        returned = asyncio.Event()
        observations = []

        async def editor():
            async with in_terminal():
                observations.append((host.getvalue(), termios.tcgetattr(slave)))
                await asyncio.sleep(0)
            returned.set()

        @screen.bindings.add("e")
        def open_editor(event):
            event.app.create_background_task(editor())

        ready = asyncio.Event()
        screen.app.after_render += lambda _: ready.set()
        task = asyncio.create_task(screen.app.run_async())
        try:
            await asyncio.wait_for(ready.wait(), 3)
            os.write(host.master, b"e")
            await asyncio.wait_for(returned.wait(), 3)
            self.assert_restored(observations[0][0])
            self.assertEqual(observations[0][1], initial)
            self.assertEqual(host.getvalue().count("\x1bP$q q\x1b\\"), 1)
            self.assertEqual(host.getvalue().count("\x1b[?1049h"), 2)
            os.write(host.master, b"\x04")
            self.assertFalse(await asyncio.wait_for(task, 3))
            self.assert_restored(host.getvalue())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_resize_publishes_content_without_a_blank_or_host_reset(self):
        """A burst of size changes retains screen ownership and paints the final size."""
        for mode in (None, 1, 2):
            with self.subTest(initial_sync=mode):
                sizes = [Size(rows=20, columns=80)]
                screen, host, _, _, _ = self.fixture(sizes=sizes)
                host.sync_mode = mode
                ready = asyncio.Event()
                screen.app.after_render += lambda _, ready=ready: ready.set()
                task = asyncio.create_task(screen.app.run_async())
                try:
                    await asyncio.wait_for(ready.wait(), 3)
                    for rows, cols in ((45, 170), (18, 62), (36, 120)):
                        ready.clear()
                        host.writes.clear()
                        sizes.append(Size(rows=rows - 2, columns=cols - 10))
                        screen.app._on_resize()
                        sizes.append(Size(rows=rows, columns=cols))
                        screen.app._on_resize()
                        await asyncio.wait_for(ready.wait(), 3)
                        parts = host.writes
                        wire = "".join(parts)
                        self.assertIn("Body", wire)
                        for part in parts:
                            if "\x1b[J" in part:
                                self.assertIn("Body", part)
                        for reset in (
                            "\x1b[?1049",
                            "\x1b[?2004",
                            "\x1b[?1003",
                            "\x1b[6n",
                        ):
                            self.assertNotIn(reset, wire)
                        self.assertEqual(wire.count("Body"), 1)
                        if mode == 2:
                            self.assertLess(
                                wire.index("\x1b[?2026h"), wire.index("Body")
                            )
                            self.assertLess(
                                wire.index("Body"), wire.index("\x1b[?2026l")
                            )
                        else:
                            self.assertNotIn("\x1b[?2026h", wire)
                            self.assertNotIn("\x1b[?2026l", wire)
                    os.write(host.master, b"\x04")
                    await asyncio.wait_for(task, 3)
                    self.assert_restored(host.getvalue())
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_unanswered_probe_does_not_restore_late_unobserved_values(self):
        """Unknown state stays unknown after timeout; a late report cannot replace it."""
        screen, host, _, _, _ = self.fixture(reply=False)
        with patch.object(Presentation, "PROBE_SECONDS", 0.01):
            await self.finish(screen, host, key=b"\x1bP1$r5 q\x1b\\\x04")
        self.assertNotIn("\x1b[5 q", host.getvalue())
        self.assertIsNone(screen.app.presentation.host.profile.initial_cursor_style)

    async def test_dummy_output_and_caller_owned_pipe_need_no_terminal_probe(self):
        """Pipe tests retain their normal PTK behavior without fake host evidence."""
        with create_pipe_input() as source:
            parser = source.vt100_parser
            screen = Screen(
                "Help",
                [("Page", TextArea(text="Body"))],
                input=source,
                output=DummyOutput(),
            )
            source.send_bytes(b"\x04")
            self.assertFalse(await asyncio.wait_for(screen.app.run_async(), 3))
            self.assertIs(source.vt100_parser, parser)
            self.assertFalse(source.closed)

    def test_restore_only_probe_cannot_enable_features_even_if_explicitly_on(self):
        """Presentation observations do not act as an actual session capability probe."""
        broker = HostBroker(features={"hyperlinks": "on", "kitty_graphics": "on"})
        broker.begin_restore_probe()
        broker.control("\x1bP1$r3 q\x1b\\")
        profile = broker.finish_probe()
        self.assertFalse(profile.hyperlinks)
        self.assertFalse(profile.kitty_graphics)
        self.assertEqual(profile.initial_cursor_style.code, 3)
