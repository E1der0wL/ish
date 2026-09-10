"""Verify display text, styles, and control-sequence boundaries in ANSI prompts."""

import unittest

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_width

from ish.ui.ansi import ShellANSI


def visible(value):
    """Join only the visible characters from formatted ANSI fragments."""
    return "".join(
        text
        for style, text in to_formatted_text(value)
        if "[ZeroWidthEscape]" not in style
    )


class RecordingANSI(ShellANSI):
    """Test formatter recording complete control sequences to observe parser boundaries."""

    def __init__(self, value):
        """Prepare the observed control-sequence list and parse the ANSI input."""
        self.controls = []
        super().__init__(value)

    def handle_control(self, kind, payload):
        """Record completed control strings in the test observation list."""
        self.controls.append((kind, payload))
        super().handle_control(kind, payload)


class ShellANSITests(unittest.TestCase):
    """Verify prompt styles, character widths, and control-string leak prevention."""

    def test_reported_prompt(self):
        """Keep the reported OSC title text out of the visible prompt input line."""
        title = "user@DESKTOP-K9ID8T1: /mnt/d/Programs/ish"
        prompt = "user@DESKTOP-K9ID8T1:/mnt/d/Programs/ish$ "
        value = ShellANSI("\x1b]0;" + title + "\x07\x1b[32m" + prompt + "\x1b[0m")
        self.assertEqual(visible(value), prompt)
        self.assertEqual(value.title, title)
        self.assertEqual(value.icon_title, title)
        self.assertEqual(
            to_formatted_text(value),
            to_formatted_text(ANSI("\x1b[32m" + prompt + "\x1b[0m")),
        )

    def test_string_families_and_terminators(self):
        """Prevent payload leaks across control-string families and BEL/ST terminators."""
        for start, kind in ShellANSI._STRING_STARTS.items():
            for end in ("\x1b\\", "\x9c") + (("\x07",) if kind == "OSC" else ()):
                with self.subTest(kind=kind, end=end):
                    value = RecordingANSI(
                        "before\x1b" + start + "payload" + end + "after"
                    )
                    self.assertEqual(visible(value), "beforeafter")
                    self.assertEqual(value.controls, [(kind, "payload")])

    def test_c1_introducers(self):
        """Separate control sequences with eight-bit C1 introducers from display text."""
        for start, kind in ShellANSI._C1_STARTS.items():
            value = RecordingANSI(start + "payload\x9cOK")
            self.assertEqual(visible(value), "OK")
            self.assertEqual(value.controls, [(kind, "payload")])

    def test_hyperlink_label_survives(self):
        """Hide OSC hyperlink metadata while preserving the visible link label."""
        value = RecordingANSI("\x1b]8;;https://example.com\x1b\\label\x1b]8;;\x1b\\$")
        self.assertEqual(visible(value), "label$")
        self.assertEqual(len(value.controls), 2)

    def test_unsupported_csi_and_escape_do_not_leak(self):
        """Keep unsupported CSI and ESC parameters out of ordinary display text."""
        value = RecordingANSI("\x1b[?25l\x1b[2J\x1b(B\x1b[1 qOK")
        self.assertEqual(visible(value), "OK")
        self.assertEqual(
            value.controls,
            [("CSI", "?25l"), ("CSI", "2J"), ("ESC", "(B"), ("CSI", "1 q")],
        )

    def test_styles_match_upstream(self):
        """Match prompt-toolkit's default ANSI styles for supported SGR sequences."""
        for text in (
            "\x1b[1;31mred\x1b[0m plain",
            "\x1b[38;2;1;2;3mRGB",
            "\x1b[48;5;123m256",
            "a\x1b[3Cb",
            "\x9b32mgreen",
        ):
            self.assertEqual(
                to_formatted_text(ShellANSI(text)), to_formatted_text(ANSI(text))
            )

    def test_style_survives_control_string(self):
        """Preserve text styles across nonprinting control strings."""
        value = ShellANSI("\x1b[31ma\x1b]2;title\x07b\x1b[0mc")
        self.assertEqual(
            to_formatted_text(value), to_formatted_text(ANSI("\x1b[31mab\x1b[0mc"))
        )

    def test_unterminated_strings_are_not_displayed(self):
        """Do not display unterminated control strings as prompt text."""
        for tail in ("\x1b]2;title", "\x1bPdata", "\x1b[?25", "\x1b("):
            self.assertEqual(visible(ShellANSI("OK" + tail)), "OK")

    def test_cancellation_and_non_osc_bell(self):
        """Handle cancellation characters without treating BEL as a terminator outside OSC."""
        self.assertEqual(visible(ShellANSI("\x1b]2;bad\x18OK")), "OK")
        value = RecordingANSI("\x1bPdata\x07more\x1b\\OK")
        self.assertEqual(value.controls, [("DCS", "data\x07more")])
        self.assertEqual(visible(value), "OK")

    def test_control_buffer_limit(self):
        """Keep memory used for oversized control strings within its limit."""
        value = RecordingANSI("\x1b]" + "x" * (ShellANSI._MAX_CONTROL + 1) + "\x07OK")
        self.assertEqual(visible(value), "OK")
        self.assertEqual(value.controls, [])

    def test_unicode_and_width(self):
        """Preserve display text and terminal widths for Hangul and combining characters."""
        value = ShellANSI("\x1b]2;title\x07한글$ ")
        self.assertEqual(visible(value), "한글$ ")
        self.assertEqual(fragment_list_width(to_formatted_text(value)), 6)

    def test_explicit_zero_width_contract(self):
        """Preserve prompt-toolkit's zero-width contract for explicit SOH/STX regions."""
        text = "\x01\x1b]2;title\x07\x02\x1b[32mOK"
        self.assertEqual(
            to_formatted_text(ShellANSI(text)), to_formatted_text(ANSI(text))
        )

    def test_formatting_retains_subclass(self):
        """Keep the extended ANSI class after format and percent-format operations."""
        value = ShellANSI("\x1b]2;title\x07{} ").format("OK")
        self.assertIsInstance(value, ShellANSI)
        self.assertEqual(value.title, "title")
        self.assertEqual(visible(value), "OK ")
        self.assertEqual(visible(ShellANSI("\x1b]2;title\x07%s") % "OK"), "OK")


if __name__ == "__main__":
    unittest.main()
