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
F2, F10 = b"\x1bOQ", b"\x1b[21~"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
TCSH = shutil.which("tcsh")

OUTPUT_READER = """import sys
from pathlib import Path

Path('program-ready').write_text('waiting')
print('LIVE_NATIVE_OUTPUT', flush=True)
Path('native-result').write_text(sys.stdin.readline())
"""

KEY_READER = """import os
import termios
import tty
from pathlib import Path

saved = termios.tcgetattr(0)
data = bytearray()
try:
    tty.setraw(0)
    Path('keys-ready').write_text('raw')
    while not data.endswith(b'|done|'):
        data.extend(os.read(0, 128))
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
Path('key-result').write_bytes(data)
"""

COLOR_READER = """import sys
from pathlib import Path

sys.stdout.write('\\x1b[0m\\x1b[2J\\x1b[HDEFAULT \\x1b[48;2;0;0;0mBLACK \\x1b[49mRESET')
sys.stdout.flush()
Path('color-ready').write_text('waiting')
sys.stdin.readline()
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
        if state.get("alternate") or "cursor" not in state:
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
        """Resize the outer terminal and expect the native child to use all of it."""
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
    """Check native shell behavior, actual rendered cells, and final input consumers."""

    def setUp(self):
        """Start an isolated shell occupying the entire outer terminal."""
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="ish-vt-cli-"))
        self.root = Path(directory)
        self.terminal = Terminal(self.root)
        self.addCleanup(self.terminal.close)
        self.terminal.require(self.terminal.at_prompt, STARTUP_DEADLINE)
        self.require_full_screen()

    def require_full_screen(self):
        """Confirm native terminal dimensions without any reserved UI rows."""
        terminal = self.terminal
        terminal.require(
            lambda: terminal.frame_has("READY>") or terminal.state()["alternate"]
        )
        state = terminal.state()
        self.assertEqual((state["rows"], state["cols"]), (terminal.rows, terminal.cols))
        self.assertNotIn("popup", state)
        self.assertNotIn("popup_text", state)
        self.assertFalse(terminal.frame_has("ish-vt |"))
        self.assertFalse(terminal.frame_has("ish-vt popup"))

    def test_native_backslash_continuation_keeps_both_lines(self):
        """Native tcsh collects continuation while PTK supplies only terminal rendering."""
        terminal = self.terminal
        terminal.submit("printf '%s\\n' one \\")
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        terminal.require(lambda: terminal.frame_has("CONT>"))
        terminal.submit("two > backslash")
        terminal.result("backslash", b"one\ntwo\n")
        terminal.require(terminal.at_prompt)
        self.require_full_screen()

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
        self.require_full_screen()

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
        """Tab reaches the native tcsh editor instead of triggering a PTK widget."""
        terminal = self.terminal
        (self.root / "uniquely-completed-file").write_text("native completion\n")
        terminal.send("cat uniquely-comp\t > completed\r")
        terminal.result("completed", b"native completion\n")
        terminal.require(terminal.at_prompt)

    def test_external_program_stdin_and_output_remain_native(self):
        """Program output is rendered while its ordinary stdin receives exact data."""
        terminal = self.terminal
        (self.root / "output-reader.py").write_text(OUTPUT_READER)
        terminal.submit(f"{shlex.quote(sys.executable)} output-reader.py")
        terminal.result("program-ready", b"waiting")
        terminal.require(lambda: terminal.frame_has("LIVE_NATIVE_OUTPUT"))
        terminal.send("  real stdin 한 글  \r")
        terminal.result("native-result", "  real stdin 한 글  \n".encode())
        terminal.require(terminal.at_prompt)

    def test_bracketed_paste_waits_for_native_enter(self):
        """A shell paste remains editable until native Enter submits the command."""
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

    def test_f2_and_f10_reach_the_native_stdin_consumer(self):
        """Former application shortcuts are ordinary native xterm function keys."""
        terminal = self.terminal
        (self.root / "key-reader.py").write_text(KEY_READER)
        terminal.submit(f"{shlex.quote(sys.executable)} key-reader.py")
        terminal.result("keys-ready", b"raw")
        terminal.send(F2 + F10 + b"|done|")
        terminal.result("key-result", b"\x1bOQ\x1b[21~|done|")
        terminal.require(terminal.at_prompt)
        terminal.submit("printf alive > after-keys")
        terminal.result("after-keys", b"alive")
        terminal.require(terminal.at_prompt)
        self.require_full_screen()

    def test_default_background_and_explicit_black_are_distinct_in_outer_cells(self):
        """Default colors inherit the host while an explicit RGB black stays black."""
        terminal = self.terminal
        (self.root / "color-reader.py").write_text(COLOR_READER)
        terminal.submit(f"{shlex.quote(sys.executable)} color-reader.py")
        terminal.result("color-ready", b"waiting")
        terminal.require(
            lambda: terminal.screen.display[0].startswith("DEFAULT BLACK RESET")
        )
        row = terminal.screen.buffer[0]
        self.assertEqual(row[0].bg, "default")
        self.assertIn(row[8].bg, {"black", "000000"})
        self.assertEqual(row[14].bg, "default")
        self.assertEqual(
            terminal.screen.buffer[terminal.rows - 1][terminal.cols - 1].bg,
            "default",
        )
        terminal.send(b"\r")
        terminal.require(terminal.at_prompt)

    @unittest.skipUnless(shutil.which("vim"), "vim is required")
    def test_vim_save_resize_and_return_use_the_full_terminal_size(self):
        """A real alternate-screen editor uses every row and preserves pasted Unicode."""
        terminal = self.terminal
        terminal.submit("stty size > initial-size")
        terminal.result("initial-size", f"{ROWS} {COLS}\n".encode())
        terminal.require(terminal.at_prompt)
        terminal.submit("vim -Nu NONE -n -i NONE edited.txt")
        terminal.require(lambda: terminal.state().get("alternate") is True)
        terminal.require(lambda: terminal.frame_has("edited.txt"))
        self.require_full_screen()
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
        terminal.resize(33, 110)
        terminal.require(
            lambda: (
                (terminal.state().get("rows"), terminal.state().get("cols"))
                == (33, 110)
            )
        )
        terminal.send(
            ":call writefile([string(&lines), string(&columns)], 'vim-size')\r"
        )
        terminal.result("vim-size", b"33\n110\n")
        self.require_full_screen()
        terminal.send(":q\r")
        terminal.require(terminal.at_prompt)
        terminal.require(lambda: terminal.frame_has("READY>"))
        self.assertFalse(terminal.state()["alternate"])
        self.require_full_screen()
        terminal.submit("stty size > returned-size")
        terminal.result("returned-size", b"33 110\n")

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
