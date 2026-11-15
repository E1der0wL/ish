"""Check prompt-panel reservation against an independent terminal screen."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

import pyte

from ish.stream.app import App
from ish.stream.protocol import Stream

COLUMNS = 80
ROWS = 24
BLANK = " " * COLUMNS
CASES = ((1, 0, 1), (8, 0, 8), (19, 0, 19), (20, 1, 19), (22, 3, 19), (24, 5, 19))


class ReverseWrapScreen(pyte.HistoryScreen):
    """Model the host's enabled reverse-wrap behavior for the BS fixture."""

    def backspace(self):
        """Move across the previous row at its left edge, as this host mode allows."""
        if self.cursor.x == 0 and self.cursor.y > 0:
            self.cursor.y -= 1
            self.cursor.x = self.columns - 1
        else:
            super().backspace()


class ScrollTests(unittest.TestCase):
    """Compare actual App output with literal cursor and scroll expectations."""

    def make_app(self, row, column, *, reusable, screen_type=pyte.HistoryScreen):
        """Populate native output up to the cursor, with blank space below it."""
        screen = screen_type(COLUMNS, ROWS, history=100)
        terminal = pyte.ByteStream(screen)
        for number in range(1, row + 1):
            terminal.feed(f"\x1b[{number};1Hnative row {number:02}".encode())
        terminal.feed(f"\x1b[{row};{column}H".encode())
        app = App(["tcsh"], Path("/tmp"))
        app.rows, app.cols = ROWS, COLUMNS
        app.reusable = reusable
        app.panel = Mock()
        app.session = Mock()
        app.writer = Mock()
        app.writer.write.side_effect = terminal.feed
        return app, screen

    def assert_no_query_or_resize(self, app):
        """Decoration must not request input responses or notify a fake resize."""
        data = b"".join(call.args[0] for call in app.writer.write.call_args_list)
        self.assertNotIn(b"\x1b[6n", data)
        self.assertNotIn(b"\x1b[?6n", data)
        app.session.resize.assert_not_called()
        app.panel.show.assert_called_once_with()

    def test_reuse_scrolls_only_rows_that_do_not_fit(self):
        """Reserve five blank rows without scrolling when they already fit."""
        for row, scroll, end_row in CASES:
            for column in (1, 10, 80):
                with self.subTest(row=row, column=column):
                    app, screen = self.make_app(row, column, reusable=True)
                    before = list(screen.display)

                    app._reserve()

                    self.assertEqual(screen.display, before[scroll:] + [BLANK] * scroll)
                    self.assertEqual(screen.display[19:], [BLANK] * 5)
                    self.assertEqual(
                        (screen.cursor.y + 1, screen.cursor.x + 1), (end_row, column)
                    )
                    self.assertEqual(len(screen.history.top), scroll)
                    history = [
                        "".join(line[index].data for index in range(COLUMNS))
                        for line in screen.history.top
                    ]
                    self.assertEqual(history, before[:scroll])
                    self.assert_no_query_or_resize(app)

    def test_unknown_layout_reserves_fresh_rows(self):
        """An initial full screen keeps old output in order and clears the panel."""
        app, screen = self.make_app(24, 10, reusable=False)
        before = list(screen.display)

        app._reserve()

        self.assertEqual(screen.display, before[5:] + [BLANK] * 5)
        self.assertEqual((screen.cursor.y + 1, screen.cursor.x + 1), (19, 10))
        history = [
            "".join(line[index].data for index in range(COLUMNS))
            for line in screen.history.top
        ]
        self.assertEqual(history, before[:5])
        self.assert_no_query_or_resize(app)

    def test_backward_native_output_does_not_reuse_occupied_rows(self):
        """Moving above existing text revokes reuse before the next primary."""
        app, screen = self.make_app(24, 10, reusable=True)
        before = list(screen.display)
        app.stream = Stream(
            "scroll-test", app._forward, app._event, app._unsafe, rewind=app._rewind
        )

        app._output(b"\x1b[6A\x1b]777;ish;scroll-test;1;P\x07")

        self.assertEqual(screen.display, before[5:] + [BLANK] * 5)
        self.assertEqual(screen.display[18].rstrip(), "native row 24")
        self.assertEqual(screen.display[19:], [BLANK] * 5)
        self.assert_no_query_or_resize(app)

    def test_native_backspace_can_leave_content_below_the_cursor(self):
        """An inherited reverse-wrap mode must not cause panel content loss."""
        app, screen = self.make_app(20, 1, reusable=True, screen_type=ReverseWrapScreen)
        before = list(screen.display)
        app.stream = Stream(
            "scroll-test", app._forward, app._event, app._unsafe, rewind=app._rewind
        )

        app._output(b"\b\x1b]777;ish;scroll-test;1;P\x07")

        self.assertEqual(screen.display, before[5:] + [BLANK] * 5)
        self.assertEqual(screen.display[14].rstrip(), "native row 20")
        self.assertEqual(screen.display[19:], [BLANK] * 5)
        self.assert_no_query_or_resize(app)

    def test_explicit_erase_reuses_blank_space_without_scrolling(self):
        """Native ED0 confirms spare rows even after an unknown initial layout."""
        app, screen = self.make_app(8, 10, reusable=False)
        app.stream = Stream(
            "scroll-test",
            app._forward,
            app._event,
            app._unsafe,
            rewind=app._rewind,
            cleared=app._cleared,
        )
        before = list(screen.display)

        app._output(b"\x1b[J\x1b[K\x1b[?1h\x1b=\x1b]777;ish;scroll-test;1;P\x07")

        self.assertEqual(screen.display[:7], before[:7])
        self.assertEqual(len(screen.history.top), 0)
        self.assertEqual((screen.cursor.y + 1, screen.cursor.x + 1), (8, 10))
        self.assertEqual(screen.display[19:], [BLANK] * 5)
        self.assert_no_query_or_resize(app)


if __name__ == "__main__":
    unittest.main()
