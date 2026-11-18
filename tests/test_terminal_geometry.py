"""Serialized host-size refresh, independent of shell input readiness."""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

from ish.terminal.host import HostBroker, HostParser


class GeometryTests(unittest.TestCase):
    """Check exact queries, current dimensions and untouched input/capabilities."""

    def setUp(self):
        """Start with an explicitly queried host that reports no kernel pixels."""
        self.changed = 0

        def changed():
            self.changed += 1

        self.broker = HostBroker(on_geometry=changed)
        self.broker.begin_probe(80, 24, query_id=2147515049)
        self.broker.control("\x1b[6;20;10t")
        self.broker.control("\x1b[4;480;800t")
        self.broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        self.broker.finish_probe()
        self.broker.refresh_geometry(80, 24, 0, 0)
        self.keys = []
        self.parser = HostParser(Vt100Parser(self.keys.append), self.broker)

    def request(self, cols, rows):
        """Record actual outer grid movement before explicitly requesting pixels."""
        self.broker.refresh_geometry(cols, rows, 0, 0)
        return self.broker.request_geometry()

    def test_complete_pair_restores_images_without_reopening_capability_probe(self):
        """Two matching size responses enable the previously verified backend."""
        self.assertTrue(self.request(100, 30))
        serial, wire = self.broker.take_geometry_query()
        self.assertEqual((serial, wire), (1, "\x1b[16t\x1b[14t"))
        self.assertIsNone(self.broker.take_geometry_query())
        self.assertIsNone(self.broker.profile.image_protocol)
        self.parser.feed("a\x1b[6;22;11t")
        self.assertTrue(self.broker.geometry_pending)
        self.parser.feed("\x1b[4;660;1100tb")
        self.assertFalse(self.broker.geometry_pending)
        self.assertEqual(
            (self.broker.profile.cell_width, self.broker.profile.cell_height), (11, 22)
        )
        self.assertEqual(self.broker.profile.image_protocol, "kitty")
        self.assertEqual([key.data for key in self.keys], ["a", "b"])
        self.assertEqual(self.changed, 1)
        self.assertEqual(
            self.broker.feature_status["kitty_graphics"].support, "supported"
        )

    def test_queued_resizes_coalesce_before_any_query_is_sent(self):
        """A renderer takes only the newest request, not one query per SIGWINCH."""
        self.request(90, 25)
        self.request(120, 36)
        self.assertEqual(self.broker.take_geometry_query(), (1, "\x1b[16t\x1b[14t"))
        self.parser.feed("\x1b[6;20;10t\x1b[4;720;1200t")
        self.assertEqual(
            (self.broker.profile.pixel_width, self.broker.profile.pixel_height),
            (1200, 720),
        )
        self.assertFalse(self.broker.geometry_pending)

    def test_resize_retires_pair_and_drains_both_before_new_query(self):
        """Old results never acquire the new layout; no overlapping pair is sent."""
        self.request(90, 25)
        self.broker.take_geometry_query()
        self.parser.feed("\x1b[6;20;10t")
        self.request(120, 36)
        self.assertIsNone(self.broker.take_geometry_query())
        self.parser.feed("\x1b[4;500;900t\x1b[6;20;10t\x1b[4;500;900t")
        self.assertEqual(self.changed, 1)
        self.assertTrue(self.broker.geometry_pending)
        self.assertFalse(self.broker.profile.has_cell_size)
        self.assertEqual(self.broker.take_geometry_query(), (2, "\x1b[16t\x1b[14t"))
        self.parser.feed("\x1b[6;18;9t\x1b[4;648;1080t")
        self.assertEqual(
            (self.broker.profile.cell_width, self.broker.profile.cell_height), (9, 18)
        )

    def test_return_to_same_grid_does_not_revive_a_retired_layout_epoch(self):
        """Equal row/column counts do not make an old in-flight pair current."""
        self.request(90, 25)
        self.broker.take_geometry_query()
        self.request(100, 30)
        self.request(90, 25)
        self.parser.feed("\x1b[6;20;10t\x1b[4;500;900t")
        self.assertFalse(self.broker.profile.has_cell_size)
        self.assertTrue(self.broker.geometry_pending)
        self.assertEqual(self.broker.take_geometry_query()[0], 2)

    def test_timeout_rejects_late_pairs_and_prohibits_another_query(self):
        """A timed-out identity-free response cannot authorize a later refresh."""
        self.request(100, 30)
        serial, _ = self.broker.take_geometry_query()
        self.broker.expire_geometry(serial + 1)
        self.assertTrue(self.broker.geometry_pending)
        self.broker.expire_geometry(serial)
        self.parser.feed("\x1b[6;20;10t\x1b[4;600;1000t")
        self.assertFalse(self.broker.geometry_pending)
        self.assertFalse(self.broker.profile.has_cell_size)
        self.assertFalse(self.request(110, 33))
        self.assertIsNone(self.broker.take_geometry_query())
        self.assertEqual(self.changed, 1)

    def test_old_timer_cannot_cancel_the_next_valid_pair(self):
        """Local serials protect timers without claiming a wire response identity."""
        self.request(90, 25)
        old, _ = self.broker.take_geometry_query()
        self.request(100, 30)
        self.parser.feed("\x1b[6;20;10t\x1b[4;500;900t")
        new, _ = self.broker.take_geometry_query()
        self.assertNotEqual(new, old)
        self.broker.expire_geometry(old)
        self.assertTrue(self.broker.geometry_pending)
        self.parser.feed("\x1b[6;20;10t\x1b[4;600;1000t")
        self.assertEqual(self.broker.profile.image_protocol, "kitty")

    def test_missing_initial_response_cannot_be_followed_by_a_refresh(self):
        """The first pair must drain fully before using this bounded extension."""
        broker = HostBroker()
        broker.begin_probe(80, 24)
        broker.control("\x1b[6;20;10t")
        broker.finish_probe()
        broker.refresh_geometry(90, 25, 0, 0)
        self.assertFalse(broker.request_geometry())
        broker.control("\x1b[4;480;800t")
        self.assertFalse(broker.request_geometry())
        self.assertIsNone(broker.take_geometry_query())

    def test_invalid_or_conflicting_pair_fails_closed_without_guessing(self):
        """Size consistency rejects mixed, oversized and contradictory reports."""
        for wire in (
            "\x1b[6;20;10t\x1b[4;601;1000t",
            "\x1b[6;20;10t\x1b[6;22;11t",
            "\x1b[6;4097;10t",
            "\x1b[4;0;1000t",
        ):
            with self.subTest(wire=wire):
                self.setUp()
                self.request(100, 30)
                self.broker.take_geometry_query()
                self.parser.feed(wire)
                self.assertFalse(self.broker.geometry_pending)
                self.assertFalse(self.broker.profile.has_cell_size)
                self.assertFalse(self.request(110, 33))

    def test_identical_pending_duplicate_is_consumed_once(self):
        """A repeated kind does not complete a two-kind pair."""
        self.request(100, 30)
        self.broker.take_geometry_query()
        self.parser.feed("\x1b[6;20;10t\x1b[6;20;10t")
        self.assertTrue(self.broker.geometry_pending)
        self.assertEqual(self.changed, 0)
        self.parser.feed("\x1b[4;600;1000t")
        self.assertEqual(self.changed, 1)

    def test_kernel_geometry_remains_independent_after_query_timeout(self):
        """A later real ioctl can restore geometry without retrying stale queries."""
        self.request(100, 30)
        serial, _ = self.broker.take_geometry_query()
        self.broker.expire_geometry(serial)
        profile = self.broker.refresh_geometry(100, 30, 1000, 600)
        self.assertEqual((profile.cell_width, profile.cell_height), (10, 20))
        self.assertEqual(profile.image_protocol, "kitty")
        self.assertIsNone(self.broker.take_geometry_query())

    def test_new_kernel_measurement_wins_and_old_pair_only_drains(self):
        """Old size reports never replace independently measured current pixels."""
        self.request(100, 30)
        self.broker.take_geometry_query()
        self.broker.refresh_geometry(120, 36, 1440, 864)
        self.parser.feed("\x1b[6;20;10t\x1b[4;600;1000t")
        self.assertFalse(self.broker.geometry_pending)
        self.assertIsNone(self.broker.take_geometry_query())
        self.assertEqual(
            (self.broker.profile.cell_width, self.broker.profile.cell_height), (12, 24)
        )

    def test_cancelled_refresh_and_paste_do_not_become_key_or_geometry_evidence(self):
        """Paste stays user data; cancellation consumes all later control replies."""
        self.request(100, 30)
        self.broker.take_geometry_query()
        self.parser.feed("\x1b[200~\x1b[6;20;10t\x1b[4;600;1000t\x1b[201~")
        self.assertFalse(self.broker.profile.has_cell_size)
        self.assertEqual(len(self.keys), 1)
        self.assertEqual(self.keys[0].data, "\x1b[6;20;10t\x1b[4;600;1000t")
        self.broker.cancel_geometry()
        self.parser.feed("x\x1b[6;20;10t\x1b[4;600;1000ty")
        self.assertEqual([key.data for key in self.keys[1:]], ["x", "y"])
        self.assertFalse(self.request(110, 33))

    def test_disabled_unknown_or_unrestorable_features_do_not_query_geometry(self):
        """Text-only sessions never pause output for an unusable pixel feature."""
        for features, replies in (
            ({}, ()),
            (
                {
                    "kitty_graphics": "off",
                    "iterm_graphics": "off",
                    "sixel_graphics": "off",
                    "pixel_mouse": "off",
                },
                ("\x1b_Gi=2147515049;OK\x1b\\", "\x1b[?1016;2$y"),
            ),
            ({"sixel_graphics": "on"}, ("\x1b[?62;4c", "\x1b[?80;0$y")),
        ):
            with self.subTest(features=features):
                broker = HostBroker(features=features)
                broker.begin_probe(80, 24, query_id=2147515049)
                for reply in ("\x1b[6;20;10t", "\x1b[4;480;800t", *replies):
                    broker.control(reply)
                broker.finish_probe()
                broker.refresh_geometry(100, 30, 0, 0)
                self.assertFalse(broker.request_geometry())
                self.assertFalse(broker.geometry_pending)
                self.assertIsNone(broker.take_geometry_query())

    def test_selected_pixel_features_query_without_fabricating_observations(self):
        """Explicit output and verified automatic mouse selection reuse one policy."""
        for features, replies, feature, evidence in (
            ({"iterm_graphics": "on"}, (), "iterm_graphics", "unqueried"),
            ({"pixel_mouse": "on"}, (), "pixel_mouse", "unknown"),
            ({}, ("\x1b[?1016;2$y",), "pixel_mouse", "supported"),
        ):
            with self.subTest(features=features, replies=replies):
                broker = HostBroker(features=features)
                broker.begin_probe(80, 24)
                for reply in ("\x1b[6;20;10t", "\x1b[4;480;800t", *replies):
                    broker.control(reply)
                broker.finish_probe()
                broker.refresh_geometry(100, 30, 0, 0)
                self.assertTrue(broker.request_geometry())
                self.assertEqual(broker.take_geometry_query(), (1, "\x1b[16t\x1b[14t"))
                self.assertEqual(broker.feature_status[feature].support, evidence)
                self.assertFalse(broker.feature_status[feature].active)


@unittest.skipUnless(os.name == "posix", "Native VT and PTY require POSIX")
class AppGeometryTests(unittest.IsolatedAsyncioTestCase):
    """Verify real PTK input and PTY output across presentation-only pauses."""

    ROOT = Path(__file__).resolve().parents[1]
    IMAGE = b"\x1b_Ga=T,i=77,f=32,s=1,v=1,q=2,C=1;/wAA/w==\x1b\\"

    @asynccontextmanager
    async def terminal(self):
        """Negotiate a deterministic host while running the real app and engine."""
        from ish.terminal.app import TerminalApp

        grid = [Size(rows=8, columns=40)]
        stream = io.StringIO()
        with create_pipe_input() as pipe:
            output = Vt100_Output(
                stream, lambda: grid[0], term="xterm-256color", enable_cpr=False
            )
            with (
                create_app_session(input=pipe, output=output),
                patch("ish.terminal.app.create_input", return_value=pipe),
            ):
                owner = TerminalApp("/bin/sh", self.ROOT)
            owner.presentation_ready = True
            owner.host.begin_probe(40, 8, query_id=2147515049)
            owner.host.control("\x1b[6;20;10t")
            owner.host.control("\x1b[4;160;400t")
            owner.host.control("\x1b_Gi=2147515049;OK\x1b\\")
            owner.host.finish_probe()
            owner.vt.resize(40, 8, 10, 20)
            owner.vt.set_graphics(True)
            ready = asyncio.Event()
            owner.app.after_render += lambda _: ready.set()
            task = asyncio.create_task(owner.app.run_async(set_exception_handler=False))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                yield owner, pipe, grid, stream
            finally:
                if owner.geometry_timer is not None:
                    owner.geometry_timer.cancel()
                if owner.animation is not None:
                    owner.animation.cancel()
                owner.host.cancel_geometry()
                if owner.app.is_running and not owner.app.is_done:
                    owner.app.exit()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    await owner.plugins.close()
                    await owner.clipboard.aclose()
                    await owner.session.close()
                    owner.vt.close()

    async def start_child(self, owner, result, *, image=None):
        """Write an actual image, then persist stdin before producing more output."""
        received = bytearray()
        ready = asyncio.Event()
        script = (
            "import os,pathlib; os.write(1,"
            + repr((self.IMAGE if image is None else image) + b"CHILD_READY\r\n")
            + "); value=input(); pathlib.Path("
            + repr(str(result))
            + ").write_text(value,encoding='utf-8'); "
            "os.write(1,b'LATE_OUTPUT\\r\\n'); input()"
        )

        def output(data):
            owner._output(data)
            received.extend(data)
            if b"CHILD_READY" in received:
                ready.set()

        owner.session.output = output
        owner.session.argv = [sys.executable, "-c", script]
        owner.session.start(*owner.size)
        await ready.wait()
        self.assertEqual(owner.vt.image(77).rgba, b"\xff\x00\x00\xff")
        return received

    async def begin_resize(self, owner, grid, stream):
        """Exercise the renderer's ordinary measured-size path and sole writer."""
        grid[0] = Size(rows=10, columns=50)
        owner.app.invalidate()
        while "\x1b[16t\x1b[14t" not in stream.getvalue():
            await asyncio.sleep(0.01)
        self.assertTrue(owner.host.geometry_pending)
        self.assertFalse(owner.session.output_enabled)
        self.assertTrue(owner.vt.graphics_enabled)
        self.assertTrue(owner.vt.image_exists(77))

    async def test_resize_keeps_images_and_stdin_while_pty_output_waits_for_geometry(
        self,
    ):
        """Input reaches its file while output waits; matching reports resume it."""
        async with self.terminal() as (owner, pipe, grid, stream), asyncio.timeout(5):
            with tempfile.TemporaryDirectory() as directory:
                result = Path(directory) / "input"
                received = await self.start_child(owner, result)
                await self.begin_resize(owner, grid, stream)
                pipe.send_text("typed 한글\r")
                while not result.exists():
                    await asyncio.sleep(0.01)
                self.assertEqual(result.read_text(), "typed 한글")
                self.assertNotIn(b"LATE_OUTPUT", received)
                self.assertEqual((owner.vt.cell_width, owner.vt.cell_height), (10, 20))
                pipe.send_bytes(b"\x1b[6;22;11t\x1b[4;220;550t")
                while b"LATE_OUTPUT" not in received:
                    await asyncio.sleep(0.01)
                self.assertTrue(owner.session.output_enabled)
                self.assertEqual((owner.vt.cell_width, owner.vt.cell_height), (11, 22))
                self.assertEqual(owner.vt.image(77).rgba, b"\xff\x00\x00\xff")
                self.assertEqual(owner.profile.image_protocol, "kitty")
                self.assertEqual(stream.getvalue().count("\x1b[16t\x1b[14t"), 1)
                self.assertIsNone(owner.plugins.manager)
                self.assertEqual(owner.plugins.popups, [])

    async def test_resize_restarts_animation_without_further_child_output(self):
        """Geometry recovery restarts native frames after the old timer has stopped."""
        animation = (
            self.IMAGE
            + b"\x1b_Ga=f,i=77,f=32,s=1,v=1,z=40,q=2;AAD//w==\x1b\\"
            + b"\x1b_Ga=a,i=77,r=1,z=30,q=2;\x1b\\"
            + b"\x1b_Ga=a,i=77,s=3,q=2;\x1b\\"
        )
        async with self.terminal() as (owner, pipe, grid, stream), asyncio.timeout(5):
            with tempfile.TemporaryDirectory() as directory:
                result = Path(directory) / "input"
                received = await self.start_child(owner, result, image=animation)
                self.assertIsNotNone(owner.animation)
                await self.begin_resize(owner, grid, stream)
                while owner.animation is not None:
                    await asyncio.sleep(0.001)
                before = bytes(received)
                pipe.send_bytes(b"\x1b[6;22;11t\x1b[4;220;550t")
                while not owner.session.output_enabled:
                    await asyncio.sleep(0.001)
                frames = {}
                while len(frames) < 2:
                    current = owner.vt.image(77)
                    frames[current.generation] = current.rgba
                    await asyncio.sleep(0.001)
                self.assertEqual(
                    set(frames.values()), {b"\xff\x00\x00\xff", b"\x00\x00\xff\xff"}
                )
                self.assertEqual(bytes(received), before)
                self.assertFalse(result.exists())
                self.assertTrue(owner.vt.graphics_enabled)
                self.assertIsNotNone(owner.animation)

    async def test_geometry_timeout_disables_images_and_resumes_native_text(self):
        """No response preserves text and stdin rather than leaving the PTY paused."""
        async with self.terminal() as (owner, pipe, grid, stream), asyncio.timeout(5):
            with tempfile.TemporaryDirectory() as directory:
                result = Path(directory) / "input"
                received = await self.start_child(owner, result)
                await self.begin_resize(owner, grid, stream)
                pipe.send_text("timeout-input\r")
                while not result.exists():
                    await asyncio.sleep(0.01)
                self.assertEqual(result.read_text(), "timeout-input")
                self.assertNotIn(b"LATE_OUTPUT", received)
                while b"LATE_OUTPUT" not in received:
                    await asyncio.sleep(0.01)
                self.assertFalse(owner.host.geometry_pending)
                self.assertTrue(owner.session.output_enabled)
                self.assertFalse(owner.vt.graphics_enabled)
                self.assertFalse(owner.vt.image_exists(77))
                self.assertEqual((owner.vt.cell_width, owner.vt.cell_height), (0, 0))
                grid[0] = Size(rows=12, columns=60)
                owner.app.invalidate()
                while owner.size != (60, 12):
                    await asyncio.sleep(0.01)
                self.assertEqual(stream.getvalue().count("\x1b[16t\x1b[14t"), 1)
                self.assertTrue(owner.session.output_enabled)

    async def test_popup_escape_adjacent_to_geometry_response_never_reaches_stdin(self):
        """Real PTK focus changes consume Escape without leaking response suffixes."""
        async with self.terminal() as (owner, pipe, grid, stream), asyncio.timeout(5):
            with tempfile.TemporaryDirectory() as directory:
                result = Path(directory) / "input"
                received = await self.start_child(owner, result)
                editor = TextArea(multiline=False)
                bindings = KeyBindings()
                popup = owner.plugins.show_popup(editor, width=20, height=3)

                @bindings.add("escape", eager=True)
                def close(event):
                    owner.plugins.close_popup(popup)

                editor.control.key_bindings = bindings
                await self.begin_resize(owner, grid, stream)
                pipe.send_bytes(b"\x1b\x1b[6;22;11t\x1b[4;220;550tclean-input\r")
                while not result.exists() or b"LATE_OUTPUT" not in received:
                    await asyncio.sleep(0.01)
                self.assertIsNone(await popup.result)
                self.assertEqual(owner.plugins.popups, [])
                self.assertEqual(result.read_bytes(), b"clean-input")
                self.assertEqual(editor.text, "")
                self.assertTrue(owner.session.output_enabled)
                self.assertEqual((owner.vt.cell_width, owner.vt.cell_height), (11, 22))
                self.assertEqual(owner.vt.image(77).rgba, b"\xff\x00\x00\xff")


if __name__ == "__main__":
    unittest.main()
