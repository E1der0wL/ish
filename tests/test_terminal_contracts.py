"""Exercise baseline VT contracts using literal screen, reply and input results."""

import ctypes
import curses
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ish.terminal import diagnostics
from ish.terminal.vt import ALT, CTRL, SHIFT, Terminal, TerminalError
from terminal_scenarios import SHELLS, HooklessTerminal
from test_terminal_cli import STARTUP_DEADLINE

QUERY_READER = r"""import os
import termios
import tty
from pathlib import Path

saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    os.write(1, b'\x1b[3;4H\x1b[6n\x1b[?2004h\x1b[?2004$p\x1b[?1h')
    expected = b'\x1b[3;4R\x1b[?2004;1$y'
    data = bytearray()
    while len(data) < len(expected):
        data.extend(os.read(0, len(expected) - len(data)))
    Path('query-result').write_bytes(data)
    keys = bytearray()
    while not keys.endswith(b'|done|'):
        keys.extend(os.read(0, 4096))
    Path('query-keys').write_bytes(keys)
finally:
    os.write(1, b'\x1b[?2004l\x1b[?1l\r\n')
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


def rows(terminal):
    """Read native rendered text without using the implementation's style codec."""
    return [
        "".join(value for _, value in row).rstrip() for row in terminal.frame().lines
    ]


class BaselineContracts(unittest.TestCase):
    """Keep independent examples for VT01/03–09/12/13 and HX01/02/04."""

    def test_insert_delete_repeat_and_erase_are_positioned(self):
        """ICH/DCH/ECH/REP affect exactly the requested cells."""
        cases = (
            (b"abcd\x1b[1;2H\x1b[2@X", "aX bcd"),
            (b"abcdef\x1b[1;3H\x1b[2P", "abef"),
            (b"abcdef\x1b[1;3H\x1b[2X", "ab  ef"),
            (b"X\x1b[3b", "XXXX"),
            (b"abcdef\x1b[1;3H\x1b[1K", "   def"),
        )
        for wire, expected in cases:
            with self.subTest(wire=wire), Terminal(10, 4, lambda _: None) as terminal:
                terminal.feed(wire)
                self.assertEqual(rows(terminal)[0], expected)

    def test_line_edit_and_region_scroll_preserve_outside_rows(self):
        """IL/DL and scroll margins leave the first and last rows unchanged."""
        with Terminal(8, 5, lambda _: None) as terminal:
            terminal.feed(b"A\r\nB\r\nC\r\nD\r\nE")
            terminal.feed(b"\x1b[2;4r\x1b[2;1H\x1b[L")
            self.assertEqual(rows(terminal), ["A", "", "B", "C", "E"])
            terminal.feed(b"\x1b[M")
            self.assertEqual(rows(terminal), ["A", "B", "C", "", "E"])
            terminal.feed(b"\x1b[4;1HZ\n")
            self.assertEqual(rows(terminal), ["A", "C", "Z", "", "E"])

    def test_tabs_origin_wrap_and_saved_cursor(self):
        """Tab stops, origin mode, wrap and DECSC/DECRC have fixed expectations."""
        with Terminal(12, 5, lambda _: None) as terminal:
            terminal.feed(b"\x1b[3g\x1b[1;5H\x1bH\rA\tB")
            self.assertEqual(rows(terminal)[0], "A   B")
            terminal.feed(b"\x1b[2;4r\x1b[?6h\x1b[1;2HQ")
            self.assertEqual(rows(terminal)[1], " Q")
            terminal.feed(b"\x1b7\x1b[3;1HZ\x1b8R")
            self.assertEqual(rows(terminal)[1], " QR")
            terminal.feed(b"\x1b[?6l\x1b[r\x1b[1;12HXy")
            self.assertEqual(rows(terminal)[0], "A   B      X")
            self.assertEqual(rows(terminal)[1], "yQR")

    def test_dec_line_drawing_and_selective_reset(self):
        """ACS is interpreted by native VT and SGR reset does not erase text."""
        with Terminal(16, 3, lambda _: None) as terminal:
            terminal.feed(b"\x1b(0lqk\x1b(B\x1b[1;3;4mX\x1b[0mY")
            frame = terminal.frame()
            self.assertEqual(rows(terminal)[0], "\u250c\u2500\u2510XY")
            x, y = frame.cells[0][3:5]
            self.assertIn("bold", x.style)
            self.assertNotIn("bold", y.style)
            terminal.feed(b"\x1bc")
            self.assertEqual(rows(terminal), ["", "", ""])
            self.assertEqual(
                (terminal.frame().cursor_x, terminal.frame().cursor_y), (0, 0)
            )

    def test_fragmentation_cancellation_and_query_identity(self):
        """All split positions preserve text, replies and the virtual identity."""
        wire = "A한\x1b[31mB\x1b[0m\x1b[6n".encode()
        for split in range(len(wire) + 1):
            replies = []
            with self.subTest(split=split), Terminal(20, 4, replies.append) as terminal:
                terminal.feed(wire[:split])
                terminal.feed(wire[split:])
                self.assertEqual(rows(terminal)[0], "A한B")
                self.assertEqual(b"".join(replies), b"\x1b[1;5R")
                terminal.feed(b"\x1b]9999;discard\x18Z")
                self.assertEqual(rows(terminal)[0], "A한BZ")
        replies = []
        with Terminal(20, 4, replies.append) as terminal:
            terminal.feed(b"\x1b[>q\x1bP+q544e\x1b\\")
            self.assertEqual(
                b"".join(replies),
                b"\x1bP>|ish 2.0.0\x1b\\"
                b"\x1bP1+r544E=787465726D2D323536636F6C6F72\x1b\\",
            )
            self.assertFalse(terminal.processing_error)

    def test_processing_failure_cannot_be_reported_as_a_successful_feed(self):
        """An injected semantic failure aborts before delivering a successful result."""
        with Terminal(20, 4, lambda _: None) as terminal:
            with patch.object(terminal._library, "ish_vt_feed", return_value=-102):
                with self.assertRaises(TerminalError):
                    terminal.feed(b"X")

    def test_native_diagnostics_do_not_dereference_or_capture_payloads(self):
        """Even invalid payload pointers are ignored; only severity is counted."""
        before = diagnostics.snapshot()
        diagnostics.capture(None, 1, ctypes.c_void_p(1), 999, ctypes.c_void_p(1), 999)
        after = diagnostics.snapshot()
        self.assertEqual(after["warning"], before["warning"] + 1)
        self.assertEqual(set(after), {"error", "warning", "info", "debug"})

    def test_forward_backward_and_cleared_tabs_use_physical_columns(self):
        """CHT/CBT and TBC use stops rather than counting Unicode characters."""
        with Terminal(24, 4, lambda _: None) as terminal:
            terminal.feed(b"\x1b[3g\x1b[1;5H\x1bH\x1b[1;13H\x1bH")
            terminal.feed("\r한\x1b[2IA\x1b[2ZB".encode())
            self.assertEqual(rows(terminal)[0], "한  B       A")
            terminal.feed(b"\x1b[1;5H\x1b[g\r\tC")
            self.assertEqual(rows(terminal)[0], "한  B       C")
            terminal.feed(b"\x1b[3g\r\tD")
            self.assertEqual(
                [
                    (cell.x, cell.text)
                    for cell in terminal.frame().cells[0]
                    if cell.text == "D"
                ],
                [(23, "D")],
            )

    def test_horizontal_margins_limit_wrapping_and_scrolling(self):
        """DECLRMM and DECOM confine a wrapped line to the selected rectangle."""
        with Terminal(10, 4, lambda _: None) as terminal:
            terminal.feed(b"LEFT\x1b[1;10HR\x1b[4;1HBOTTOM")
            terminal.feed(b"\x1b[?69h\x1b[3;8s\x1b[2;3r\x1b[?6h")
            terminal.feed(b"123456abcdefZ")
            self.assertEqual(
                rows(terminal), ["LEFT     R", "  abcdef", "  Z", "BOTTOM"]
            )
            self.assertEqual(
                (terminal.frame().cursor_x, terminal.frame().cursor_y), (3, 2)
            )

    def test_wrap_disable_and_carriage_return_cancel_pending_wrap(self):
        """DECAWM disabled overwrites the last cell; CR cancels a pending wrap."""
        with Terminal(5, 3, lambda _: None) as terminal:
            terminal.feed(b"\x1b[?7l1234567")
            self.assertEqual(rows(terminal), ["12347", "", ""])
            terminal.feed(b"\x1b[?7h\rABCDE\rZ")
            self.assertEqual(rows(terminal), ["ZBCDE", "", ""])
            terminal.feed(b"\x1b[1;5HXQ")
            self.assertEqual(rows(terminal), ["ZBCDX", "Q", ""])

    def test_alternate_47_1047_and_saved_cursor_1048_have_distinct_effects(self):
        """Screen switching preserves primary text; cursor save does not switch."""
        for mode in (47, 1047):
            with self.subTest(mode=mode), Terminal(10, 4, lambda _: None) as terminal:
                terminal.feed(b"primary")
                terminal.feed(f"\x1b[?{mode}h\x1b[Halternate".encode())
                self.assertTrue(terminal.frame().alternate)
                self.assertEqual(rows(terminal)[0], "alternate")
                terminal.feed(f"\x1b[?{mode}l".encode())
                self.assertFalse(terminal.frame().alternate)
                self.assertEqual(rows(terminal)[0], "primary")
        with Terminal(10, 4, lambda _: None) as terminal:
            terminal.feed(b"\x1b[2;4H\x1b[?1048h\x1b[4;9HX\x1b[?1048lY")
            self.assertFalse(terminal.frame().alternate)
            self.assertEqual(rows(terminal), ["", "   Y", "", "        X"])

    def test_1049_resize_and_repeated_exit_keep_primary_text(self):
        """Resizing a full-screen application preserves the main screen on exit."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed("한글 primary\x1b[?1049h\x1b[Happlication".encode())
            terminal.resize(16, 6)
            self.assertEqual(rows(terminal)[0], "application")
            terminal.feed(b"\x1b[?1049l\x1b[?1049l")
            self.assertEqual(rows(terminal)[0], "한글 primary")
            self.assertFalse(terminal.frame().alternate)
            self.assertEqual((terminal.frame().cols, terminal.frame().rows), (16, 6))

    def test_soft_reset_resets_input_modes_without_erasing_text(self):
        """DECSTR clears origin/insert/cursor-key modes and keeps screen contents."""
        replies = []
        with Terminal(12, 5, replies.append) as terminal:
            terminal.feed(b"KEEP\x1b[2;4r\x1b[?6h\x1b[?1h\x1b[4h\x1b[!p")
            terminal.feed(b"\x1b[?6$p\x1b[?1$p\x1b[4$p\x1b[3;2H\x1b[6n")
            self.assertEqual(rows(terminal)[0], "KEEP")
            self.assertEqual(
                b"".join(replies),
                b"\x1b[?6;2$y\x1b[?1;2$y\x1b[4;2$y\x1b[3;2R",
            )
            self.assertEqual(terminal.encode_key("up"), b"\x1b[A")

    def test_queries_report_only_virtual_modes_cursor_and_sizes(self):
        """DSR and DECRQM preserve literal results, including an unknown mode."""
        replies = []
        with Terminal(12, 5, replies.append) as terminal:
            terminal.feed(b"\x1b[3;4H\x1b[5n\x1b[6n\x1b[?2004$p")
            terminal.feed(b"\x1b[?2004h\x1b[?2004$p\x1b[?9999$p\x1b[18t")
            self.assertEqual(
                b"".join(replies),
                b"\x1b[0n\x1b[3;4R\x1b[?2004;2$y"
                b"\x1b[?2004;1$y\x1b[?9999;0$y\x1b[8;5;12t",
            )
            replies.clear()
            terminal.resize(30, 9, 8, 16)
            terminal.feed(b"\x1b[14t\x1b[16t\x1b[18t")
            self.assertEqual(
                b"".join(replies),
                b"\x1b[4;144;240t\x1b[6;16;8t\x1b[8;9;30t",
            )

    def test_region_scroll_reverse_index_and_next_line_keep_outer_rows(self):
        """SU/SD/RI/NEL cannot move rows outside DECSTBM margins."""
        with Terminal(8, 5, lambda _: None) as terminal:
            terminal.feed(b"A\r\nB\r\nC\r\nD\r\nE\x1b[2;4r\x1b[S")
            self.assertEqual(rows(terminal), ["A", "C", "D", "", "E"])
            terminal.feed(b"\x1b[T")
            self.assertEqual(rows(terminal), ["A", "", "C", "D", "E"])
            terminal.feed(b"\x1b[2;1H\x1bMX")
            self.assertEqual(rows(terminal), ["A", "X", "", "C", "E"])
            terminal.feed(b"\x1b[4;7H\x1bEY")
            self.assertEqual(rows(terminal), ["A", "", "C", "Y", "E"])

    def test_history_erase_does_not_erase_the_visible_screen(self):
        """ED 3 clears scrollback while ED 2 clears only the visible cells."""
        with Terminal(12, 3, lambda _: None) as terminal:
            terminal.feed(b"old\r\n" * 10 + b"visible")
            self.assertGreater(terminal.frame().scrollback_rows, 0)
            visible = rows(terminal)
            terminal.feed(b"\x1b[3J")
            self.assertEqual(terminal.frame().scrollback_rows, 0)
            self.assertEqual(rows(terminal), visible)
            terminal.feed(b"\x1b[2J")
            self.assertEqual(rows(terminal), ["", "", ""])


class TerminfoContracts(unittest.TestCase):
    """Exercise the advertised database's basic capabilities against fixed results."""

    @classmethod
    def setUpClass(cls):
        """Load the real advertised terminfo without selecting a fallback database."""
        curses.setupterm(term="xterm-256color")

    def capability(self, name, *parameters):
        """Fetch actual advertised bytes; a missing required capability is failure."""
        value = curses.tigetstr(name)
        self.assertIsNotNone(value, name)
        return curses.tparm(value, *parameters) if parameters else value

    def test_cursor_capabilities_reach_the_expected_cell(self):
        """Both single and parameterized motions agree with terminfo's contract."""
        cases = (
            ("cub", (2,), (3, 2)),
            ("cub1", (), (4, 2)),
            ("cuf", (2,), (7, 2)),
            ("cuf1", (), (6, 2)),
            ("cuu", (2,), (5, 0)),
            ("cuu1", (), (5, 1)),
            ("cud", (2,), (5, 4)),
            ("cud1", (), (5, 3)),
            ("hpa", (1,), (1, 2)),
            ("vpa", (1,), (5, 1)),
            ("cup", (1, 2), (2, 1)),
            ("home", (), (0, 0)),
            ("cr", (), (0, 2)),
        )
        for name, arguments, expected in cases:
            with self.subTest(name=name), Terminal(12, 6, lambda _: None) as terminal:
                terminal.feed(b"\x1b[3;6H" + self.capability(name, *arguments))
                frame = terminal.frame()
                self.assertEqual((frame.cursor_x, frame.cursor_y), expected)

    def test_edit_capabilities_produce_expected_text(self):
        """Advertised erase/insert/delete/insert-mode actions preserve neighbours."""
        cases = (
            (self.capability("dch", 2), "abef"),
            (self.capability("dch1"), "abdef"),
            (self.capability("ech", 2), "ab  ef"),
            (self.capability("el"), "ab"),
            (self.capability("el1"), "   def"),
            (self.capability("ich", 2), "ab  cdef"),
            (
                self.capability("smir") + b"X" + self.capability("rmir") + b"Y",
                "abXYdef",
            ),
        )
        for wire, expected in cases:
            with self.subTest(wire=wire), Terminal(12, 4, lambda _: None) as terminal:
                terminal.feed(b"abcdef\x1b[1;3H" + wire)
                self.assertEqual(rows(terminal)[0], expected)

    def test_all_advertised_function_keys_match_native_modifier_encoding(self):
        """kf13–63 are modified F1–F12, not missing physical F26–F63 keys."""
        groups = (
            (0, 0),
            (12, SHIFT),
            (24, CTRL),
            (36, SHIFT | CTRL),
            (48, ALT),
            (60, SHIFT | ALT),
        )
        with Terminal(12, 4, lambda _: None) as terminal:
            for base, modifiers in groups:
                for key in range(1, min(12, 63 - base) + 1):
                    name = f"kf{base + key}"
                    with self.subTest(capability=name):
                        self.assertEqual(
                            terminal.encode_key(f"f{key}", modifiers),
                            self.capability(name),
                        )

    def test_advertised_navigation_matches_application_cursor_mode(self):
        """smkx cursor-key mode produces the codes consumed by terminfo programs."""
        keys = (
            ("kcuu1", "up"),
            ("kcud1", "down"),
            ("kcub1", "left"),
            ("kcuf1", "right"),
            ("khome", "home"),
            ("kend", "end"),
            ("kich1", "insert"),
            ("kdch1", "delete"),
            ("kpp", "pageup"),
            ("knp", "pagedown"),
        )
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(self.capability("smkx"))
            for name, key in keys:
                with self.subTest(name=name):
                    self.assertEqual(terminal.encode_key(key), self.capability(name))
            terminal.feed(self.capability("rmkx"))
            self.assertEqual(terminal.encode_key("up"), b"\x1b[A")

    def test_legacy_f3_compatibility_does_not_change_negotiated_keyboard(self):
        """Native flags and per-screen push/pop decide which key contract applies."""
        with Terminal(12, 4, lambda _: None) as terminal:
            self.assertEqual(terminal.keyboard_flags, 0)
            self.assertEqual(terminal.encode_key("f3", SHIFT), b"\x1b[1;2R")
            self.assertEqual(terminal.encode_key("f3", SHIFT, action="release"), b"")
            terminal.feed(b"\x1b[>1u")
            self.assertEqual(terminal.keyboard_flags, 1)
            self.assertEqual(terminal.encode_key("f3", SHIFT), b"\x1b[13;2~")
            terminal.feed(b"\x1b[>3u")
            self.assertEqual(terminal.keyboard_flags, 3)
            self.assertEqual(
                terminal.encode_key("f3", SHIFT, action="release"), b"\x1b[13;2:3~"
            )
            terminal.feed(b"\x1b[?1049h")
            self.assertEqual(terminal.keyboard_flags, 0)
            self.assertEqual(terminal.encode_key("f3", SHIFT), b"\x1b[1;2R")
            terminal.feed(b"\x1b[?1049l\x1b[<u")
            self.assertEqual(terminal.keyboard_flags, 1)
            self.assertEqual(terminal.encode_key("f3", SHIFT), b"\x1b[13;2~")
            terminal.feed(b"\x1b[<u")
            self.assertEqual(terminal.keyboard_flags, 0)
            self.assertEqual(terminal.encode_key("f3", SHIFT), b"\x1b[1;2R")

    def test_all_advertised_modified_navigation_matches_native_encoding(self):
        """The remaining 65 navigation entries retain their exact advertised bytes."""
        keys = (
            ("DC", "delete"),
            ("DN", "down"),
            ("END", "end"),
            ("HOM", "home"),
            ("IC", "insert"),
            ("LFT", "left"),
            ("NXT", "pagedown"),
            ("PRV", "pageup"),
            ("RIT", "right"),
            ("UP", "up"),
        )
        modifiers = (
            (2, SHIFT),
            (3, ALT),
            (4, SHIFT | ALT),
            (5, CTRL),
            (6, SHIFT | CTRL),
            (7, ALT | CTRL),
        )
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(self.capability("smkx"))
            for suffix, key in keys:
                for number, modifier in modifiers:
                    name = "k" + suffix + (str(number) if number != 2 else "")
                    with self.subTest(capability=name):
                        self.assertEqual(
                            terminal.encode_key(key, modifier), self.capability(name)
                        )
            for name, key, modifier in (
                ("kbeg", "kpbegin", 0),
                ("kbs", "backspace", 0),
                ("kcbt", "tab", SHIFT),
                ("kind", "down", SHIFT),
                ("kri", "up", SHIFT),
            ):
                with self.subTest(capability=name):
                    self.assertEqual(
                        terminal.encode_key(key, modifier), self.capability(name)
                    )

    def test_all_advertised_keypad_entries_match_application_keypad_mode(self):
        """All 18 keypad promises apply with the native numeric override disabled."""
        keys = (
            ("ka1", "kp7"),
            ("ka2", "kp8"),
            ("ka3", "kp9"),
            ("kb1", "kp4"),
            ("kb2", "kp5"),
            ("kb3", "kp6"),
            ("kc1", "kp1"),
            ("kc2", "kp2"),
            ("kc3", "kp3"),
            ("kent", "kpenter"),
            ("kp5", "kpbegin"),
            ("kpADD", "kpadd"),
            ("kpCMA", "kpseparator"),
            ("kpDIV", "kpdivide"),
            ("kpDOT", "kpdecimal"),
            ("kpMUL", "kpmultiply"),
            ("kpSUB", "kpsubtract"),
            ("kpZRO", "kp0"),
        )
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(self.capability("smkx") + b"\x1b[?1035l")
            for name, key in keys:
                with self.subTest(capability=name):
                    self.assertEqual(terminal.encode_key(key), self.capability(name))

    def test_keypad_separator_obeys_native_modes_modifiers_and_event_actions(self):
        """Separator fallback is legacy-only and never creates a release keystroke."""
        applications = (
            b"\x1bOl",
            b"\x1bO2l",
            b"\x1bO5l",
            b"\x1bO6l",
            b"\x1bO3l",
            b"\x1bO4l",
            b"\x1bO7l",
            b"\x1bO8l",
        )
        with Terminal(12, 4, lambda _: None) as terminal:
            for setup, expected in (
                (b"\x1b>", (b",",) * 8),
                (b"\x1b=\x1b[?1035l", applications),
                (b"\x1b[?1035h", (b",",) * 8),
                (b"\x1b[?1035l", applications),
                (b"\x1b>", (b",",) * 8),
            ):
                terminal.feed(setup)
                for modifier, wire in enumerate(expected):
                    for action in ("press", "repeat", "release"):
                        with self.subTest(
                            setup=setup, modifier=modifier, action=action
                        ):
                            self.assertEqual(
                                terminal.encode_key(
                                    "kpseparator", modifier, action=action
                                ),
                                b"" if action == "release" else wire,
                            )
            terminal.feed(b"\x1b=\x1b[?1035l\x1b[>3u")
            self.assertEqual(terminal.encode_key("kpseparator"), b"\x1b[57416u")
            self.assertEqual(
                terminal.encode_key("kpseparator", ALT, action="repeat"),
                b"\x1b[57416;3:2u",
            )
            self.assertEqual(
                terminal.encode_key("kpseparator", CTRL, action="release"),
                b"\x1b[57416;5:3u",
            )
            terminal.feed(b"\x1b[?1049h")
            self.assertEqual(terminal.encode_key("kpseparator"), b"\x1bOl")
            terminal.feed(b"\x1b[?1049l")
            self.assertEqual(terminal.encode_key("kpseparator"), b"\x1b[57416u")
            terminal.feed(b"\x1b[<u\x1b[!p")
            self.assertEqual(terminal.encode_key("kpseparator"), b",")

    def test_saved_cursor_tabs_wrap_and_screen_clear_use_database_bytes(self):
        """Save/restore, tab, wrap and clear promises work without handcrafted codes."""
        with Terminal(12, 4, lambda _: None) as terminal:
            terminal.feed(self.capability("cup", 1, 4) + self.capability("sc"))
            terminal.feed(self.capability("home") + b"X" + self.capability("rc") + b"Y")
            self.assertEqual(rows(terminal), ["X", "    Y", "", ""])
            terminal.feed(self.capability("clear"))
            self.assertEqual(rows(terminal), ["", "", "", ""])
            self.assertEqual(
                (terminal.frame().cursor_x, terminal.frame().cursor_y), (0, 0)
            )
            terminal.feed(self.capability("tbc") + self.capability("hpa", 4))
            terminal.feed(
                self.capability("hts")
                + self.capability("cr")
                + self.capability("ht")
                + b"T"
            )
            self.assertEqual(rows(terminal)[0], "    T")
            terminal.feed(self.capability("cbt") + b"B")
            self.assertEqual(rows(terminal)[0], "    B")
            terminal.feed(
                self.capability("rmam") + self.capability("cup", 2, 11) + b"123"
            )
            self.assertEqual(rows(terminal)[2], "           3")
            terminal.feed(
                self.capability("smam") + self.capability("cup", 2, 11) + b"AB"
            )
            self.assertEqual(rows(terminal)[2:], ["           A", "B"])


class LiveQueryContracts(unittest.TestCase):
    """Replies and mode-dependent input must reach the actual foreground process."""

    def test_three_shells_return_queries_and_keys_to_the_native_reader(self):
        """Run PTK with and without a bar; verify child files, not modeled ownership."""
        for shell in SHELLS:
            for plugins in (False, True):
                with (
                    self.subTest(shell=shell, plugins=plugins),
                    tempfile.TemporaryDirectory(prefix="ish-query-") as directory,
                ):
                    root = Path(directory)
                    (root / "query-reader.py").write_text(QUERY_READER)
                    terminal = HooklessTerminal(root, shell=shell, plugins=plugins)
                    try:
                        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
                        terminal.submit(
                            f"{shlex.quote(sys.executable)} query-reader.py"
                        )
                        terminal.result("query-result", b"\x1b[3;4R\x1b[?2004;1$y")
                        terminal.send(
                            b"\x1b[A\x1b[13;2~\x1b[13;5~" + "한글|done|".encode()
                        )
                        terminal.result(
                            "query-keys",
                            b"\x1bOA\x1b[1;2R\x1b[1;5R" + "한글|done|".encode(),
                        )
                        terminal.finish()
                    finally:
                        terminal.close()


class SearchContracts(unittest.TestCase):
    """Native search owns match identity through scrollback, resize and screen changes."""

    def complete(self, terminal):
        """Bound this test by progress count instead of using a production timeout."""
        for _ in range(200):
            complete, count = terminal.search_step(budget=1)
            if complete:
                return count
        self.fail("native search never completed")

    def test_history_search_selects_and_reveals_matches(self):
        """A match outside the viewport is selected as text and scrolled into view."""
        with Terminal(16, 3, lambda _: None) as terminal:
            terminal.feed(
                "old 한글 needle\r\n".encode() + b"line\r\n" * 20 + b"new NEEDLE"
            )
            terminal.set_search("needle")
            self.assertEqual(self.complete(terminal), 2)
            self.assertEqual(terminal.select_match(), 0)
            self.assertEqual(terminal.selection_text(), "NEEDLE")
            self.assertEqual(terminal.select_match(), 1)
            self.assertEqual(terminal.selection_text(), "needle")
            self.assertFalse(terminal.viewport_active)
            self.assertIn("old 한글 needle", rows(terminal))
            terminal.resize(20, 4)
            self.assertEqual(self.complete(terminal), 2)
            terminal.set_search("한글")
            self.assertEqual(self.complete(terminal), 1)
            self.assertEqual(terminal.select_match(), 0)
            self.assertEqual(terminal.selection_text(), "한글")

    def test_screen_reset_eviction_and_empty_queries_drop_old_results(self):
        """Search results follow native history lifetime and active-screen ownership."""
        with Terminal(20, 3, lambda _: None) as terminal:
            terminal.feed(b"primary")
            terminal.set_search("primary")
            self.assertEqual(self.complete(terminal), 1)
            terminal.feed(b"\x1b[?1049h\x1b[Halternate")
            self.assertEqual(self.complete(terminal), 0)
            terminal.feed(b"\x1b[?1049l")
            self.assertEqual(self.complete(terminal), 1)
            terminal.feed(b"\r\n" * 20)
            terminal.set_scrollback_limit(0, 0)
            self.assertEqual(self.complete(terminal), 0)
            self.assertIsNone(terminal.select_match())
            terminal.set_search("")
            self.assertEqual(self.complete(terminal), 0)


if __name__ == "__main__":
    unittest.main()
