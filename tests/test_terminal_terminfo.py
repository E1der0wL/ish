"""Compare the remaining advertised capabilities with independent native results.

These are database-to-engine samples, not a claim that every physical host key
or rendered pixel is supported. Explicit profile differences remain visible.
"""

import curses
import re
import subprocess
import unittest
from pathlib import Path

from ish.terminal.clipboard import ClipboardBroker
from ish.terminal.host import HostProfile
from ish.terminal.vt import ALT, Terminal
from test_terminal_contracts import rows

FLAGS = "OTbs am bce ccc km mc5i mir msgr npc xenl AX XF XT".split()
NUMBERS = {"colors": 256, "cols": 80, "it": 8, "lines": 24, "pairs": 65536}
LEGACY_EXCLUDED = "mc0 mc4 mc5 meml memu smm rmm".split()


class RemainingTerminfoContracts(unittest.TestCase):
    """Use real xterm-256color entries, with literal text and protocol expectations."""

    # Ubuntu 24.04's database has older XTerm reply patterns. Neither form
    # matches ish's identity; the query/reply test checks that independently.
    REVIEWED_IDENTITY_PATTERNS = {
        r"rv=\E\\[41;[1-6][0-9][0-9];0c,": r"rv=\E\\[>41;[1-6][0-9][0-9];0c,",
        r"xr=\EP>\\|XTerm\\([1-9][0-9]+\\)\E\\\\,": r"xr=\EP>\\|XTerm\\(([1-9][0-9]+)\\)\E\\\\,",
    }

    @classmethod
    def setUpClass(cls):
        """A missing advertised database is an error, not a skipped capability."""
        curses.setupterm(term="xterm-256color")

    def capability(self, name, *parameters):
        """Expand numeric or string parameters through the installed ncurses tools."""
        value = curses.tigetstr(name)
        self.assertIsNotNone(value, name)
        if any(isinstance(value, str) for value in parameters):
            return subprocess.check_output(
                ["tput", "-T", "xterm-256color", name, *map(str, parameters)]
            )
        return curses.tparm(value, *parameters) if parameters else value

    def test_advertised_inventory_has_no_unreviewed_entries(self):
        """A changed installed database must be reviewed against the saved source."""
        catalog = (
            Path(__file__).resolve().parents[1] / "docs" / "VT_TERMINFO_CATALOG.md"
        ).read_text(encoding="utf-8")
        saved = catalog.split("```text\n", 1)[1].split("```", 1)[0]
        current = subprocess.check_output(
            ["infocmp", "-1", "-x", "xterm-256color"], text=True
        )
        expected = {
            line.strip() for line in saved.splitlines() if line.startswith("\t")
        }
        actual = {
            line.strip() for line in current.splitlines() if line.startswith("\t")
        }
        self.assertEqual(len(expected), 287)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(
            {self.REVIEWED_IDENTITY_PATTERNS.get(entry, entry) for entry in actual},
            expected,
        )

    def test_numeric_metadata_describes_defaults_not_a_fixed_runtime_size(self):
        """The initial columns, rows and tabs coexist with later actual size reports."""
        for name, expected in NUMBERS.items():
            with self.subTest(capability=name):
                self.assertEqual(curses.tigetnum(name), expected)
        replies = []
        with Terminal(80, 24, replies.append) as terminal:
            terminal.feed(b"A\tB")
            self.assertEqual(rows(terminal)[0], "A       B")
            terminal.resize(97, 31)
            terminal.feed(b"\x1b[18t")
            self.assertEqual(replies, [b"\x1b[8;31;97t"])

    def test_boolean_metadata_and_cursor_movement_promises(self):
        """Flags include behavioral guarantees; printer metadata remains excluded."""
        for name in FLAGS:
            with self.subTest(capability=name):
                self.assertEqual(curses.tigetflag(name), 1)
        self.assertIsNone(curses.tigetstr("pad"))  # npc: no mandatory pad byte.
        with Terminal(5, 4, lambda _: None) as terminal:
            terminal.feed(b"ABCDE")
            self.assertEqual(rows(terminal), ["ABCDE", "", "", ""])
            terminal.feed(b"F")  # am/xenl: wrap is delayed until the next glyph.
            self.assertEqual(rows(terminal), ["ABCDE", "F", "", ""])
            terminal.feed(b"\x08Z")  # OTbs: backspace moves, without erasing.
            self.assertEqual(rows(terminal)[1], "Z")
            terminal.feed(b"\x1b[1;1H\x1b[4h\x1b[1;3HX\x1b[4l")
            self.assertEqual(rows(terminal)[0], "ABXCD")  # mir
            terminal.feed(b"\x1b[7m\x1b[1;2HY\x1b[0m")  # msgr
            self.assertEqual(rows(terminal)[0], "AYXCD")
            self.assertIn("reverse", terminal.frame().cells[0][1].style)
            self.assertEqual(terminal.encode_key("x", ALT), b"\x1bx")  # km
            self.assertEqual(terminal.encode_key("é"), "é".encode())

    def test_background_erase_and_default_reset_follow_boolean_promises(self):
        """bce and AX retain explicit erase color and restore inherited defaults."""
        with Terminal(6, 3, lambda _: None) as terminal:
            terminal.feed(b"abcdef\x1b[1;3H\x1b[48;2;11;22;33m")
            terminal.feed(self.capability("ed"))
            self.assertEqual(rows(terminal), ["ab", "", ""])
            self.assertEqual(terminal.frame().cells[0][2].bg, (11, 22, 33))
            terminal.feed(self.capability("op") + b"Z")
            self.assertIsNone(terminal.frame().cells[0][2].bg)

    def test_line_edits_repeat_and_history_erase_use_database_sequences(self):
        """Parameterized and single-line operations preserve literal neighbours."""
        for name, parameters, expected in (
            ("il", (2,), ["A", "", "", "B", "C"]),
            ("il1", (), ["A", "", "B", "C", "D"]),
            ("dl", (2,), ["A", "D", "E", "", ""]),
            ("dl1", (), ["A", "C", "D", "E", ""]),
        ):
            with (
                self.subTest(capability=name),
                Terminal(8, 5, lambda _: None) as terminal,
            ):
                terminal.feed(b"A\r\nB\r\nC\r\nD\r\nE\x1b[2;1H")
                terminal.feed(self.capability(name, *parameters))
                self.assertEqual(rows(terminal), expected)
        with Terminal(12, 3, lambda _: None) as terminal:
            terminal.feed(self.capability("rep", ord("Q"), 5))
            self.assertEqual(rows(terminal)[0], "QQQQQ")
            terminal.feed(b"\r\nline" * 12)
            self.assertGreater(terminal.frame().scrollback_rows, 0)
            visible = rows(terminal)
            terminal.feed(self.capability("E3"))
            self.assertEqual(terminal.frame().scrollback_rows, 0)
            self.assertEqual(rows(terminal), visible)

    def test_region_scroll_entries_preserve_rows_outside_the_region(self):
        """csr/ind/indn/ri/rin/nel use the advertised row and cursor semantics."""
        for name, args, position, expected in (
            ("ind", (), b"\x1b[4;1H", ["A", "C", "D", "", "E"]),
            ("indn", (2,), b"\x1b[3;1H", ["A", "D", "", "", "E"]),
            ("ri", (), b"\x1b[2;1H", ["A", "", "B", "C", "E"]),
            ("rin", (2,), b"\x1b[3;1H", ["A", "", "", "B", "E"]),
            ("nel", (), b"\x1b[4;7H", ["A", "C", "D", "", "E"]),
        ):
            with (
                self.subTest(capability=name),
                Terminal(8, 5, lambda _: None) as terminal,
            ):
                terminal.feed(b"A\r\nB\r\nC\r\nD\r\nE")
                terminal.feed(self.capability("csr", 1, 3) + position)
                terminal.feed(self.capability(name, *args))
                self.assertEqual(rows(terminal), expected)
                if name == "nel":
                    self.assertEqual(terminal.frame().cursor_x, 0)

    def test_horizontal_margin_parameters_and_cancellation(self):
        """Both separate-margin capabilities and the combined form describe a rectangle."""
        for wire, body, expected in (
            (
                self.capability("smglr", 2, 7),
                b"123456abcdefZ",
                ["LEFT     R", "  abcdef", "  Z", "BOTTOM"],
            ),
            (
                self.capability("smglp", 2),
                b"12345678abcdefghZ",
                ["LEFT     R", "  abcdefgh", "  Z", "BOTTOM"],
            ),
            (
                self.capability("smgrp", 7),
                b"12345678abcdefghZ",
                ["LEFT     R", "abcdefgh", "Z", "BOTTOM"],
            ),
        ):
            with self.subTest(wire=wire), Terminal(10, 4, lambda _: None) as terminal:
                terminal.feed(b"LEFT\x1b[1;10HR\x1b[4;1HBOTTOM")
                terminal.feed(wire + b"\x1b[2;3r\x1b[?6h" + body)
                self.assertEqual(rows(terminal), expected)
                terminal.feed(self.capability("mgc") + b"\x1b[?6l\x1b[1;1HX")
                self.assertEqual(rows(terminal)[0], "XEFT     R")

    def test_all_256_color_parameters_palette_change_and_reset(self):
        """colors/pairs/ccc and setaf/setab/initc/oc/op share one native palette."""
        palette = [(index, 255 - index, index // 2) for index in range(256)]
        with Terminal(4, 2, lambda _: None) as terminal:
            terminal.set_appearance(palette=palette)
            for index, rgb in enumerate(palette):
                terminal.feed(b"\x1b[H" + self.capability("setaf", index))
                terminal.feed(self.capability("setab", 255 - index) + b"X")
                cell = terminal.frame().cells[0][0]
                self.assertEqual(cell.fg, rgb, index)
                self.assertEqual(cell.bg, palette[255 - index], index)
            terminal.feed(self.capability("initc", 42, 1000, 500, 0))
            terminal.feed(b"\x1b[H" + self.capability("setaf", 42) + b"Y")
            self.assertEqual(terminal.frame().cells[0][0].fg, (255, 127, 0))
            terminal.feed(self.capability("oc"))
            self.assertEqual(terminal.frame().cells[0][0].fg, palette[42])
            terminal.feed(self.capability("op") + b"Z")
            self.assertIsNone(terminal.frame().cells[0][1].fg)
            self.assertIsNone(terminal.frame().cells[0][1].bg)

    def test_basic_renditions_and_selective_resets_preserve_other_attributes(self):
        """Every advertised SGR style is read from actual native cells."""
        for enable, disable, style in (
            ("sitm", "ritm", "italic"),
            ("smul", "rmul", "underline"),
            ("smso", "rmso", "reverse"),
            ("smxx", "rmxx", "strike"),
        ):
            with (
                self.subTest(capability=enable),
                Terminal(8, 3, lambda _: None) as terminal,
            ):
                terminal.feed(self.capability("bold") + self.capability(enable) + b"A")
                terminal.feed(
                    self.capability(disable) + b"B" + self.capability("sgr0") + b"C"
                )
                a, b, c = terminal.frame().cells[0][:3]
                self.assertIn(style, a.style)
                self.assertNotIn(style, b.style)
                self.assertIn("bold", b.style)
                self.assertEqual(c.flags, 0)
        for name, style in (
            ("blink", "blink"),
            ("dim", "dim"),
            ("invis", "hidden"),
            ("rev", "reverse"),
        ):
            with (
                self.subTest(capability=name),
                Terminal(8, 3, lambda _: None) as terminal,
            ):
                terminal.feed(self.capability(name) + b"A")
                self.assertIn(style, terminal.frame().cells[0][0].style)
        with Terminal(8, 3, lambda _: None) as terminal:
            terminal.feed(self.capability("sgr", 1, 1, 0, 1, 1, 1, 1, 0, 0) + b"A")
            self.assertEqual(
                set(terminal.frame().cells[0][0].style.split()),
                {
                    "bold",
                    "dim",
                    "blink",
                    "reverse",
                    "hidden",
                    "underline",
                    "fg:default",
                    "bg:default",
                },
            )
            self.assertEqual(terminal.frame().cells[0][0].underline, 1)

    def test_cursor_visibility_style_and_color_have_native_reset_semantics(self):
        """Host presentation remains gated separately from the child's cursor model."""
        with Terminal(8, 3, lambda _: None) as terminal:
            terminal.feed(self.capability("civis"))
            self.assertFalse(terminal.frame().cursor_visible)
            terminal.feed(self.capability("cnorm"))
            self.assertTrue(terminal.frame().cursor_visible)
            self.assertFalse(terminal.frame().cursor_blink)
            terminal.feed(self.capability("cvvis"))
            self.assertTrue(terminal.frame().cursor_visible)
            self.assertTrue(terminal.frame().cursor_blink)
            for code, shape, blink in (
                (1, "block", True),
                (2, "block", False),
                (3, "underline", True),
                (4, "underline", False),
                (5, "bar", True),
                (6, "bar", False),
            ):
                terminal.feed(self.capability("Ss", code))
                self.assertEqual(
                    (terminal.frame().cursor_shape, terminal.frame().cursor_blink),
                    (shape, blink),
                )
            terminal.feed(self.capability("Se"))
            self.assertEqual(
                (terminal.frame().cursor_shape, terminal.frame().cursor_blink),
                ("block", False),
            )
            terminal.feed(self.capability("Cs", "#123456"))
            self.assertEqual(terminal.frame().cursor_color, (18, 52, 86))
            terminal.feed(self.capability("Cr"))
            self.assertIsNone(terminal.frame().cursor_color)

    def test_acs_map_and_ascii_reset_cover_the_advertised_glyphs(self):
        """The acsc source map is metadata; emitted glyphs follow the DEC graphics set."""
        mapping = self.capability("acsc")
        self.assertEqual(mapping[::2], mapping[1::2])
        with Terminal(40, 3, lambda _: None) as terminal:
            terminal.feed(self.capability("smacs") + mapping[1::2])
            terminal.feed(self.capability("rmacs") + b"q")
            self.assertEqual(rows(terminal)[0], "◆▒°±␋┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·q")

    def test_initialization_reset_and_alternate_screen_use_real_compound_entries(self):
        """is2/rs2 reset modes; rs1 clears the screen and resets the palette."""
        for name in ("is2", "rs2"):
            replies = []
            with (
                self.subTest(capability=name),
                Terminal(12, 4, replies.append) as terminal,
            ):
                terminal.feed(b"KEEP\x1b[?1h\x1b[4h\x1b=\x1b[?1035l")
                terminal.feed(self.capability(name) + b"\x1b[?1$p\x1b[4$p\x1b[?66$p")
                self.assertEqual(rows(terminal)[0], "KEEP")
                self.assertEqual(
                    b"".join(replies), b"\x1b[?1;2$y\x1b[4;2$y\x1b[?66;2$y"
                )
        with Terminal(12, 4, lambda _: None) as terminal:
            palette = [None] * 256
            palette[1] = (9, 8, 7)
            terminal.set_appearance(palette=palette)
            terminal.feed(self.capability("initc", 1, 1000, 0, 0))
            terminal.feed(b"PRIMARY" + self.capability("smcup") + b"ALTERNATE")
            self.assertTrue(terminal.frame().alternate)
            terminal.feed(self.capability("rmcup"))
            self.assertFalse(terminal.frame().alternate)
            self.assertEqual(rows(terminal)[0], "PRIMARY")
            terminal.feed(self.capability("rs1"))
            self.assertEqual(rows(terminal), ["", "", "", ""])
            terminal.feed(self.capability("setaf", 1) + b"X")
            self.assertEqual(terminal.frame().cells[0][0].fg, (9, 8, 7))

    def test_mouse_focus_and_paste_pairs_have_matching_input_results(self):
        """Output enables the advertised input format, and disabling stops reports."""
        with Terminal(20, 8, lambda _: None) as terminal:
            terminal.feed(self.capability("XM", 1))
            self.assertEqual(self.capability("kmous"), b"\x1b[<")
            for action, pressed, expected in (
                ("press", 1, b"\x1b[<0;5;3M"),
                ("release", 0, b"\x1b[<0;5;3m"),
            ):
                self.assertEqual(terminal.encode_mouse(action, "left", 4, 2), expected)
                # The installed experimental xm template has an extra delimiter.
                # Keep this known profile difference visible; do not break SGR
                # mouse input to imitate a template no known interpreter uses.
                self.assertEqual(
                    self.capability("xm", 4, 2, 0, pressed),
                    expected[:-1] + b";" + expected[-1:],
                )
            terminal.feed(self.capability("XM", 0))
            self.assertFalse(terminal.mouse_reporting)
            terminal.feed(self.capability("fe"))
            self.assertEqual(terminal.encode_focus(True), self.capability("kxIN"))
            self.assertEqual(terminal.encode_focus(False), self.capability("kxOUT"))
            terminal.feed(self.capability("fd"))
            self.assertEqual(terminal.encode_focus(True), b"")
            terminal.feed(self.capability("BE"))
            self.assertEqual(
                terminal.encode_paste("한글"),
                self.capability("PS") + "한글".encode() + self.capability("PE"),
            )
            terminal.feed(self.capability("BD"))
            self.assertEqual(terminal.encode_paste("한글"), "한글".encode())

    def test_clipboard_capability_uses_the_existing_policy_broker(self):
        """Ms carries encoded data once; emitting a write is not a completion claim."""
        emitted, replies = [], []
        broker = ClipboardBroker(
            reply=replies.append, emit=emitted.append, profile=lambda: HostProfile()
        )
        with Terminal(20, 4, replies.append) as terminal:
            terminal.clipboard = broker
            terminal.feed(self.capability("Ms", "c", "aGVsbG8="))
            self.assertEqual(emitted, ["\x1b]52;c;aGVsbG8=\x1b\\"])
            self.assertEqual(replies, [])
            self.assertEqual(rows(terminal), ["", "", "", ""])

    def test_cursor_device_queries_and_explicit_identity_profile_differences(self):
        """Replies identify ish; XTerm-specific rv/xr patterns are documented differences."""
        replies = []
        with Terminal(20, 4, replies.append) as terminal:
            terminal.feed(b"\x1b[2;3H" + self.capability("u7"))
            self.assertEqual(replies.pop(), b"\x1b[2;3R")
            # u6 describes a received report; it is not an output tparm program.
            self.assertEqual(self.capability("u6"), b"\x1b[%i%d;%dR")
            terminal.feed(self.capability("u9"))
            self.assertEqual(replies.pop(), b"\x1b[?62;22c")
            self.assertEqual(self.capability("u8"), b"\x1b[?%[;0123456789]c")
            terminal.feed(self.capability("RV"))
            secondary = replies.pop()
            self.assertEqual(secondary, b"\x1b[>1;0;0c")
            self.assertIsNone(re.fullmatch(self.capability("rv"), secondary))
            terminal.feed(self.capability("XR"))
            version = replies.pop()
            self.assertEqual(version, b"\x1bP>|ish 2.0.0\x1b\\")
            self.assertIsNone(re.fullmatch(self.capability("xr"), version))

    def test_bell_and_visual_bell_have_distinct_native_effects(self):
        """BEL is an effect; flash is timed reverse video, not printable padding."""
        with Terminal(8, 3, lambda _: None) as terminal:
            terminal.set_appearance(foreground=(1, 2, 3), background=(4, 5, 6))
            terminal.feed(b"X" + self.capability("bel"))
            self.assertEqual(
                [event.kind for event in terminal.drain_events()], ["bell"]
            )
            before, padding, after = re.split(
                rb"(\$<[0-9]+/>)", self.capability("flash")
            )
            self.assertEqual(padding, b"$<100/>")
            terminal.feed(before)
            cell = terminal.frame().cells[0][0]
            self.assertIn("reverse", cell.style)
            self.assertIsNone(cell.fg)
            self.assertIsNone(cell.bg)
            terminal.feed(after)
            cell = terminal.frame().cells[0][0]
            self.assertNotIn("reverse", cell.style)
            self.assertIsNone(cell.fg)
            self.assertIsNone(cell.bg)

    def test_excluded_legacy_entries_recover_without_claiming_peripheral_support(self):
        """Shared xterm entries do not make printer, memory-lock or 8-bit meta supported."""
        for name in LEGACY_EXCLUDED:
            with (
                self.subTest(capability=name),
                Terminal(20, 4, lambda _: None) as terminal,
            ):
                terminal.feed(self.capability(name) + b"NEXT")
                self.assertEqual(rows(terminal)[0], "NEXT")
                self.assertFalse(terminal.processing_error)


if __name__ == "__main__":
    unittest.main()
