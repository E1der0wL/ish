"""Verify observable terminal behavior through the actual libghostty binding."""

import unittest

from ish.terminal.vt import CTRL, Terminal, TerminalError


def text(frame, row=0):
    """Read visible characters independently of cell style coalescing."""
    return "".join(value for _, value in frame.lines[row]).rstrip()


class TerminalVTTests(unittest.TestCase):
    """Use fixed VT protocol expectations rather than a second production parser."""

    def test_split_unicode_and_wide_cells_keep_text_and_cursor(self):
        """UTF-8 boundaries, wide tails, and combining marks do not add characters."""
        content = "A한e\u0301Z"
        data = content.encode()
        for split in range(len(data) + 1):
            with self.subTest(split=split), Terminal(12, 4, lambda _: None) as terminal:
                terminal.feed(data[:split])
                terminal.feed(data[split:])
                frame = terminal.frame()
                self.assertEqual(text(frame), content)
                self.assertEqual((frame.cursor_x, frame.cursor_y), (5, 0))

    def test_cursor_erase_and_rgb_style(self):
        """Cursor motion and erase preserve the remaining cells and explicit style."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(b"abc\x1b[2D\x1b[K\x1b[1;38;2;10;20;30mZ\x1b[0m")
            frame = terminal.frame()
            self.assertEqual(text(frame), "aZ")
            style = next(style for style, value in frame.lines[0] if "Z" in value)
            self.assertIn("fg:#0a141e", style)
            self.assertIn("bold", style)
            self.assertEqual((frame.cursor_x, frame.cursor_y), (2, 0))

    def test_alternate_screen_restores_primary_and_cursor(self):
        """A TUI's alternate screen never destroys the saved shell screen."""
        with Terminal(20, 4, lambda _: None) as terminal:
            # 1049 switches/clears the screen; CUP explicitly places TUI output.
            terminal.feed(b"primary\x1b[?1049h\x1b[Halternate")
            frame = terminal.frame()
            self.assertTrue(frame.alternate)
            self.assertEqual(text(frame), "alternate")
            terminal.feed(b"\x1b[?1049l")
            frame = terminal.frame()
            self.assertFalse(frame.alternate)
            self.assertEqual(text(frame), "primary")
            self.assertEqual((frame.cursor_x, frame.cursor_y), (7, 0))

    def test_terminal_queries_reply_once_at_the_virtual_cursor(self):
        """The emulated cursor answers child DSR queries without querying the host."""
        replies = []
        with Terminal(20, 8, replies.append) as terminal:
            terminal.feed(b"\x1b[3;5H\x1b[6")
            self.assertEqual(replies, [])
            terminal.feed(b"n")
            self.assertEqual(b"".join(replies), b"\x1b[3;5R")
            terminal.feed(b"plain")
            self.assertEqual(b"".join(replies), b"\x1b[3;5R")

    def test_cursor_keys_paste_and_controls_follow_terminal_modes(self):
        """Native application cursor and bracketed paste modes drive input encoding."""
        with Terminal(20, 4, lambda _: None) as terminal:
            self.assertEqual(terminal.encode_key("up"), b"\x1b[A")
            self.assertEqual(terminal.encode_key("c", CTRL), b"\x03")
            self.assertEqual(terminal.encode_key("enter"), b"\r")
            self.assertEqual(terminal.encode_key("\n"), b"\n")
            self.assertEqual(terminal.encode_paste("한 글"), "한 글".encode())
            terminal.feed(b"\x1b[?1h\x1b[?2004h")
            self.assertEqual(terminal.encode_key("up"), b"\x1bOA")
            self.assertEqual(
                terminal.encode_paste("한 글"),
                b"\x1b[200~" + "한 글".encode() + b"\x1b[201~",
            )

    def test_resize_updates_the_grid_and_native_size_report(self):
        """The virtual grid and terminal size reply agree after a resize."""
        replies = []
        with Terminal(12, 4, replies.append) as terminal:
            terminal.feed(b"\x1b[2;3HZ")
            terminal.resize(30, 9)
            frame = terminal.frame()
            self.assertEqual((frame.cols, frame.rows), (30, 9))
            self.assertEqual(text(frame, 1), "  Z")
            terminal.feed(b"\x1b[18t")
            self.assertEqual(b"".join(replies), b"\x1b[8;9;30t")

    def test_closed_terminal_rejects_further_use(self):
        """Repeated cleanup is safe and cannot access freed native state."""
        terminal = Terminal(12, 4, lambda _: None)
        terminal.close()
        terminal.close()
        with self.assertRaises(TerminalError):
            terminal.feed(b"text")


if __name__ == "__main__":
    unittest.main()
