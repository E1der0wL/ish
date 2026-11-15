"""Observe the real PTK panel's display, editing, focus and bounded output."""

import asyncio
import contextlib
import io
import unittest

import pyte

from ish.stream.panel import Panel


class PanelTests(unittest.IsolatedAsyncioTestCase):
    """Exercise public panel input/output through PTK, independent of shell state."""

    async def asyncSetUp(self):
        """Start a hidden panel and collect only externally published effects."""
        self.frames = []
        self.focus = []
        self.results = []
        self.remainders = []
        self.changed = asyncio.Event()

        def painted(frame):
            self.frames.append(frame)
            self.changed.set()

        self.panel = Panel(
            painted,
            on_focus=self.focus.append,
            on_result=self.results.append,
            on_remainder=self.remainders.append,
        )
        self.host_output = io.StringIO()
        with contextlib.redirect_stdout(self.host_output):
            await self.panel.start(50, 5)

    async def asyncTearDown(self):
        """Close the application without writing terminal teardown sequences."""
        with contextlib.redirect_stdout(self.host_output):
            await self.panel.close()
        self.assertEqual(self.host_output.getvalue(), "")

    async def frame_matching(self, predicate):
        """Use a deadline only to fail a missing externally visible result."""
        async with asyncio.timeout(3):
            while True:
                if self.frames and predicate(self.frames[-1]):
                    return self.frames[-1]
                self.changed.clear()
                await self.changed.wait()

    async def test_hidden_panel_has_no_output_or_input_ownership(self):
        """An unshown panel must not alter display or consume native input."""
        self.assertEqual(self.frames, [])
        self.assertFalse(self.panel.open_popup())
        self.assertFalse(self.panel.feed(b"echo native\r"))
        self.assertEqual(self.focus, [])

    async def test_only_reserved_rows_are_painted_with_default_background(self):
        """Panel painting preserves rows above and below its rectangle."""
        self.panel.show()
        frame = await self.frame_matching(lambda value: not value.popup)
        self.assertIn("ish stream | native shell", frame.lines[-1])
        screen = pyte.Screen(50, 12)
        stream = pyte.ByteStream(screen)
        for row in range(1, 13):
            stream.feed(f"\x1b[{row};1Hkeep row {row}".encode())
        old = screen.display.copy()
        payload = frame.render(5)
        stream.feed(payload)
        self.assertEqual(screen.display[:4], old[:4])
        self.assertEqual(screen.display[9:], old[9:])
        self.assertIn("ish stream | native shell", screen.display[8])
        self.assertEqual(screen.buffer[8][0].bg, "default")
        for forbidden in (b"\x1b[?", b"\x1b]", b"\x1bP", b"\x1b[2J"):
            self.assertNotIn(forbidden, payload)

    async def test_actual_ptk_editing_returns_popup_text_once(self):
        """Only the popup consumes text; PTK editing produces the accepted result."""
        self.panel.show()
        self.assertTrue(self.panel.open_popup())
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.focus, [True])
        self.assertTrue(self.panel.feed("hello 한글".encode()))
        self.assertTrue(self.panel.feed(b"\x7f!"))
        frame = await self.frame_matching(lambda value: "hello 한!" in value.lines[1])
        self.assertTrue(frame.popup)
        self.assertEqual(frame.cursor[1], 1)
        self.assertTrue(self.panel.feed(b"\r"))
        self.assertEqual(self.results, ["hello 한!"])
        self.assertEqual(self.focus, [True, False])
        self.assertFalse(self.panel.feed(b"native"))
        self.assertFalse(self.panel.close_popup("stale"))
        self.assertEqual(self.results, ["hello 한!"])

    async def test_cancelled_input_cannot_reach_next_popup(self):
        """Partial UTF-8 and escape fragments belong to the cancelled popup."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"old\xe3")
        self.panel.close_popup()
        self.panel.open_popup()
        self.panel.feed(b"new\rSHOULD_NOT_LEAK")
        self.assertEqual(self.results, [None, "new"])
        self.assertEqual(self.remainders, [b"SHOULD_NOT_LEAK"])
        self.panel.open_popup()
        self.panel.feed(b"\r")
        self.assertEqual(self.results, [None, "new", ""])

    async def test_arrows_and_fragmented_unicode_use_ptk_key_parser(self):
        """A split arrow sequence must edit the popup instead of cancelling it."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"ab\x1b[")
        self.panel.feed(b"D")
        text = "界".encode()
        self.panel.feed(text[:1])
        self.panel.feed(text[1:])
        self.panel.feed(b"\r")
        self.assertEqual(self.results, ["a界b"])

    async def test_hide_revokes_focus_and_suppresses_queued_frames(self):
        """Leaving the prompt cancels UI before any subsequent input routing."""
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        count = len(self.frames)
        self.panel.hide()
        self.assertFalse(self.panel.popup)
        self.assertFalse(self.panel.feed(b"shell input"))
        self.assertEqual(self.results, [None])
        self.assertEqual(self.focus, [True, False])
        await asyncio.sleep(0)
        self.assertEqual(len(self.frames), count)

    async def test_resize_clips_wide_cells_and_cancels_too_small_popup(self):
        """Narrow viewports cannot write beyond the caller's reserved columns."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed("界界界".encode())
        self.panel.resize(7, 4)
        frame = await self.frame_matching(lambda value: value.columns == 7)
        screen = pyte.Screen(15, 10)
        stream = pyte.ByteStream(screen)
        stream.feed(b"\x1b[4;8HKEEP")
        stream.feed(frame.render(3))
        self.assertEqual(
            "".join(screen.buffer[3][col].data for col in range(7, 11)), "KEEP"
        )
        self.panel.resize(7, 2)
        self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [None])
        self.assertFalse(self.panel.open_popup())

    async def test_escape_interrupt_and_eof_cancel_only_popup(self):
        """Popup cancel keys never become shell interrupts or terminal output."""
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        self.panel.feed(b"\x1b")
        await self.frame_matching(lambda value: not value.popup)
        self.assertEqual(self.results, [None])
        for key in (b"\x03", b"\x04"):
            self.panel.open_popup()
            self.assertTrue(self.panel.feed(key))
            self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [None, None, None])
        self.assertEqual(self.focus, [True, False, True, False, True, False])

    async def test_accept_and_cancel_return_the_original_unconsumed_suffix(self):
        """One read can end popup input and retain exact native typeahead bytes."""
        self.panel.show()
        tail = b"next command\r\n\xe3\x81\x82\xff"
        for key, result in (
            (b"\r", "value"),
            (b"\n", "value"),
            (b"\x03", None),
            (b"\x04", None),
        ):
            with self.subTest(key=key):
                self.panel.open_popup()
                self.panel.feed(b"value" + key + tail)
                self.assertEqual(self.results[-1], result)
                self.assertEqual(self.remainders[-1], tail)
                self.assertFalse(self.panel.popup)
        self.assertEqual(len(self.remainders), 4)

    async def test_paste_newlines_remain_popup_data_until_explicit_accept(self):
        """Multiline paste cannot become a native command through suffix routing."""
        self.panel.show()
        self.panel.open_popup()
        text = "first\nsecond 한글\x03\x04"
        self.panel.feed(b"\x1b[200~" + text.encode() + b"\x1b[201~")
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.results, [])
        self.assertEqual(self.remainders, [])
        self.panel.feed(b"\rAFTER")
        self.assertEqual(self.results, [text])
        self.assertEqual(self.remainders, [b"AFTER"])

    async def test_alt_editing_does_not_cancel_or_release_popup_input(self):
        """Escape plus a character keeps PTK's normal Meta-key interpretation."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\x1bz")
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"one two\x1bb")
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.remainders, [])
        self.panel.feed(b"X\rTAIL")
        self.assertEqual(self.results, ["one Xtwo"])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_large_paste_does_not_block_a_pipe_or_reach_shell(self):
        """A popup receives a bracketed paste through the PTK editing path."""
        self.panel.show()
        self.panel.open_popup()
        text = "word " * 2000
        self.assertTrue(self.panel.feed(b"\x1b[200~" + text.encode() + b"\x1b[201~"))
        self.panel.feed(b"\r")
        self.assertEqual(self.results, [text])


if __name__ == "__main__":
    unittest.main()
