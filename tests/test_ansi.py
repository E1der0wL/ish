import unittest

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_width

from ish.ui.ansi import ShellANSI


def visible(value):
    return "".join(text for style, text in to_formatted_text(value)
                   if "[ZeroWidthEscape]" not in style)


class RecordingANSI(ShellANSI):
    def __init__(self, value):
        self.controls = []
        super().__init__(value)

    def handle_control(self, kind, payload):
        self.controls.append((kind, payload))
        super().handle_control(kind, payload)


class ShellANSITests(unittest.TestCase):
    def test_reported_prompt(self):
        title = "user@DESKTOP-K9ID8T1: /mnt/d/Programs/ish"
        prompt = "user@DESKTOP-K9ID8T1:/mnt/d/Programs/ish$ "
        value = ShellANSI("\x1b]0;" + title + "\x07\x1b[32m" + prompt + "\x1b[0m")
        self.assertEqual(visible(value), prompt)
        self.assertEqual(value.title, title)
        self.assertEqual(value.icon_title, title)
        self.assertEqual(to_formatted_text(value), to_formatted_text(ANSI("\x1b[32m" + prompt + "\x1b[0m")))

    def test_string_families_and_terminators(self):
        for start, kind in ShellANSI._STRING_STARTS.items():
            for end in ("\x1b\\", "\x9c") + (("\x07",) if kind == "OSC" else ()):
                with self.subTest(kind=kind, end=end):
                    value = RecordingANSI("before\x1b" + start + "payload" + end + "after")
                    self.assertEqual(visible(value), "beforeafter")
                    self.assertEqual(value.controls, [(kind, "payload")])

    def test_c1_introducers(self):
        for start, kind in ShellANSI._C1_STARTS.items():
            value = RecordingANSI(start + "payload\x9cOK")
            self.assertEqual(visible(value), "OK")
            self.assertEqual(value.controls, [(kind, "payload")])

    def test_hyperlink_label_survives(self):
        value = RecordingANSI("\x1b]8;;https://example.com\x1b\\label\x1b]8;;\x1b\\$")
        self.assertEqual(visible(value), "label$")
        self.assertEqual(len(value.controls), 2)

    def test_unsupported_csi_and_escape_do_not_leak(self):
        value = RecordingANSI("\x1b[?25l\x1b[2J\x1b(B\x1b[1 qOK")
        self.assertEqual(visible(value), "OK")
        self.assertEqual(value.controls, [("CSI", "?25l"), ("CSI", "2J"), ("ESC", "(B"), ("CSI", "1 q")])

    def test_styles_match_upstream(self):
        for text in ("\x1b[1;31mred\x1b[0m plain", "\x1b[38;2;1;2;3mRGB",
                     "\x1b[48;5;123m256", "a\x1b[3Cb", "\x9b32mgreen"):
            self.assertEqual(to_formatted_text(ShellANSI(text)), to_formatted_text(ANSI(text)))

    def test_style_survives_control_string(self):
        value = ShellANSI("\x1b[31ma\x1b]2;title\x07b\x1b[0mc")
        self.assertEqual(to_formatted_text(value), to_formatted_text(ANSI("\x1b[31mab\x1b[0mc")))

    def test_unterminated_strings_are_not_displayed(self):
        for tail in ("\x1b]2;title", "\x1bPdata", "\x1b[?25", "\x1b("):
            self.assertEqual(visible(ShellANSI("OK" + tail)), "OK")

    def test_cancellation_and_non_osc_bell(self):
        self.assertEqual(visible(ShellANSI("\x1b]2;bad\x18OK")), "OK")
        value = RecordingANSI("\x1bPdata\x07more\x1b\\OK")
        self.assertEqual(value.controls, [("DCS", "data\x07more")])
        self.assertEqual(visible(value), "OK")

    def test_control_buffer_limit(self):
        value = RecordingANSI("\x1b]" + "x" * (ShellANSI._MAX_CONTROL + 1) + "\x07OK")
        self.assertEqual(visible(value), "OK")
        self.assertEqual(value.controls, [])

    def test_unicode_and_width(self):
        value = ShellANSI("\x1b]2;title\x07한글$ ")
        self.assertEqual(visible(value), "한글$ ")
        self.assertEqual(fragment_list_width(to_formatted_text(value)), 6)

    def test_explicit_zero_width_contract(self):
        text = "\x01\x1b]2;title\x07\x02\x1b[32mOK"
        self.assertEqual(to_formatted_text(ShellANSI(text)), to_formatted_text(ANSI(text)))

    def test_formatting_retains_subclass(self):
        value = ShellANSI("\x1b]2;title\x07{} ").format("OK")
        self.assertIsInstance(value, ShellANSI)
        self.assertEqual(value.title, "title")
        self.assertEqual(visible(value), "OK ")
        self.assertEqual(visible(ShellANSI("\x1b]2;title\x07%s") % "OK"), "OK")


if __name__ == "__main__":
    unittest.main()
