"""Verify Bash semantic handoff through the real ish CLI and PTK key bindings."""

import contextlib
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 15
STARTUP_DEADLINE = 30
WITNESS = b"\x1b[19~"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
EDITOR_ON = b"\x1b[?2004h"
EDITOR_OFF = b"\x1b[?2004l"

RC = """
import json
import os
from pathlib import Path
from semantic_workers import input_probe, probe
prompt.set_tool('probe', function=probe)
prompt.set_tool('inputprobe', function=input_probe)
saved = []
def witness(event):
    shell = prompt.interactive_shell
    request = shell.edit_request
    wire = shell._handoff.request
    if wire is not None and wire.kind.value == 'C':
        body = f'1;{shell.signals.token};{wire.submission};{wire.sequence};C'.encode()
        saved.append(bytes((1,)) + b'edit' + bytes((31,)) + body + bytes((4,)))
    value = {
        'running': prompt.app.is_running,
        'commands': None if request is None else request.commands,
        'text': prompt.default_buffer.text,
    }
    with open('witnesses', 'a') as output:
        print(json.dumps(value), file=output)
prompt.set_key('f8', handler=witness)
def replay_old(event):
    shell = prompt.interactive_shell
    fd = os.open(shell.shell_pipe_path, os.O_WRONLY)
    try:
        os.write(fd, saved[0] + saved[-1] + saved[-1])
    finally:
        os.close(fd)
    old_ack = str(shell.accepted_prompt_id).encode() + bytes((10,))
    os.write(shell.input_ack_pipe, old_ack + old_ack)
    Path('injected').write_text('done')
prompt.set_key('f9', handler=replay_old)
"""
WORKERS = """
from pathlib import Path
def probe(*args):
    Path('tool-ran').write_text(repr(args))
def input_probe():
    Path('input-result').write_text(input('INPUT> '))
"""


class Terminal:
    """Own a controlling PTY and observe files written by actual PTK callbacks."""

    def __init__(self, root):
        """Start an isolated CLI using the default Bash adapter's verified capability."""
        self.root, self.output, self.cpr, self.witness_count = root, bytearray(), b"", 0
        self.submit_cycle = 0
        (root / ".bashrc").write_text("PS1='READY> '\n")
        (root / "semantic_workers.py").write_text(WORKERS)
        (root / "ish").mkdir()
        (root / "ish/.ishrc.py").write_text(RC)
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(root))),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            os.execve(
                sys.executable,
                [sys.executable, "-B", "-m", "ish.main", shutil.which("bash")],
                env,
            )
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))

    def receive(self):
        """Answer terminal cursor queries while collecting diagnostic output."""
        if not select.select([self.fd], [], [], 0.02)[0]:
            return
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return
        self.output.extend(data)
        queries = self.cpr + data
        self.cpr = queries[-3:]
        for _ in range(queries.count(b"\x1b[6n")):
            os.write(self.fd, b"\x1b[1;1R")

    def require(self, predicate, timeout=DEADLINE):
        """Use timeout only to fail a missing observable result."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.receive()
        if not predicate():
            raise AssertionError(repr(bytes(self.output[-5000:])))

    def send(self, data):
        """Deliver exact terminal input, including escape sequences and control bytes."""
        data = data.encode() if isinstance(data, str) else data
        if b"\r" in data:
            self.submit_cycle = self.output.count(EDITOR_ON)
        os.write(self.fd, data)

    def witness(self, *, sent=False):
        """Require a real PTK binding to attest editor activation and its text."""
        if not sent:
            # A result file can be written before its process exits. Sending a
            # custom key then would correctly give it native replay semantics.
            # Observe the next PTK primary before asking its bound callback.
            self.receive()
            self.require(
                lambda: (
                    self.output.count(EDITOR_ON) > self.submit_cycle
                    and self.output.rfind(EDITOR_ON) > self.output.rfind(EDITOR_OFF)
                    and b"READY>" in self.output[self.output.rfind(EDITOR_ON) :]
                )
            )
            self.send(WITNESS)
        self.witness_count += 1
        path = self.root / "witnesses"
        self.require(
            lambda: (
                path.exists()
                and len(path.read_text().splitlines()) >= self.witness_count
            )
        )
        result = json.loads(path.read_text().splitlines()[self.witness_count - 1])
        if not result["running"]:
            raise AssertionError(result)
        return result

    def result(self, name, expected):
        """Wait for the final file bytes, not merely an internal routing state."""
        path = self.root / name
        self.require(lambda: path.exists() and path.read_bytes() == expected)

    def close(self):
        """Terminate and reap only this CLI, allowing its normal session cleanup."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)
        deadline = time.monotonic() + DEADLINE
        while True:
            reaped, _ = os.waitpid(self.pid, os.WNOHANG)
            if reaped:
                break
            if time.monotonic() >= deadline:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
                break
            self.receive()
        os.close(self.fd)


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class BashSemanticCLITests(unittest.TestCase):
    """Check consumers and data through actual primary and continuation editors."""

    def setUp(self):
        """Wait for the initial PTK UI; all later editor evidence uses bound callbacks."""
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ish-semantic-cli-")
        )
        self.root = Path(directory)
        self.terminal = Terminal(self.root)
        self.addCleanup(self.terminal.close)
        self.terminal.require(
            lambda: (
                b"READY>" in self.terminal.output and EDITOR_ON in self.terminal.output
            ),
            STARTUP_DEADLINE,
        )

    def test_fast_heredoc_input_uses_ptk_and_never_dispatches_tools(self):
        """Text immediately after Enter belongs to PTK syntax editing, including tool names."""
        terminal = self.terminal
        terminal.send(b"cat <<'END' > result\r" + "  probe 한 글  ".encode() + WITNESS)
        self.assertEqual(
            terminal.witness(sent=True),
            {"running": True, "commands": False, "text": "  probe 한 글  "},
        )
        terminal.send(b"\rish_exit" + WITNESS)
        self.assertEqual(
            terminal.witness(sent=True),
            {"running": True, "commands": False, "text": "ish_exit"},
        )
        terminal.send(b"\rEND\r")
        terminal.result("result", "  probe 한 글  \nish_exit\n".encode())
        self.assertTrue(terminal.witness()["commands"])
        self.assertFalse((self.root / "tool-ran").exists())

    def test_fast_read_input_and_returned_next_command_reach_their_consumers(self):
        """Native read gets one line and unread typeahead becomes the next PTK submission."""
        terminal = self.terminal
        terminal.send(
            'IFS= read -r value; printf "%s" "$value" > read-result\r  native 한 글  \recho next > next-result\r'
        )
        terminal.result("read-result", "  native 한 글  ".encode())
        terminal.result("next-result", b"next\n")
        self.assertTrue(terminal.witness()["commands"])

    def test_blank_and_syntax_error_return_fast_input_to_primary_editor(self):
        """No raw decision is needed before the confirmed next primary consumes pending keys."""
        terminal = self.terminal
        terminal.send(b"\rnext" + WITNESS)
        self.assertEqual(
            terminal.witness(sent=True),
            {"running": True, "commands": True, "text": "next"},
        )
        terminal.send(b"\x03)\rsecond" + WITNESS)
        self.assertEqual(
            terminal.witness(sent=True),
            {"running": True, "commands": True, "text": "second"},
        )

    def test_multiline_paste_preserves_heredoc_data_and_following_command(self):
        """A pasted block is consumed line by line according to actual shell requests."""
        terminal = self.terminal
        body = (
            "cat <<'END' > result\nprobe 한 글\n  ish_exit  \nEND\necho after > after\n"
        )
        terminal.send(PASTE_START + body.encode() + PASTE_END + b"\r")
        terminal.result("result", "probe 한 글\n  ish_exit  \n".encode())
        terminal.result("after", b"after\n")
        self.assertFalse((self.root / "tool-ran").exists())

    def test_ctrl_c_cancels_continuation_and_preserves_following_typeahead(self):
        """The next primary receives fresh keys after cancelling a PTK syntax editor."""
        terminal = self.terminal
        terminal.send(b"for item in one; do\r" + WITNESS)
        self.assertFalse(terminal.witness(sent=True)["commands"])
        terminal.send(b"discarded\x03echo intact > result\r")
        terminal.result("result", b"intact\n")
        self.assertTrue(terminal.witness()["commands"])

    def test_ctrl_d_finishes_heredoc_and_returns_to_primary(self):
        """EOF at an empty continuation editor is passed to the waiting shell parser."""
        terminal = self.terminal
        terminal.send(b"cat <<'END' > result\rbody\r" + WITNESS)
        self.assertEqual(
            terminal.witness(sent=True),
            {"running": True, "commands": False, "text": ""},
        )
        terminal.send(b"\x04")
        terminal.result("result", b"body\n")
        self.assertTrue(terminal.witness()["commands"])

    def test_cancelled_duplicate_wire_events_and_old_acks_leave_new_edit_intact(self):
        """Replay real old wire identities while PTK owns a different syntax request."""
        terminal = self.terminal
        terminal.send(b"for item in one; do\r" + WITNESS)
        self.assertFalse(terminal.witness(sent=True)["commands"])
        terminal.send(b"\x03" + WITNESS)
        self.assertTrue(terminal.witness(sent=True)["commands"])
        terminal.send(b"cat <<'END' > result\r" + WITNESS)
        self.assertFalse(terminal.witness(sent=True)["commands"])
        terminal.send(b"\x1b[20~kept\rEND\r")
        terminal.result("injected", b"done")
        terminal.result("result", b"kept\n")
        self.assertTrue(terminal.witness()["commands"])

    def test_external_stdin_and_python_input_keep_their_native_paths(self):
        """Program input and Python input() consume typeahead as data under semantic mode."""
        terminal = self.terminal
        terminal.send(b"cat > external\rprobe native-data\r\x04")
        terminal.result("external", b"probe native-data\n")
        self.assertTrue(terminal.witness()["commands"])
        terminal.send("inputprobe\r한 글 input\r")
        terminal.result("input-result", "한 글 input".encode())
        self.assertTrue(terminal.witness()["commands"])
        self.assertFalse((self.root / "tool-ran").exists())

    def test_source_and_command_substitution_consume_native_stdin(self):
        """Running shell code retains stdin even when its own source contains shell syntax."""
        terminal = self.terminal
        (self.root / "source.sh").write_text(
            'IFS= read -r value\nprintf "%s" "$value" > sourced\n'
        )
        terminal.send("source ./source.sh\r  source data  \r")
        terminal.result("sourced", b"  source data  ")
        self.assertTrue(terminal.witness()["commands"])
        terminal.send(
            'value=$(IFS= read -r line; printf "%s" "$line"); printf "%s" "$value" > substituted\r  substitution  \r'
        )
        terminal.result("substituted", b"  substitution  ")
        self.assertTrue(terminal.witness()["commands"])


if __name__ == "__main__":
    unittest.main()
