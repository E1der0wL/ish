"""Verify the experimental terminal CLI using real PTYs and an independent screen."""

import contextlib
import errno
import fcntl
import json
import os
import select
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

import psutil
import pyte

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 15
STARTUP_DEADLINE = 45
ROWS, COLS = 24, 100
F2 = b"\x1bOQ"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
POPUP_TITLE = "ish-vt popup"
BAR = "ish-vt | tcsh | F2 popup | F10 quit"
TCSH = shutil.which("tcsh")

OUTPUT_READER = """import signal
import sys
import time
from pathlib import Path

def interrupted(signum, frame):
    Path('unexpected-signal').write_text(str(signum))
    raise SystemExit(130)

signal.signal(signal.SIGINT, interrupted)
Path('program-ready').write_text('waiting')
while not Path('release-output').exists():
    time.sleep(0.005)
print('CHILD_OUTPUT_WHILE_POPUP', flush=True)
Path('native-result').write_text(sys.stdin.readline())
"""

INTERRUPT_READER = """import signal
import time
from pathlib import Path

def interrupted(signum, frame):
    Path('interrupted').write_text(str(signum))
    raise SystemExit(130)

signal.signal(signal.SIGINT, interrupted)
Path('interrupt-ready').write_text('waiting')
while True:
    time.sleep(1)
"""


class Terminal:
    """Drive one real outer terminal without consulting production input policy."""

    def __init__(self, root, *, rows=ROWS, cols=COLS):
        """Launch the new CLI in a private HOME with a known native tcsh prompt."""
        self.root = root
        self.rows, self.cols = rows, cols
        self.output = bytearray()
        self.query_tail = b""
        self.status = None
        self.prompt_before = None
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.state_path = root / "terminal-state.json"
        (root / ".tcshrc").write_text(
            "set prompt = 'READY> '\n"
            "set prompt2 = 'CONT> '\n"
            "set history = 100\n"
            "unset autologout\n"
        )
        environment = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name, value in os.environ.items():
            if name.startswith("ISH_GHOSTTY_"):
                environment[name] = value
        self.fd, slave = pty_open(rows, cols)
        self.attrs = termios.tcgetattr(slave)
        self.pid = os.fork()
        if self.pid == 0:
            os.close(self.fd)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for target in (0, 1, 2):
                os.dup2(slave, target)
            if slave > 2:
                os.close(slave)
            os.chdir(root)
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "ish.terminal",
                    "--shell",
                    TCSH,
                    "--cwd",
                    str(root),
                    "--dump-state",
                    str(self.state_path),
                ],
                environment,
            )
        os.close(slave)

    def send(self, data):
        """Deliver terminal bytes exactly, including native and PTK control keys."""
        data = data.encode() if isinstance(data, str) else data
        while data:
            count = os.write(self.fd, data)
            data = data[count:]

    def submit(self, text):
        """Press native Enter after one physical line."""
        state = self.state()
        self.prompt_before = (
            tuple(state.get("lines", [])),
            tuple(state.get("cursor", [])),
        )
        self.send(text + "\r")

    def receive(self):
        """Reconstruct outer frames independently and answer PTK cursor queries."""
        if not select.select([self.fd], [], [], 0.02)[0]:
            return
        try:
            data = os.read(self.fd, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            return
        if not data:
            return
        self.output.extend(data)
        self.stream.feed(data)
        combined = self.query_tail + data
        self.query_tail = combined[-3:]
        for _ in range(combined.count(b"\x1b[6n")):
            self.send(b"\x1b[1;1R")

    def state(self):
        """Read explicit opt-in diagnostics; these never grant production ownership."""
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}

    def frame_has(self, text):
        """Search actual rendered outer cells, not the child's diagnostic snapshot."""
        return any(text in line for line in self.screen.display)

    def at_prompt(self, continuation=False):
        """Observe the current child cursor line solely to synchronize the test."""
        state = self.state()
        if state.get("popup") or state.get("alternate") or "cursor" not in state:
            return False
        lines = state.get("lines", [])
        row = state["cursor"][1]
        expected = "CONT>" if continuation else "READY>"
        current = (tuple(lines), tuple(state["cursor"]))
        return (
            current != self.prompt_before
            and row < len(lines)
            and lines[row].rstrip().endswith(expected)
        )

    def require(self, predicate, timeout=DEADLINE):
        """Use elapsed time only to fail an outcome that never arrived."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.receive()
            if self.exited():
                self.receive()
                break
        if not predicate():
            raise AssertionError(
                json.dumps(
                    {
                        "state": self.state(),
                        "frame": self.screen.display,
                        "output": repr(bytes(self.output[-4000:])),
                    },
                    ensure_ascii=False,
                )
            )

    def result(self, name, expected):
        """Require exact bytes from the actual final consumer."""
        path = self.root / name
        self.require(lambda: path.exists() and path.read_bytes() == expected)

    def resize(self, rows, cols):
        """Resize the outer terminal; the child pane must reserve exactly one row."""
        self.rows, self.cols = rows, cols
        self.screen.resize(lines=rows, columns=cols)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def exited(self):
        """Observe and reap the CLI once."""
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status is not None

    def finish(self):
        """Let native tcsh consume EOF and check outer terminal restoration."""
        self.require(self.at_prompt)
        self.send(b"\x04")
        self.require(self.exited)
        if self.status != 0:
            raise AssertionError((self.status, repr(bytes(self.output[-3000:]))))
        if termios.tcgetattr(self.fd) != self.attrs:
            raise AssertionError("The outer terminal's termios was not restored")

    def close(self):
        """Clean up only this fixture and its descendants, including failed sessions."""
        if self.status is None:
            descendants = []
            with contextlib.suppress(psutil.NoSuchProcess):
                descendants = psutil.Process(self.pid).children(recursive=True)
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while not self.exited() and time.monotonic() < deadline:
                self.receive()
            if self.status is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(self.pid, signal.SIGKILL)
                _, status = os.waitpid(self.pid, 0)
                self.status = os.waitstatus_to_exitcode(status)
            for process in reversed(descendants):
                with contextlib.suppress(psutil.NoSuchProcess):
                    process.kill()
        os.close(self.fd)


def pty_open(rows, cols):
    """Set initial geometry before the application can change terminal attributes."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return master, slave


@unittest.skipUnless(TCSH, "tcsh is required")
class TerminalCLITests(unittest.TestCase):
    """Check native shell behavior, actual rendered UI, and final input consumers."""

    def setUp(self):
        """Start one independent terminal and observe its native prompt and PTK bar."""
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="ish-vt-cli-"))
        self.root = Path(directory)
        self.terminal = Terminal(self.root)
        self.addCleanup(self.terminal.close)
        self.terminal.require(self.terminal.at_prompt, STARTUP_DEADLINE)
        self.require_bar()

    def require_bar(self):
        """Verify the visible one-row bar is outside the child's terminal height."""
        terminal = self.terminal
        terminal.require(lambda: BAR in terminal.screen.display[-1])
        self.assertEqual(sum(BAR in line for line in terminal.screen.display), 1)
        state = terminal.state()
        self.assertEqual(
            (state["rows"], state["cols"]), (terminal.rows - 1, terminal.cols)
        )

    def open_popup(self):
        """Require both popup focus diagnostics and the independently rendered frame."""
        terminal = self.terminal
        terminal.send(F2)
        terminal.require(
            lambda: terminal.state().get("popup") and terminal.frame_has(POPUP_TITLE)
        )
        self.assertTrue(terminal.frame_has("This input stays in the popup."))
        self.assertTrue(terminal.frame_has("Close"))
        self.require_bar()

    def require_popup_closed(self):
        """Wait until both the focus state and visible popup have been removed."""
        terminal = self.terminal
        terminal.require(
            lambda: (
                not terminal.state().get("popup")
                and not terminal.frame_has(POPUP_TITLE)
            )
        )
        self.require_bar()

    def test_native_backslash_continuation_keeps_both_lines(self):
        """Native tcsh collects continuation while PTK only supplies the terminal UI."""
        terminal = self.terminal
        terminal.submit("printf '%s\\n' one \\")
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        terminal.require(lambda: terminal.frame_has("CONT>"))
        terminal.submit("two > backslash")
        terminal.result("backslash", b"one\ntwo\n")
        terminal.require(terminal.at_prompt)
        self.require_bar()

    def test_nested_foreach_and_while_preread_then_execute(self):
        """The actual native parser waits for both end markers before side effects."""
        terminal = self.terminal
        terminal.submit("set count = 0")
        terminal.require(terminal.at_prompt)
        for line in (
            "while ( $count < 1 )",
            "foreach value ( A B )",
            "echo $value >> nested",
        ):
            terminal.submit(line)
            terminal.require(lambda: terminal.at_prompt(continuation=True))
        self.assertFalse((self.root / "nested").exists())
        terminal.send("end\r@ count++\rend\r")
        terminal.result("nested", b"A\nB\n")
        terminal.require(terminal.at_prompt)

    def test_heredoc_fast_lines_preserve_spaces_unicode_and_command_names(self):
        """Rapid native input reaches heredoc unchanged, including former ish names."""
        terminal = self.terminal
        payload = "  한 글 🙂  \nish_exit\nish_reload\nprobe\n"
        terminal.send("cat << END > heredoc\r" + payload.replace("\n", "\r") + "END\r")
        terminal.result("heredoc", payload.encode())
        terminal.require(terminal.at_prompt)
        self.require_bar()

    def test_dollar_input_consumes_native_data(self):
        """The shell's ordinary stdin consumer receives an exact fast Enter batch."""
        terminal = self.terminal
        terminal.send(
            'set answer = "$<"; printf "%s" "$answer" > dollar-input\r'
            "  native 한 글  \r"
        )
        terminal.result("dollar-input", "  native 한 글  ".encode())
        terminal.require(terminal.at_prompt)

    def test_native_tab_completes_a_filename(self):
        """Tab is forwarded to the native tcsh editor rather than consumed by PTK."""
        terminal = self.terminal
        (self.root / "uniquely-completed-file").write_text("native completion\n")
        terminal.send("cat uniquely-comp\t > completed\r")
        terminal.result("completed", b"native completion\n")
        terminal.require(terminal.at_prompt)

    def test_popup_toggle_escape_and_close_button_keep_input_local(self):
        """F2, Escape, and the actual Close button return focus without shell input."""
        terminal = self.terminal
        self.open_popup()
        terminal.send("button-only")
        terminal.require(
            lambda: "button-only" in terminal.state().get("popup_text", "")
        )
        terminal.require(lambda: terminal.frame_has("button-only"))
        # Two Tabs must cycle back to the popup textarea, never the terminal.
        terminal.send(b"\t\t\rpopup-after-two-tabs")
        terminal.require(
            lambda: "popup-after-two-tabs" in terminal.state().get("popup_text", "")
        )
        self.assertTrue(terminal.state()["popup"])
        terminal.send(b"\x1bxafter-alt")
        terminal.require(lambda: "after-alt" in terminal.state().get("popup_text", ""))
        self.assertTrue(terminal.state()["popup"])
        self.assertTrue(terminal.frame_has(POPUP_TITLE))
        # Three further Tabs focus Close. Unbound text must not reach the shell.
        terminal.send(b"\t\t\techo>popup-leak\r")
        self.require_popup_closed()
        self.open_popup()
        terminal.send(b"\x1b")
        self.require_popup_closed()
        self.open_popup()
        terminal.send(F2)
        self.require_popup_closed()
        terminal.submit("printf isolated > popup-result")
        terminal.result("popup-result", b"isolated")
        terminal.require(terminal.at_prompt)
        self.assertFalse((self.root / "popup-leak").exists())

    def test_popup_during_execution_keeps_output_live_and_stdin_separate(self):
        """Child output updates under the popup while popup text and Ctrl+C stay local."""
        terminal = self.terminal
        (self.root / "output-reader.py").write_text(OUTPUT_READER)
        terminal.submit(f"{shlex.quote(sys.executable)} output-reader.py")
        terminal.result("program-ready", b"waiting")
        self.open_popup()
        terminal.send("echo POPUP_ONLY > leaked\r")
        terminal.require(lambda: "POPUP_ONLY" in terminal.state().get("popup_text", ""))
        terminal.require(lambda: terminal.frame_has("POPUP_ONLY"))
        (self.root / "release-output").touch()
        terminal.require(
            lambda: any(
                "CHILD_OUTPUT_WHILE_POPUP" in line
                for line in terminal.state().get("lines", [])
            )
        )
        terminal.require(lambda: terminal.frame_has("CHILD_OUTPUT_WHILE_POPUP"))
        self.assertTrue(terminal.state()["popup"])
        terminal.send(b"\x03")
        self.require_popup_closed()
        terminal.send("  real stdin 한 글  \r")
        terminal.result("native-result", "  real stdin 한 글  \n".encode())
        terminal.require(terminal.at_prompt)
        self.assertFalse((self.root / "leaked").exists())
        self.assertFalse((self.root / "unexpected-signal").exists())

    def test_bracketed_paste_uses_native_terminal_or_popup_focus(self):
        """The implemented paste path respects native Enter and popup text ownership."""
        terminal = self.terminal
        command = "printf '%s' 'shell paste 한 글' > pasted-shell"
        terminal.send(PASTE_START + command.encode() + PASTE_END)
        terminal.require(
            lambda: any(
                "pasted-shell" in line for line in terminal.state().get("lines", [])
            )
        )
        self.assertFalse((self.root / "pasted-shell").exists())
        terminal.send(b"\r")
        terminal.result("pasted-shell", "shell paste 한 글".encode())
        terminal.require(terminal.at_prompt)
        self.open_popup()
        text = "echo leaked > paste-leak\n  popup 한 글  "
        terminal.send(PASTE_START + text.encode() + PASTE_END)
        terminal.require(lambda: terminal.state().get("popup_text") == text)
        terminal.require(lambda: terminal.frame_has("popup 한 글"))
        self.assertTrue(terminal.state()["popup"])
        terminal.send(b"\x1b")
        self.require_popup_closed()
        terminal.submit("printf returned > paste-return")
        terminal.result("paste-return", b"returned")
        terminal.require(terminal.at_prompt)
        self.assertFalse((self.root / "paste-leak").exists())

    @unittest.skipUnless(shutil.which("vim"), "vim is required")
    def test_vim_save_resize_and_return_preserve_the_bar_and_pane_size(self):
        """A real alternate-screen editor receives pane geometry and saves native input."""
        terminal = self.terminal
        terminal.submit("stty size > initial-size")
        terminal.result("initial-size", f"{ROWS - 1} {COLS}\n".encode())
        terminal.require(terminal.at_prompt)
        terminal.submit("vim -Nu NONE -n -i NONE edited.txt")
        terminal.require(lambda: terminal.state().get("alternate") is True)
        terminal.require(lambda: terminal.frame_has("edited.txt"))
        self.require_bar()
        terminal.send(
            b"i"
            + PASTE_START
            + "Vim 한 글\nsecond line".encode()
            + PASTE_END
            + b"\x1b:w\r"
        )
        expected = "Vim 한 글\nsecond line\n".encode()
        terminal.result("edited.txt", expected)
        terminal.require(lambda: terminal.frame_has("Vim 한 글"))
        self.open_popup()
        text = "popup-only Vim input"
        terminal.send(text)
        terminal.require(lambda: terminal.state().get("popup_text") == text)
        terminal.require(lambda: terminal.frame_has(text))
        terminal.send(b"\x1b")
        self.require_popup_closed()
        self.assertTrue(terminal.state()["alternate"])
        terminal.require(lambda: terminal.frame_has("Vim 한 글"))
        terminal.send(":w\r")
        terminal.result("edited.txt", expected)
        terminal.resize(33, 110)
        terminal.require(
            lambda: (
                (terminal.state().get("rows"), terminal.state().get("cols"))
                == (32, 110)
            )
        )
        terminal.send(
            ":call writefile([string(&lines), string(&columns)], 'vim-size')\r"
        )
        terminal.result("vim-size", b"32\n110\n")
        self.require_bar()
        terminal.send(":q\r")
        terminal.require(terminal.at_prompt)
        terminal.require(lambda: terminal.frame_has("READY>"))
        self.assertFalse(terminal.state()["alternate"])
        self.require_bar()
        terminal.submit("stty size > returned-size")
        terminal.result("returned-size", b"32 110\n")

    def test_native_interrupt_eof_and_outer_terminal_restoration(self):
        """Ctrl+C reaches the running program; Ctrl+D reaches cat and then native tcsh."""
        terminal = self.terminal
        (self.root / "interrupt-reader.py").write_text(INTERRUPT_READER)
        terminal.submit(f"{shlex.quote(sys.executable)} interrupt-reader.py")
        terminal.result("interrupt-ready", b"waiting")
        terminal.send(b"\x03")
        terminal.result("interrupted", str(signal.SIGINT).encode())
        terminal.require(terminal.at_prompt)
        terminal.submit("cat > eof-result")
        terminal.require(lambda: (self.root / "eof-result").exists())
        terminal.send(b"native EOF\r\x04")
        terminal.result("eof-result", b"native EOF\n")
        terminal.require(terminal.at_prompt)
        terminal.finish()


if __name__ == "__main__":
    unittest.main()
