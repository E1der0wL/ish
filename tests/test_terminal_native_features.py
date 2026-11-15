"""Check appearance and selection through native VT replies and copied text."""

import os
import unittest

from ish.terminal.vt import Terminal


@unittest.skipUnless(os.name == "posix", "Native bridge requires POSIX")
class NativeFeaturesTests(unittest.TestCase):
    """Expected replies and selected words are independent of the bridge codec."""

    def setUp(self):
        """Create one native terminal with a captured child reply channel."""
        self.replies = []
        self.vt = Terminal(20, 4, self.replies.append)

    def tearDown(self):
        """Release native gesture, render and terminal resources."""
        self.vt.close()

    def test_host_colors_answer_queries_without_painting_default_cells(self):
        """Knowing the host RGB must not turn inherited backgrounds opaque."""
        self.vt.set_appearance(
            foreground=(1, 2, 3),
            background=(4, 5, 6),
            cursor=(7, 8, 9),
            scheme="dark",
        )
        self.vt.feed(b"X\x1b]10;?\x07\x1b]11;?\x07\x1b]12;?\x07\x1b[?996n")
        self.assertEqual(
            self.replies,
            [
                b"\x1b]10;rgb:0101/0202/0303\x07",
                b"\x1b]11;rgb:0404/0505/0606\x07",
                b"\x1b]12;rgb:0707/0808/0909\x07",
                b"\x1b[?997;1n",
            ],
        )
        frame = self.vt.frame()
        self.assertIsNone(frame.cells[0][0].fg)
        self.assertIsNone(frame.cells[0][0].bg)
        self.assertTrue(frame.cursor_default)
        self.assertIsNone(frame.cursor_color)

    def test_same_rgb_override_stays_explicit_across_host_change_and_reset(self):
        """Color equality does not erase the child's explicit override identity."""
        self.vt.set_appearance(
            foreground=(1, 2, 3), background=(4, 5, 6), cursor=(7, 8, 9)
        )
        self.vt.feed(b"\x1b]10;#010203\x07\x1b]11;#040506\x07\x1b]12;#070809\x07X")
        before = self.vt.frame()
        self.assertEqual(before.cells[0][0].fg, (1, 2, 3))
        self.assertEqual(before.cells[0][0].bg, (4, 5, 6))
        self.assertEqual(before.cursor_color, (7, 8, 9))
        self.assertFalse(before.cursor_default)
        self.vt.set_appearance(
            foreground=(21, 22, 23), background=(24, 25, 26), cursor=(27, 28, 29)
        )
        self.assertEqual(self.vt.frame().cells, before.cells)
        self.assertEqual(self.vt.frame().cursor_color, (7, 8, 9))
        self.vt.feed(b"\x1b]110\x07\x1b]111\x07\x1b]112\x07\x1b]11;?\x07")
        self.assertEqual(self.replies[-1], b"\x1b]11;rgb:1818/1919/1a1a\x07")
        self.assertIsNone(self.vt.frame().cells[0][0].bg)
        self.assertIsNone(self.vt.frame().cursor_color)

    def test_palette_updates_preserve_child_override_until_osc_reset(self):
        """A new host palette changes defaults while native OSC state survives."""
        palette = [None] * 256
        palette[1] = (11, 22, 33)
        self.vt.set_appearance(palette=palette)
        self.vt.feed(b"\x1b[31mR\x1b]4;1;?\x07")
        self.assertEqual(self.vt.frame().cells[0][0].fg, (11, 22, 33))
        self.assertEqual(self.replies[-1], b"\x1b]4;1;rgb:0b0b/1616/2121\x07")
        self.vt.feed(b"\x1b]4;1;#0b1621\x07")
        palette[1] = (44, 55, 66)
        self.vt.set_appearance(palette=palette)
        self.assertEqual(self.vt.frame().cells[0][0].fg, (11, 22, 33))
        self.vt.feed(b"\x1b]104;1\x07")
        self.assertEqual(self.vt.frame().cells[0][0].fg, (44, 55, 66))

    def test_color_scheme_notifications_follow_native_subscription(self):
        """Only subscribed children receive known, changed theme reports."""
        self.vt.set_appearance(scheme="dark")
        self.assertEqual(self.replies, [])
        self.vt.feed(b"\x1b[?2031h")
        self.replies.clear()
        self.vt.set_appearance(scheme="light")
        self.vt.set_appearance(scheme="light")
        self.assertEqual(self.replies, [b"\x1b[?997;2n"])
        self.vt.set_appearance()
        self.vt.feed(b"\x1b[?996n\x1b]10;?\x07")
        self.assertEqual(self.replies, [b"\x1b[?997;2n"])
        self.vt.feed(b"\x1b[?2031l")
        self.vt.set_appearance(scheme="dark")
        self.assertEqual(self.replies, [b"\x1b[?997;2n"])

    def test_hold_captures_exact_color_boundaries_inside_one_write(self):
        """Hold callbacks observe overrides without changing native defaults."""
        self.vt.set_appearance(background=(1, 2, 3), cursor=(4, 5, 6))
        self.vt.feed(
            b"\x1b]11;#010203\x07\x1b]12;#040506\x07X\x1b[?2026h\x1b]11;#aabbcc\x07"
        )
        held = self.vt.frame()
        self.assertEqual(held.cells[0][0].bg, (1, 2, 3))
        self.assertEqual(held.cursor_color, (4, 5, 6))
        self.vt.feed(b"\x1b[?2026l\x1b[?2026h\x1b]11;#ddeeff\x07")
        self.assertEqual(self.vt.frame().cells[0][0].bg, (170, 187, 204))
        self.vt.release_render_hold()
        self.assertEqual(self.vt.frame().cells[0][0].bg, (221, 238, 255))
        self.vt.feed(b"\x1b]11;?\x07")
        self.assertEqual(self.replies[-1], b"\x1b]11;rgb:dddd/eeee/ffff\x07")

    def test_ris_preserves_native_dynamic_color_behavior_and_host_defaults(self):
        """Keep native RIS behavior and reset dynamic colors only with their OSC."""
        self.vt.set_appearance(background=(10, 20, 30), cursor=(40, 50, 60))
        self.vt.feed(b"\x1b]11;#ff0000\x07\x1b]12;#00ff00\x07\x1bc\x1b]11;?\x07")
        self.assertEqual(self.replies[-1], b"\x1b]11;rgb:ffff/0000/0000\x07")
        self.assertEqual(self.vt.frame().cells[0][0].bg, (255, 0, 0))
        self.assertEqual(self.vt.frame().cursor_color, (0, 255, 0))
        self.vt.feed(b"\x1b]111\x07\x1b]112\x07\x1b]11;?\x07")
        self.assertEqual(self.replies[-1], b"\x1b]11;rgb:0a0a/1414/1e1e\x07")
        self.assertIsNone(self.vt.frame().cells[0][0].bg)
        self.assertIsNone(self.vt.frame().cursor_color)

    def test_overline_survives_snapshot_without_affecting_other_cells(self):
        """SGR 53 and 55 decorate exactly their native text ranges."""
        self.vt.feed(b"\x1b[53mA\x1b[55mB")
        cells = self.vt.frame().cells[0]
        self.assertTrue(cells[0].overline)
        self.assertFalse(cells[1].overline)

    def test_double_word_and_triple_line_use_native_click_sequence(self):
        """Repeat clicks select words and Unicode lines without frontend parsing."""
        self.vt.feed("hello 한글 world".encode())
        for count in (1, 2, 3):
            state = self.vt.selection(
                "press", 2, 0, time_ns=1_000_000_000 + count * 100_000_000
            )
            self.assertEqual(state.click_count, count)
            self.assertEqual(
                self.vt.selection_text(), ("", "hello", "hello 한글 world")[count - 1]
            )
            released = self.vt.selection("release", 2, 0)
            self.assertEqual(released.click_count, count)
        self.assertEqual(state.behavior, "line")

    def test_rectangular_selection_and_reset_preserve_native_text(self):
        """Rectangle extraction and gesture cancellation use native selections."""
        self.vt.feed(b"abcdef\r\nghijkl\r\nmnopqr")
        self.vt.selection("press", 1, 0, position=(1.1, 0.5), time_ns=1)
        state = self.vt.selection("drag", 3, 2, position=(3.9, 2.5), rectangle=True)
        self.assertTrue(state.dragged)
        self.assertEqual(state.autoscroll, 0)
        self.assertEqual(self.vt.selection_text(), "bcd\nhij\nnop")
        self.assertEqual(self.vt.selection("reset").click_count, 0)
        self.assertEqual(self.vt.selection_text(), "bcd\nhij\nnop")
        self.vt.clear_selection()
        self.assertEqual(self.vt.selection_text(), "")

    def test_autoscroll_extends_from_tracked_history_anchor_one_row_per_tick(self):
        """Dragging beyond the pane selects history and stops moving on release."""
        self.vt.resize(8, 3)
        self.vt.feed(b"zero\r\none\r\ntwo\r\nthree\r\nfour\r\nfive")
        self.vt.scroll_top()
        self.vt.selection("press", 0, 0, time_ns=1)
        state = self.vt.selection("drag", 2, 3)
        self.assertEqual(state.autoscroll, 1)
        self.assertEqual(self.vt.frame().viewport_offset, 0)
        self.vt.selection("autoscroll", 2, 3)
        self.assertEqual(self.vt.frame().viewport_offset, 1)
        self.assertEqual(self.vt.selection_text(), "zero\none\ntwo\nth")
        released = self.vt.selection("release", -1, 4)
        self.assertEqual(released.autoscroll, 0)
        self.vt.selection("autoscroll", 2, 3)
        self.assertEqual(self.vt.frame().viewport_offset, 1)


if __name__ == "__main__":
    unittest.main()
