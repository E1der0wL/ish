"""Verify native gesture results and frontend timer lifetime without text parsing."""

import asyncio
import base64
import os
import unittest
from unittest.mock import patch

from ish.terminal.selection import SelectionController
from ish.terminal.vt import Terminal
from test_terminal_image_protocol import iterm, png


@unittest.skipUnless(os.name == "posix", "Native VT requires POSIX")
class SelectionTests(unittest.IsolatedAsyncioTestCase):
    """Read selected native text, never compute expected ranges through its helpers."""

    async def asyncSetUp(self):
        """Create an actual native grid and observe copied text."""
        self.terminal = Terminal(12, 4, lambda data: None)
        self.copied = []
        self.changed = 0

        def changed():
            self.changed += 1

        self.controller = SelectionController(
            self.terminal,
            changed=changed,
            copied=lambda: self.copied.append(self.terminal.selection_text()),
        )

    async def asyncTearDown(self):
        """Stop timer ownership before freeing the native terminal."""
        self.controller.close()
        self.terminal.close()

    async def test_single_double_and_triple_click_use_native_word_and_line_ranges(self):
        """Repeated clicks keep the native sequence across release boundaries."""
        self.terminal.feed(b"hello world")
        for count in range(1, 4):
            with patch(
                "ish.terminal.selection.time.monotonic_ns",
                return_value=count * 100_000_000,
            ):
                state = self.controller.press(2, 0)
                self.controller.release(2, 0)
            self.assertEqual(state.click_count, count)
        self.assertEqual(self.copied, ["hello", "hello world"])
        self.assertEqual(self.terminal.selection_text(), "hello world")
        self.assertFalse(self.controller.active)

    async def test_rectangle_keeps_press_modifier_when_drag_reports_no_alt(self):
        """Rectangle shape survives repeated motion and uses native grapheme ranges."""
        self.terminal.feed("A한BC\r\n12345\r\nUVWXY".encode())
        self.controller.press(1, 0, rectangle=True)
        self.controller.drag(4, 1)
        self.controller.release(4, 1)
        self.assertEqual(self.copied, ["한B\n234"])

    async def test_iterm_image_copy_keeps_blank_cells_unicode_and_newlines(self):
        """Actual inline image cells copy as spaces, preserving adjacent graphemes."""
        self.terminal.resize(12, 4, 2, 3)
        self.terminal.set_graphics(True)
        self.terminal.feed("한e\u0301".encode() + iterm(png(4, 3)))
        self.terminal.feed("가o\u0308👩\u200d💻\r\n둘째 줄".encode())
        self.assertTrue(self.terminal.frame().images)
        self.controller.press(0, 0)
        self.controller.drag(6, 1)
        self.controller.release(6, 1)
        self.assertEqual(self.copied, ["한e\u0301  가o\u0308👩\u200d💻\n둘째 줄"])
        self.assertEqual(self.terminal.selection_text(), self.copied[0])

    async def test_kitty_placeholder_copy_drops_only_image_grapheme_metadata(self):
        """Native Kitty cells omit row, column and high-ID marks from copied text."""
        self.terminal.resize(12, 4, 2, 3)
        self.terminal.set_graphics(True)
        self.terminal.feed(
            b"\x1b_Ga=T,U=1,i=17,f=32,s=4,v=3,c=2,r=1,C=1,q=2;"
            + base64.b64encode(b"\xff\0\0\xff" * 12)
            + b"\x1b\\A\x1b[38;2;0;0;17;58;2;0;0;0m"
            + "\U0010eeee\u0305\u0305\u0305\u0301\U0010eeee\u0305\u030d".encode()
            + "\x1b[0me\u0301한\r\n끝".encode()
        )
        self.assertTrue(self.terminal.frame().images)
        self.controller.press(0, 0)
        self.controller.drag(1, 1)
        self.controller.release(1, 1)
        self.assertEqual(self.copied, ["A  e\u0301한\n끝"])

    async def test_text_only_copy_preserves_combining_and_emoji_graphemes(self):
        """Ordinary Unicode is returned unchanged when there are no image cells."""
        self.terminal.feed("e\u0301한👩\u200d💻\r\nA\u0305B\u030d".encode())
        self.controller.press(0, 0)
        self.controller.drag(2, 1)
        self.controller.release(2, 1)
        self.assertEqual(self.copied, ["e\u0301한👩\u200d💻\nA\u0305B\u030d"])

    async def test_reset_preserves_highlight_but_cancels_repeat_click_sequence(self):
        """Focus cancellation must not turn a later click into an old double-click."""
        self.terminal.feed(b"hello world")
        for _ in range(2):
            self.controller.press(2, 0)
            self.controller.release(2, 0)
        self.controller.reset()
        self.assertEqual(self.terminal.selection_text(), "hello")
        state = self.controller.press(2, 0)
        self.controller.release(2, 0)
        self.assertEqual(state.click_count, 1)
        self.assertEqual(self.copied, ["hello"])

    async def test_outside_pointer_scrolls_history_and_reset_stops_ticks(self):
        """Holding beyond the pane changes native history until capture is cancelled."""
        self.terminal.feed(b"\r\n".join(f"row{i:02}".encode() for i in range(20)))
        self.terminal.scroll(-5)
        before = self.terminal.frame().viewport_offset
        self.controller.press(0, 2)
        with patch.object(SelectionController, "AUTOSCROLL_SECONDS", 0.005):
            state = self.controller.drag(4, -1)
            self.assertEqual(state.autoscroll, -1)
            async with asyncio.timeout(2):
                while self.terminal.frame().viewport_offset >= before:
                    await asyncio.sleep(0.001)
            self.controller.reset()
            stopped = self.terminal.frame().viewport_offset
            text = self.terminal.selection_text()
            self.assertLess(stopped, before)
            self.assertIn("row", text)
            await asyncio.sleep(0.03)
        self.assertEqual(self.terminal.frame().viewport_offset, stopped)
        self.assertEqual(self.terminal.selection_text(), text)
        self.assertEqual(self.copied, [])

    async def test_cancelled_timer_cannot_mutate_a_new_drag_or_closed_terminal(self):
        """Already queued callbacks retain their old capture identity."""
        self.terminal.feed(b"\r\n".join(f"row{i:02}".encode() for i in range(20)))
        self.terminal.scroll(-5)
        callbacks = []

        class Timer:
            def cancel(self):
                pass  # Deliberately deliver cancelled callbacks like a late event.

        def schedule(delay, callback, *arguments):
            callbacks.append((callback, arguments))
            return Timer()

        loop = asyncio.get_running_loop()
        with patch.object(loop, "call_later", schedule):
            self.controller.press(0, 2)
            self.controller.drag(4, -1)
            self.controller.reset()
            self.controller.press(1, 1)
            self.controller.drag(3, 1)
        self.assertTrue(callbacks)
        before = self.terminal.frame().viewport_offset
        text = self.terminal.selection_text()
        for callback, arguments in callbacks:
            callback(*arguments)
        self.assertEqual(self.terminal.frame().viewport_offset, before)
        self.assertEqual(self.terminal.selection_text(), text)
        self.controller.close()
        self.terminal.close()
        for callback, arguments in callbacks:
            callback(*arguments)
