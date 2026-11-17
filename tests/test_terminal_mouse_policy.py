"""Check outer capture bytes and final PTY consumers through real PTK input."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

import test_terminal_composition as composition

DEADLINE = 5
TRACKING = (1000, 1003, 1015, 1006)


@unittest.skipUnless(os.name == "posix", "Real PTK/native input requires POSIX")
class MousePolicyTests(unittest.IsolatedAsyncioTestCase):
    """Policies affect host ownership, not shell names or inferred prompt text."""

    async def asyncSetUp(self):
        """Use the established VT output and PTK pipe fixture without test inheritance."""
        await composition.TerminalCompositionTests.asyncSetUp(self)

    async def asyncTearDown(self):
        """Stop native selection timers before the shared fixture frees its grid."""
        self.owner.selection.close()
        await composition.TerminalCompositionTests.asyncTearDown(self)

    def render(self):
        """Observe only bytes emitted by PTK's actual renderer."""
        return composition.TerminalCompositionTests.render(self)

    async def start_input(self):
        """Wait for PTK's actual input reader and first frame."""
        await composition.TerminalCompositionTests.start_input(self)

    async def sent(self, wire, suffix):
        """Wait for an independently chosen final child byte sequence."""
        done = asyncio.Event()
        send = self.owner.session.send

        def observe(data):
            send(data)
            if bytes(self.owner.session.pending_input).endswith(suffix):
                done.set()

        self.owner.session.send = observe
        try:
            self.pipe.send_text(wire)
            await asyncio.wait_for(done.wait(), DEADLINE)
        finally:
            self.owner.session.send = send

    async def test_default_and_keyboard_surfaces_leave_host_selection_available(self):
        """Ordinary text, a bar and a keyboard popup never enable host tracking."""
        self.assertEqual(self.owner.mouse_policy, "auto")
        self.owner._output(b"SHELL> ")
        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")))
        popup = self.owner.plugins.show_popup(TextArea(multiline=False))
        self.render()
        for mode in (*TRACKING, 1016):
            self.assertNotIn(f"\x1b[?{mode}h", self.stream.getvalue())
        self.owner.plugins.close_popup(popup)
        self.owner.plugins.set_bar()
        self.render()
        self.assertFalse(self.owner.mouse_enabled)

    async def test_auto_tracks_native_modes_and_reset_without_shell_hooks(self):
        """Child mode changes alone enable/disable the outer host's tracking."""
        self.owner._output(b"\x1b[?1000h\x1b[?1006h")
        enabled = self.render()
        for mode in TRACKING:
            self.assertIn(f"\x1b[?{mode}h", enabled)
        await self.start_input()
        await self.sent("\x1b[<0;5;3M\x1b[<0;5;3m", b"\x1b[<0;5;3m")
        self.assertEqual(
            bytes(self.owner.session.pending_input), b"\x1b[<0;5;3M\x1b[<0;5;3m"
        )
        self.owner._output(b"\x1bc")
        disabled = self.render()
        for mode in TRACKING:
            self.assertIn(f"\x1b[?{mode}l", disabled)
        await self.sent("\x1b[<0;5;3MZ", b"Z")
        self.assertEqual(
            bytes(self.owner.session.pending_input),
            b"\x1b[<0;5;3M\x1b[<0;5;3mZ",
        )

    async def test_off_ignores_native_and_ui_requests_and_late_reports(self):
        """Explicit off cannot be overridden by a child or click-enabled plugin."""
        self.owner.set_mouse_policy("off")
        self.owner._output(b"\x1b[?1003h\x1b[?1006h")
        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")), mouse=True)
        self.render()
        await self.start_input()
        await self.sent("\x1b[<0;5;3M\x1b[<0;5;3mOK", b"OK")
        self.assertEqual(bytes(self.owner.session.pending_input), b"OK")
        for mode in (*TRACKING, 1016):
            self.assertNotIn(f"\x1b[?{mode}h", self.stream.getvalue())

    async def test_on_retains_local_selection_and_policy_change_releases_capture(self):
        """On copies native text; switching off cancels gestures and native presses."""
        self.owner.set_mouse_policy("on")
        self.owner._output(b"hello world")
        await self.start_input()
        copied = asyncio.Event()
        copy = self.owner.copy_selection

        def observe():
            copy()
            copied.set()

        self.owner.selection.copied = observe
        self.pipe.send_text("\x1b[<0;1;1M\x1b[<32;6;1M\x1b[<0;6;1m")
        await asyncio.wait_for(copied.wait(), DEADLINE)
        self.assertEqual(self.owner.app.clipboard.get_data().text, "hello")
        self.owner.set_mouse_policy("off")
        self.render()
        self.assertEqual(self.owner.vt.selection_text(), "")
        self.assertFalse(self.owner.selection.active)
        self.owner.set_mouse_policy("auto")
        self.owner._output(b"\x1b[?1003h\x1b[?1006h")
        self.render()
        await self.sent("\x1b[<0;5;3M", b"\x1b[<0;5;3M")
        self.owner.set_mouse_policy("off")
        self.render()
        self.assertEqual(
            bytes(self.owner.session.pending_input), b"\x1b[<0;5;3M\x1b[<0;5;3m"
        )
        self.assertEqual(self.owner.native_buttons, {})

    async def test_keyboard_popup_suspends_child_capture_and_click_ui_opts_in(self):
        """Mouse capture follows focused consumers, including nested popup removal."""
        self.owner._output(b"\x1b[?1003h")
        self.render()
        popup = self.owner.plugins.show_popup(TextArea(multiline=False))
        disabled = self.render()
        self.assertIn("\x1b[?1003l", disabled)
        click = self.owner.plugins.show_popup(TextArea(multiline=False), mouse=True)
        self.assertIn("\x1b[?1003h", self.render())
        self.owner.plugins.close_popup(click)
        self.assertIn("\x1b[?1003l", self.render())
        self.owner.plugins.close_popup(popup)
        self.assertIn("\x1b[?1003h", self.render())
        self.owner._output(b"\x1b[?1003l")
        self.render()
        self.owner.plugins.set_bar(Window(FormattedTextControl("BAR")), mouse=True)
        self.assertIn("\x1b[?1003h", self.render())
        self.owner.plugins.set_bar()
        self.assertIn("\x1b[?1003l", self.render())

    async def test_pixel_format_is_disabled_without_reenabling_cell_capture(self):
        """A resize or repeat render cannot re-enable the broker's disabled pixels."""
        self.owner.host.begin_probe(12, 6)
        self.owner.host.control("\x1b[?1016;2$y")
        self.owner.host.control("\x1b[6;20;10t")
        self.owner.host.finish_probe()
        self.owner.set_mouse_policy("on")
        with patch.object(self.owner, "_geometry", return_value=(12, 6, 120, 120)):
            self.assertIn("\x1b[?1016h", self.render())
            self.owner.set_mouse_policy("off")
            disabled = self.render()
            self.assertIn("\x1b[?1016l", disabled)
            self.assertNotIn("\x1b[?1006h", disabled)
            self.outer_size = Size(rows=8, columns=14)
            repeated = self.render() + self.render()
            for mode in (*TRACKING, 1016):
                self.assertNotIn(f"\x1b[?{mode}h", repeated)

    async def test_reports_after_capture_end_cannot_click_a_keyboard_popup(self):
        """Queued mouse bytes are consumed before PTK dispatches a new widget."""
        clicks = []
        content = Window(
            FormattedTextControl(
                [("", "CLICK", lambda event: clicks.append(event))], focusable=True
            )
        )
        self.owner.plugins.show_popup(content, left=0, top=0, width=5, height=1)
        await self.start_input()
        processed = asyncio.Event()
        self.owner.plugins.bind("x")(lambda event: processed.set())
        self.pipe.send_text("\x1b[<0;2;1M\x1b[<0;2;1mx")
        await asyncio.wait_for(processed.wait(), DEADLINE)
        self.assertEqual(clicks, [])

    async def test_explicit_mouse_bar_uses_ptk_widget_handler(self):
        """The terminal's eager native mouse binding must not swallow bar clicks."""
        clicked = asyncio.Event()
        self.owner.plugins.set_bar(
            Window(FormattedTextControl([("", "CLICK", lambda event: clicked.set())])),
            mouse=True,
        )
        await self.start_input()
        self.pipe.send_text("\x1b[<0;2;6M\x1b[<0;2;6m")
        await asyncio.wait_for(clicked.wait(), DEADLINE)
        self.assertEqual(bytes(self.owner.session.pending_input), b"")

    async def test_renderer_reset_finishes_child_press_before_releasing_host(self):
        """An explicit renderer reset has the same gesture boundary as shutdown."""
        self.owner._output(b"\x1b[?1003h\x1b[?1006h")
        await self.start_input()
        await self.sent("\x1b[<0;5;3M", b"\x1b[<0;5;3M")
        self.owner.app.renderer.reset()
        self.assertEqual(
            bytes(self.owner.session.pending_input), b"\x1b[<0;5;3M\x1b[<0;5;3m"
        )
        self.assertFalse(self.owner.mouse_captured)

    async def test_auto_mouse_reaches_actual_pty_consumer_and_file_once(self):
        """An actual program requests mouse, consumes it, then returns to host selection."""
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "received.bin"
            self.owner.session.argv = [
                sys.executable,
                "-c",
                "import os,sys,tty; from pathlib import Path; tty.setraw(0); "
                "os.write(1,b'\\x1b[?1000h\\x1b[?1006h'); data=b''\n"
                "while not data.endswith(b'\\x1b[<0;5;3m'): data += os.read(0,64)\n"
                "Path(sys.argv[1]).write_bytes(data); "
                "os.write(1,b'\\x1b[?1000l'); os.read(0,1)",
                str(result),
            ]
            await self.start_input()
            self.owner.session.start(*self.owner.size)
            async with asyncio.timeout(DEADLINE):
                while not self.owner.vt.mouse_reporting:
                    await asyncio.sleep(0.001)
            self.render()
            self.pipe.send_text("\x1b[<0;5;3M\x1b[<0;5;3m")
            async with asyncio.timeout(DEADLINE):
                while not result.exists() or self.owner.vt.mouse_reporting:
                    await asyncio.sleep(0.001)
            self.assertEqual(result.read_bytes(), b"\x1b[<0;5;3M\x1b[<0;5;3m")
            self.render()
            self.assertFalse(self.owner.mouse_enabled)


if __name__ == "__main__":
    unittest.main()
