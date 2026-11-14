"""Verify observable terminal behavior through the actual libghostty binding."""

import unittest

from prompt_toolkit.styles import Style

from ish.terminal.vt import CTRL, Terminal, TerminalError


def text(frame, row=0):
    """Read visible characters independently of cell style coalescing."""
    return "".join(value for _, value in frame.lines[row]).rstrip()


def attributes(frame, character):
    """Resolve a visible run through PTK's public style parser."""
    style = next(style for style, value in frame.lines[0] if character in value)
    return Style.from_dict({}).get_attrs_for_style_str(style)


def displayed_colors(frame, character):
    """Resolve PTK default channels and inverse to logical host colors."""
    attrs = attributes(frame, character)
    foreground = "host-fg" if attrs.color == "default" else attrs.color
    background = "host-bg" if attrs.bgcolor == "default" else attrs.bgcolor
    return (background, foreground) if attrs.reverse else (foreground, background)


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

    def test_default_colors_and_sgr_resets_keep_host_defaults(self):
        """Default, SGR 0/39/49, and explicit black retain distinct meanings."""
        with Terminal(20, 4, lambda _: None) as terminal:
            terminal.feed(
                b"A\x1b[30;40mB\x1b[0mC\x1b[38;2;0;0;0;48;2;0;0;0mD\x1b[39mE\x1b[49mF"
            )
            frame = terminal.frame()
            self.assertEqual(text(frame), "ABCDEF")
            for character in "ACF":
                with self.subTest(character=character):
                    attrs = attributes(frame, character)
                    self.assertEqual(
                        (attrs.color, attrs.bgcolor), ("default", "default")
                    )
            ansi_black = attributes(frame, "B")
            self.assertNotEqual(ansi_black.color, "default")
            self.assertNotEqual(ansi_black.bgcolor, "default")
            black = attributes(frame, "D")
            self.assertEqual((black.color, black.bgcolor), ("000000", "000000"))
            partial_reset = attributes(frame, "E")
            self.assertEqual(
                (partial_reset.color, partial_reset.bgcolor), ("default", "000000")
            )

    def test_inverse_keeps_defaults_and_explicit_black(self):
        """SGR inverse swaps semantic colors at the host without inventing RGBs."""
        with Terminal(20, 4, lambda _: None) as terminal:
            terminal.feed(b"\x1b[7mR\x1b[27mN\x1b[48;2;0;0;0;7mB\x1b[0mD")
            frame = terminal.frame()
            reversed_default = attributes(frame, "R")
            self.assertTrue(reversed_default.reverse)
            self.assertEqual(
                (reversed_default.color, reversed_default.bgcolor),
                ("default", "default"),
            )
            self.assertFalse(attributes(frame, "N").reverse)
            reversed_black = attributes(frame, "B")
            self.assertTrue(reversed_black.reverse)
            self.assertEqual(
                (reversed_black.color, reversed_black.bgcolor), ("default", "000000")
            )
            reset = attributes(frame, "D")
            self.assertFalse(reset.reverse)
            self.assertEqual((reset.color, reset.bgcolor), ("default", "default"))

    def test_erase_retains_explicit_background_and_then_resets(self):
        """Blank background-only cells retain their explicit color after erase."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(b"\x1b[48;2;0;0;0m\x1b[2J")
            for line in terminal.frame().lines:
                for style, value in line:
                    self.assertEqual(value.strip(), "")
                    self.assertEqual(
                        Style.from_dict({}).get_attrs_for_style_str(style).bgcolor,
                        "000000",
                    )
            terminal.feed(b"\x1b[49m\x1b[2J")
            for line in terminal.frame().lines:
                for style, _ in line:
                    attrs = Style.from_dict({}).get_attrs_for_style_str(style)
                    self.assertEqual(
                        (attrs.color, attrs.bgcolor), ("default", "default")
                    )

    def test_osc_default_colors_and_resets_keep_explicit_black(self):
        """Program-selected default colors apply to existing cells until OSC reset."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(b"A\x1b]10;#010203\x07\x1b]11;#000000\x07")
            changed = attributes(terminal.frame(), "A")
            self.assertEqual((changed.color, changed.bgcolor), ("010203", "000000"))
            terminal.feed(b"\x1b[0mB")
            sgr_reset = attributes(terminal.frame(), "B")
            self.assertEqual((sgr_reset.color, sgr_reset.bgcolor), ("010203", "000000"))
            terminal.feed(b"\x1b]110\x07\x1b]111\x07")
            for character in "AB":
                attrs = attributes(terminal.frame(), character)
                self.assertEqual((attrs.color, attrs.bgcolor), ("default", "default"))

    def test_reverse_video_swaps_osc_defaults_but_keeps_cell_colors(self):
        """DECSCNM changes fallback colors without reversing explicit cell colors."""
        with Terminal(20, 4, lambda _: None) as terminal:
            terminal.feed(
                b"\x1b]10;#112233\x07\x1b]11;#445566\x07"
                b"D\x1b[38;2;171;205;239mF\x1b[0;48;2;18;52;86mB"
                b"\x1b[38;2;171;205;239mE\x1b[0;7mR\x1b[0m\x1b[?5h"
            )
            reversed_frame = terminal.frame()
            expected = {
                "D": ("445566", "112233"),
                "F": ("abcdef", "112233"),
                "B": ("445566", "123456"),
                "E": ("abcdef", "123456"),
                "R": ("112233", "445566"),
            }
            for character, colors in expected.items():
                with self.subTest(character=character, reverse_video=True):
                    self.assertEqual(
                        displayed_colors(reversed_frame, character), colors
                    )
            terminal.feed(b"\x1b[?5l")
            restored = terminal.frame()
            expected = {
                "D": ("112233", "445566"),
                "F": ("abcdef", "445566"),
                "B": ("112233", "123456"),
                "E": ("abcdef", "123456"),
                "R": ("445566", "112233"),
            }
            for character, colors in expected.items():
                with self.subTest(character=character, reverse_video=False):
                    self.assertEqual(displayed_colors(restored, character), colors)

    def test_reverse_video_handles_host_and_partial_osc_defaults(self):
        """Unset defaults keep their host FG/BG identity across screen inversion."""
        cases = (
            (b"", "host-fg", "host-bg"),
            (b"\x1b]10;#112233\x07", "112233", "host-bg"),
            (b"\x1b]11;#445566\x07", "host-fg", "445566"),
        )
        for setup, foreground, background in cases:
            with self.subTest(setup=setup), Terminal(20, 4, lambda _: None) as terminal:
                terminal.feed(
                    setup + b"D\x1b[38;2;171;205;239mF\x1b[0;48;2;18;52;86mB"
                    b"\x1b[38;2;171;205;239mE\x1b[0;7mR\x1b[0m\x1b[?5h"
                )
                frame = terminal.frame()
                self.assertEqual(displayed_colors(frame, "D"), (background, foreground))
                self.assertEqual(displayed_colors(frame, "F"), ("abcdef", foreground))
                self.assertEqual(displayed_colors(frame, "B"), (background, "123456"))
                self.assertEqual(displayed_colors(frame, "E"), ("abcdef", "123456"))
                self.assertEqual(displayed_colors(frame, "R"), (foreground, background))
                terminal.feed(b"\x1b[?5l")
                restored = terminal.frame()
                self.assertEqual(
                    displayed_colors(restored, "D"), (foreground, background)
                )
                self.assertEqual(
                    displayed_colors(restored, "F"), ("abcdef", background)
                )
                self.assertEqual(
                    displayed_colors(restored, "B"), (foreground, "123456")
                )
                self.assertEqual(displayed_colors(restored, "E"), ("abcdef", "123456"))
                self.assertEqual(
                    displayed_colors(restored, "R"), (background, foreground)
                )

    def test_palette_override_retains_the_program_selected_rgb(self):
        """Indexed colors remain resolved by the VT, including OSC palette changes."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(b"\x1b]4;1;#123456\x07\x1b[31;41mA")
            attrs = attributes(terminal.frame(), "A")
            self.assertEqual((attrs.color, attrs.bgcolor), ("123456", "123456"))

    def test_closed_terminal_rejects_further_use(self):
        """Repeated cleanup is safe and cannot access freed native state."""
        terminal = Terminal(12, 4, lambda _: None)
        terminal.close()
        terminal.close()
        with self.assertRaises(TerminalError):
            terminal.feed(b"text")


if __name__ == "__main__":
    unittest.main()
