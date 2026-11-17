"""Check concrete cursor observations without assuming any host's default style."""

import os
import re
import signal
import sys
import tempfile
import termios
import unittest
from pathlib import Path

from ish.terminal.host import HostBroker
from ish.terminal.vt import Terminal as NativeTerminal
from test_terminal_cli import Terminal

QUERY = b"\x1bP$q q\x1b\\"
SHAPE_COMMAND = re.compile(rb"\x1b\[([0-9]+) q")
STYLE_CASES = (
    (1, "block", True),
    (2, "block", False),
    (3, "underline", True),
    (4, "underline", False),
    (5, "bar", True),
    (6, "bar", False),
)
MODE_QUERY = re.compile(rb"\x1b\[\?([0-9]+)\$p")
MODE_WRITE = re.compile(rb"\x1b\[\?([0-9]+)([hl])")
INITIAL_MODES = {
    1: 1,
    6: 2,
    7: 2,
    12: 1,
    25: 2,
    1000: 2,
    1002: 1,
    1003: 2,
    1004: 1,
    1005: 2,
    1006: 1,
    1015: 2,
    1016: 2,
    2004: 1,
    2026: 2,
}
CHILD = r"""import os
import select
import termios
from pathlib import Path

saved = termios.tcgetattr(0)
raw = termios.tcgetattr(0)
raw[3] &= ~(termios.ICANON | termios.ECHO)
termios.tcsetattr(0, termios.TCSANOW, raw)
def query():
    os.write(1, b'\x1bP$q q\x1b\\')
    data = bytearray()
    while not data.endswith(b'\x1b\\'):
        if not select.select([0], [], [], 5)[0]:
            raise TimeoutError('native cursor reply missing')
        data.extend(os.read(0, 1024))
    return bytes(data)
try:
    Path('initial.bin').write_bytes(query())
    os.write(1, b'\x1b[2 q')
    Path('explicit.bin').write_bytes(query())
    os.write(1, b'\x1b[0 q')
    Path('reset.bin').write_bytes(query())
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


class ProbeTests(unittest.TestCase):
    """Only the first exact reply during this probe may supply a concrete style."""

    def test_concrete_cursor_values_are_interpreted_at_the_host_boundary(self):
        """Each literal reply has one independently expected shape and blink value."""
        for code, shape, blink in STYLE_CASES:
            with self.subTest(code=code):
                broker = HostBroker()
                broker.begin_probe(80, 24)
                self.assertTrue(broker.control(f"\x1bP1$r{code} q\x1b\\"))
                observed = broker.finish_probe().initial_cursor_style
                self.assertEqual(
                    (observed.code, observed.shape, observed.blink),
                    (code, shape, blink),
                )
                self.assertEqual(broker.restore_cursor_shape(), f"\x1b[{code} q")

    def test_missing_zero_failed_and_invalid_replies_do_not_guess(self):
        """Only concrete 1–6 values enable a reset default or a restoration write."""
        for reply in (
            None,
            "\x1bP1$r0 q\x1b\\",
            "\x1bP0$r q\x1b\\",
            "\x1bP1$r7 q\x1b\\",
            "\x1bP1$r05 q\x1b\\",
        ):
            with self.subTest(reply=reply):
                broker = HostBroker()
                broker.begin_probe(80, 24)
                if reply is not None:
                    broker.control(reply)
                self.assertIsNone(broker.finish_probe().initial_cursor_style)
                self.assertEqual(broker.restore_cursor_shape(), "")

    def test_unsolicited_duplicate_late_and_cancelled_replies_cannot_replace(self):
        """A response outside its single initial probe cannot rewrite ownership."""
        broker = HostBroker()
        broker.control("\x1bP1$r6 q\x1b\\")
        broker.begin_probe(80, 24)
        broker.control("\x1bP1$r5 q\x1b\\")
        broker.control("\x1bP1$r4 q\x1b\\")
        self.assertEqual(broker.finish_probe().initial_cursor_style.code, 5)
        broker.control("\x1bP1$r3 q\x1b\\")
        self.assertEqual(broker.profile.initial_cursor_style.code, 5)
        broker = HostBroker()
        broker.begin_probe(80, 24)
        broker.control("\x1bP1$r0 q\x1b\\")
        broker.control("\x1bP1$r6 q\x1b\\")
        self.assertIsNone(broker.finish_probe().initial_cursor_style)
        broker = HostBroker()
        broker.begin_probe(80, 24)
        broker.cancel_probe()
        broker.control("\x1bP1$r6 q\x1b\\")
        self.assertIsNone(broker.profile.initial_cursor_style)


class RestorationProbeTests(unittest.TestCase):
    """Restoration requires a first concrete reply and an actual owned change."""

    def test_exclusive_mouse_tracking_is_restored_after_other_modes_are_disabled(self):
        """A later tracking reset must not cancel the requested original mode."""
        for active, wire in (
            (1000, "\x1b[?1002l\x1b[?1003l\x1b[?1000h"),
            (1002, "\x1b[?1000l\x1b[?1003l\x1b[?1002h"),
            (1003, "\x1b[?1000l\x1b[?1002l\x1b[?1003h"),
        ):
            with self.subTest(active=active):
                broker = HostBroker()
                broker.begin_probe(80, 24)
                for mode in (1000, 1002, 1003):
                    broker.control(f"\x1b[?{mode};{1 if mode == active else 2}$y")
                broker.finish_probe()
                broker.change_modes(1000, 1002, 1003)
                restored = broker.restore_modes()
                self.assertEqual(restored, wire)
                replies = []
                with NativeTerminal(8, 3, replies.append) as outer:
                    outer.feed(restored.encode() + f"\x1b[?{active}$p".encode())
                self.assertEqual(b"".join(replies), f"\x1b[?{active};1$y".encode())

    def test_only_first_requested_concrete_modes_are_restored_once(self):
        """Missing, unsupported, permanent, late and duplicate values never win."""
        broker = HostBroker()
        broker.control("\x1b[?1004;2$y")
        broker.begin_probe(80, 24)
        for reply in (
            "\x1b[?1004;1$y",
            "\x1b[?1004;2$y",
            "\x1b[?7;2$y",
            "\x1b[?1000;0$y",
            "\x1b[?1000;1$y",
            "\x1b[?1002;3$y",
            "\x1b[?1003;4$y",
            "\x1b[?25;2$y",
        ):
            broker.control(reply)
        broker.finish_probe()
        broker.control("\x1b[?7;1$y")
        broker.change_modes(7, 1000, 1002, 1003, 1004, 2004)
        self.assertEqual(broker.restore_modes(), "\x1b[?7l\x1b[?1004h")
        self.assertEqual(broker.restore_modes(), "")
        broker.change_modes(25)
        self.assertEqual(broker.restore_modes(), "\x1b[?25l")

    def test_attributes_are_bounded_observed_values_not_reset_guesses(self):
        """A concrete SGR response is restored after an owned render only."""
        for response, expected in (
            ("\x1bP1$r0;1;38:2::17:34:51m\x1b\\", "\x1b[0;1;38:2::17:34:51m"),
            ("\x1bP0$rm\x1b\\", ""),
            ("\x1bP1$r999999" * 100, ""),
            (None, ""),
        ):
            with self.subTest(response=response):
                broker = HostBroker()
                broker.begin_probe(80, 24)
                if response is not None:
                    broker.control(response)
                broker.finish_probe()
                broker.control("\x1bP1$r0;31m\x1b\\")
                self.assertEqual(broker.restore_attributes(), "")
                broker.change_attributes()
                self.assertEqual(broker.restore_attributes(), expected)
                self.assertEqual(broker.restore_attributes(), "")


class NativeCursorTests(unittest.TestCase):
    """Default setters affect the next DECSCUSR reset, never the current child."""

    def test_native_defaults_wait_for_explicit_reset(self):
        """Default changes never overwrite a running child's explicit cursor style."""
        for code, shape, blink in STYLE_CASES:
            with (
                self.subTest(code=code),
                NativeTerminal(8, 3, lambda _: None) as terminal,
            ):
                terminal.feed(b"\x1b[6 q")
                terminal.set_default_cursor(shape, blink=blink)
                self.assertEqual(
                    (terminal.frame().cursor_shape, terminal.frame().cursor_blink),
                    ("bar", False),
                )
                terminal.feed(b"\x1b[0 q")
                self.assertEqual(
                    (terminal.frame().cursor_shape, terminal.frame().cursor_blink),
                    (shape, blink),
                )


class CursorTerminal(Terminal):
    """Inject only an explicit test host reply, recording pre-query output exactly."""

    def __init__(self, root, response):
        """Start a real native query consumer with one scripted host response."""
        self.response = response
        self.query_seen = False
        self.before_query = b""
        script = root / "cursor.py"
        script.write_text(CHILD)
        super().__init__(
            root,
            shell=sys.executable,
            configure=False,
            extra_args=(
                "--no-rc",
                "--no-plugins",
                "--shell-arg=" + str(script),
            ),
        )

    def receive(self):
        """Answer only after the exact host query appears in the real output."""
        super().receive()
        if not self.query_seen and QUERY in self.output:
            self.query_seen = True
            self.before_query = bytes(self.output[: self.output.index(QUERY)])
            if self.response is not None:
                self.send(self.response)


class CursorPTYTests(unittest.TestCase):
    """Observe real PTK output/native replies; host replies here are literal fixtures."""

    def test_child_first_size_already_excludes_selected_bar(self):
        """The initial PTY dimensions use real layout before any child can query."""
        for plugins, rows in ((False, 24), (True, 21)):
            with (
                self.subTest(plugins=plugins),
                tempfile.TemporaryDirectory(prefix="ish-first-size-") as directory,
            ):
                root = Path(directory)
                script = root / "size.py"
                script.write_text(
                    "import os\nfrom pathlib import Path\n"
                    "s = os.get_terminal_size(0)\n"
                    "Path('first-size').write_text(f'{s.columns} {s.lines}')\n"
                )
                args = ["--no-rc", "--shell-arg=" + str(script)]
                if plugins:
                    package = root / "plugins" / "bar"
                    package.mkdir(parents=True)
                    (package / "__init__.py").write_text(
                        "from prompt_toolkit.layout import Window\n"
                        "PLUGIN_META = {'version': '1'}\nTERMINAL_API = 1\n"
                        "def setup(api):\n    api.set_bar(Window(), rows=3)\n"
                    )
                    args.extend(
                        ["--plugin-dir", str(package.parent), "--plugin", "bar"]
                    )
                terminal = Terminal(
                    root, shell=sys.executable, configure=False, extra_args=args
                )
                try:
                    terminal.require(terminal.exited, 30)
                    self.assertEqual(terminal.status, 0)
                    self.assertEqual((root / "first-size").read_text(), f"100 {rows}")
                finally:
                    terminal.close()

    def test_initial_paint_does_not_change_query_and_exit_restores_after_ptk_reset(
        self,
    ):
        """Startup observation, child reset and final host output keep distinct roles."""
        for response, code in (
            (b"\x1bP1$r5 q\x1b\\", 5),
            (b"\x1bP1$r0 q\x1b\\", 2),
            (None, 2),
        ):
            with (
                self.subTest(response=response),
                tempfile.TemporaryDirectory(prefix="ish-cursor-") as directory,
            ):
                root = Path(directory)
                terminal = CursorTerminal(root, response)
                try:
                    terminal.require(terminal.exited, timeout=30)
                    terminal.receive()
                    self.assertEqual(
                        terminal.status, 0, repr(bytes(terminal.output[-2000:]))
                    )
                    self.assertTrue(terminal.query_seen)
                    self.assertFalse(SHAPE_COMMAND.findall(terminal.before_query))
                    self.assertEqual(
                        (root / "initial.bin").read_bytes(),
                        f"\x1bP1$r{code} q\x1b\\".encode(),
                    )
                    self.assertEqual(
                        (root / "explicit.bin").read_bytes(), b"\x1bP1$r2 q\x1b\\"
                    )
                    self.assertEqual(
                        (root / "reset.bin").read_bytes(),
                        f"\x1bP1$r{code} q\x1b\\".encode(),
                    )
                    if code == 5:
                        commands = SHAPE_COMMAND.findall(terminal.output)
                        self.assertEqual(commands[-1], b"5")
                        self.assertLess(
                            terminal.output.rfind(b"\x1b[?1049l"),
                            terminal.output.rfind(b"\x1b[5 q"),
                        )
                finally:
                    terminal.close()


class RestoringTerminal(Terminal):
    """Literal host replies exercise the live PTK cleanup against a real PTY."""

    def __init__(self, root, modes=None):
        """Start with deliberately non-default, independently specified modes."""
        self.answered = set()
        self.modes = INITIAL_MODES if modes is None else modes
        self.cursor_answered = self.attributes_answered = False
        super().__init__(root, extra_args=("--mouse=on",))

    def receive(self):
        """Reply once per startup request; do not derive expected state from ish."""
        super().receive()
        for query in MODE_QUERY.finditer(self.output):
            mode = int(query[1])
            if mode not in self.answered:
                self.answered.add(mode)
                self.send(f"\x1b[?{mode};{self.modes.get(mode, 0)}$y")
        if not self.cursor_answered and QUERY in self.output:
            self.cursor_answered = True
            self.send(b"\x1bP1$r5 q\x1b\\")
        if not self.attributes_answered and b"\x1bP$qm\x1b\\" in self.output:
            self.attributes_answered = True
            self.send(b"\x1bP1$r0;1;38;2;17;34;51m\x1b\\")


class RestorationPTYTests(unittest.TestCase):
    """A separate wire model verifies restoration and actual input consumers."""

    def terminal(self, modes=None):
        """Keep each live shell and all its output in a private temporary home."""
        root = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="ish-restore-"))
        )
        terminal = RestoringTerminal(root, modes)
        self.addCleanup(terminal.close)
        terminal.require(terminal.at_prompt, 45)
        return terminal

    def assert_modes(self, terminal):
        """Compare final emitted state to literal non-default startup values."""
        values = dict(INITIAL_MODES)
        for match in MODE_WRITE.finditer(terminal.output):
            values[int(match[1])] = 1 if match[2] == b"h" else 2
        self.assertEqual({key: values[key] for key in INITIAL_MODES}, INITIAL_MODES)
        self.assertEqual(SHAPE_COMMAND.findall(terminal.output)[-1], b"5")
        self.assertIn(b"\x1b[0;1;38;2;17;34;51m", terminal.output)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.attrs)

    def test_initial_queries_precede_any_mode_change_and_exit_restores(self):
        """PTK construction and first paint must not overwrite queried originals."""
        terminal = self.terminal()
        first_write = MODE_WRITE.search(terminal.output)
        self.assertIsNotNone(first_write)
        for mode in INITIAL_MODES:
            self.assertIn(
                f"\x1b[?{mode}$p".encode(), terminal.output[: first_write.start()]
            )
        self.assertIn(QUERY, terminal.output[: first_write.start()])
        terminal.send(b"exit\r")
        terminal.require(terminal.exited)
        terminal.receive()
        self.assertEqual(terminal.status, 0)
        self.assert_modes(terminal)

    def test_term_and_hup_restore_the_same_observed_values(self):
        """Signal cleanup has the same final outer state as native EOF."""
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                terminal = self.terminal()
                os.kill(terminal.pid, signum)
                terminal.require(terminal.exited)
                terminal.receive()
                self.assertEqual(terminal.status, 128 + signum)
                self.assert_modes(terminal)

    def test_origin_restoration_keeps_the_primary_cursor_and_scroll_region(self):
        """DECOM writes cannot home the primary cursor after alternate exit."""
        terminal = self.terminal(INITIAL_MODES | {6: 1})
        terminal.send(b"exit\r")
        terminal.require(terminal.exited)
        terminal.receive()
        with NativeTerminal(100, 24, lambda _: None) as outer:
            outer.feed(b"\x1b[2;21r\x1b[?6h\x1b[3;4H")
            self.assertEqual((outer.frame().cursor_x, outer.frame().cursor_y), (3, 3))
            outer.feed(bytes(terminal.output))
            self.assertEqual((outer.frame().cursor_x, outer.frame().cursor_y), (3, 3))
            outer.feed(b"\x1b[1;1H")
            self.assertEqual((outer.frame().cursor_x, outer.frame().cursor_y), (0, 1))

    def test_suspend_restores_and_resume_retains_the_same_native_shell(self):
        """The stopped process leaves usable host state and resumes native input."""
        terminal = self.terminal()
        native_pid = terminal.state()["child_pid"]
        os.kill(terminal.pid, signal.SIGTSTP)

        def stopped():
            pid, status = os.waitpid(terminal.pid, os.WUNTRACED | os.WNOHANG)
            return bool(pid and os.WIFSTOPPED(status))

        terminal.require(stopped)
        terminal.receive()
        self.assert_modes(terminal)
        terminal.resize(29, 91)
        os.kill(terminal.pid, signal.SIGCONT)
        terminal.submit("echo resumed > resumed.txt")
        terminal.result("resumed.txt", b"resumed\n")
        terminal.require(lambda: terminal.state().get("cols") == 91)
        self.assertEqual(terminal.state()["child_pid"], native_pid)
        terminal.send(b"exit\r")
        terminal.require(terminal.exited)
        terminal.receive()
        self.assert_modes(terminal)


if __name__ == "__main__":
    unittest.main()
