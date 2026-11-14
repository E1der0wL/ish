"""Verify mouse decoding against literal XTerm wire reports and cell positions."""

import unittest

from ish.terminal.mouse import MouseEvent, decode_mouse
from ish.terminal.vt import ALT, CTRL, SHIFT


class MouseDecodeTests(unittest.TestCase):
    """Check external mouse input without using a production encoder as an oracle."""

    def test_sgr_buttons_releases_and_motion(self):
        """SGR identifies release buttons and distinguishes drag from hover."""
        cases = (
            ("\x1b[<0;5;9M", "press", "left"),
            ("\x1b[<1;5;9M", "press", "middle"),
            ("\x1b[<2;5;9M", "press", "right"),
            ("\x1b[<0;5;9m", "release", "left"),
            ("\x1b[<1;5;9m", "release", "middle"),
            ("\x1b[<2;5;9m", "release", "right"),
            ("\x1b[<32;5;9M", "move", "left"),
            ("\x1b[<33;5;9M", "move", "middle"),
            ("\x1b[<34;5;9M", "move", "right"),
            ("\x1b[<35;5;9M", "move", None),
        )
        for wire, action, button in cases:
            with self.subTest(wire=repr(wire)):
                self.assertEqual(
                    decode_mouse(wire), MouseEvent(action, button, 4, 8, 0)
                )

    def test_sgr_modifiers_keep_their_vt_meaning(self):
        """Wire Shift/Alt/Control bits map to the public VT modifier constants."""
        cases = (
            ("\x1b[<4;1;1M", "press", "left", SHIFT),
            ("\x1b[<8;1;1M", "press", "left", ALT),
            ("\x1b[<16;1;1M", "press", "left", CTRL),
            ("\x1b[<28;1;1M", "press", "left", SHIFT | ALT | CTRL),
            ("\x1b[<61;1;1M", "move", "middle", SHIFT | ALT | CTRL),
            ("\x1b[<30;1;1m", "release", "right", SHIFT | ALT | CTRL),
            ("\x1b[<63;1;1M", "move", None, SHIFT | ALT | CTRL),
        )
        for wire, action, button, modifiers in cases:
            with self.subTest(wire=repr(wire)):
                self.assertEqual(
                    decode_mouse(wire), MouseEvent(action, button, 0, 0, modifiers)
                )

    def test_vertical_and_horizontal_wheels_are_press_events(self):
        """All four wheel directions retain their identities and modifier bits."""
        cases = (
            ("\x1b[<64;12;3M", "wheelup", 0),
            ("\x1b[<65;12;3M", "wheeldown", 0),
            ("\x1b[<66;12;3M", "wheelleft", 0),
            ("\x1b[<67;12;3M", "wheelright", 0),
            ("\x1b[<92;12;3M", "wheelup", SHIFT | ALT | CTRL),
            ("\x1b[<93;12;3M", "wheeldown", SHIFT | ALT | CTRL),
        )
        for wire, button, modifiers in cases:
            with self.subTest(wire=repr(wire)):
                self.assertEqual(
                    decode_mouse(wire), MouseEvent("press", button, 11, 2, modifiers)
                )

    def test_legacy_x10_format_preserves_unknown_release(self):
        """Old byte reports cannot identify which button a release belongs to."""
        cases = (
            ("\x1b[M !!", MouseEvent("press", "left", 0, 0, 0)),
            ('\x1b[M!"#', MouseEvent("press", "middle", 1, 2, 0)),
            ('\x1b[M"$%', MouseEvent("press", "right", 3, 4, 0)),
            ("\x1b[M#*+", MouseEvent("release", None, 9, 10, 0)),
            ("\x1b[M<!!", MouseEvent("press", "left", 0, 0, SHIFT | ALT | CTRL)),
            ("\x1b[M@!!", MouseEvent("move", "left", 0, 0, 0)),
            ("\x1b[MC!!", MouseEvent("move", None, 0, 0, 0)),
            ("\x1b[M`!!", MouseEvent("press", "wheelup", 0, 0, 0)),
            ("\x1b[Ma!!", MouseEvent("press", "wheeldown", 0, 0, 0)),
        )
        for wire, expected in cases:
            with self.subTest(wire=repr(wire)):
                self.assertEqual(decode_mouse(wire), expected)

    def test_legacy_high_bytes_survive_surrogateescape(self):
        """Raw high coordinates from PTK's UTF-8 reader remain physical cell offsets."""
        wire = b"\x1b[M \xff\x80".decode("utf-8", errors="surrogateescape")
        self.assertEqual(decode_mouse(wire), MouseEvent("press", "left", 222, 95, 0))
        self.assertEqual(
            decode_mouse("\x1b[M \xff\x80"), MouseEvent("press", "left", 222, 95, 0)
        )

    def test_urxvt_has_x10_button_offset_but_decimal_coordinates(self):
        """1015 decimal button values include 32; coordinates do not."""
        cases = (
            ("\x1b[32;301;202M", "press", "left", 0),
            ("\x1b[35;301;202M", "release", None, 0),
            ("\x1b[60;301;202M", "press", "left", SHIFT | ALT | CTRL),
            ("\x1b[64;301;202M", "move", "left", 0),
            ("\x1b[67;301;202M", "move", None, 0),
            ("\x1b[96;301;202M", "press", "wheelup", 0),
            ("\x1b[97;301;202M", "press", "wheeldown", 0),
            ("\x1b[98;301;202M", "press", "wheelleft", 0),
            ("\x1b[99;301;202M", "press", "wheelright", 0),
        )
        for wire, action, button, modifiers in cases:
            with self.subTest(wire=repr(wire)):
                self.assertEqual(
                    decode_mouse(wire), MouseEvent(action, button, 300, 201, modifiers)
                )

    def test_cell_coordinates_are_not_unicode_string_indices(self):
        """A click after A한🙂 stays at physical column six, not string index three."""
        self.assertEqual(
            decode_mouse("\x1b[<0;6;2M"), MouseEvent("press", "left", 5, 1, 0)
        )
        # The right half of a wide glyph must not be folded onto its left half.
        self.assertEqual(
            decode_mouse("\x1b[<0;3;2M"), MouseEvent("press", "left", 2, 1, 0)
        )
        self.assertEqual(
            decode_mouse("\x1b[<0;65535;65535M"),
            MouseEvent("press", "left", 65534, 65534, 0),
        )

    def test_malformed_out_of_range_and_unsupported_reports_are_rejected(self):
        """Invalid mouse input cannot turn into a command, wheel, or guessed release."""
        cases = (
            "",
            "echo injected",
            "\x1b[6n",
            "\x1b[1;2R",
            "\x1b[<0;1;1Mtail",
            "\x1b[<0;1;1M\n",
            "\x1b[<0;1;1M\x1b[<0;2;2M",
            "\x1b[<0;1M",
            "\x1b[<;1;1M",
            "\x1b[<0;1;1;2M",
            "\x1b[<0;0;1M",
            "\x1b[<0;1;0M",
            "\x1b[<0;-1;1M",
            "\x1b[<0;65536;1M",
            "\x1b[<0;1;65536M",
            "\x1b[<0;99999999999999999999;1M",
            "\x1b[<0;+1;1M",
            "\x1b[<0; 1;1M",
            "\x1b[<0;１;1M",
            "\x1b[<128;1;1M",
            "\x1b[<255;1;1M",
            "\x1b[160;1;1M",
            "\x1b[<3;1;1M",
            "\x1b[<3;1;1m",
            "\x1b[<32;1;1m",
            "\x1b[<64;1;1m",
            "\x1b[<96;1;1M",
            "\x1b[31;1;1M",
            "\x1b[32;1;1m",
            "\x1b[32;0;1M",
            "\x1b[M",
            "\x1b[M !!x",
            "\x1b[M  !",
            "\x1b[M ! ",
            "\x1b[M\x1f!!",
            "\x1b[M\xa0!!",
            "\x1b[M \u0100!",
            "\x1b[M \ud800!",
            "\x1b[M \udc00!",
        )
        for wire in cases:
            with self.subTest(wire=repr(wire)):
                self.assertIsNone(decode_mouse(wire))


if __name__ == "__main__":
    unittest.main()
