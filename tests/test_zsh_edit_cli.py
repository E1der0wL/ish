"""Verify zsh semantic ownership through the actual ish CLI and PTK editor."""

import contextlib
import fcntl
import json
import os
import pty
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

ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 30
DEADLINE = 10
EDIT_ON = b"\x1b[?2004h"
SNAPSHOT = b"\x1b[19~"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"

RC = """import json
from pathlib import Path
from witnesses import probe, inputtool
shell = prompt.interactive_shell
prompt.set_tool('probe', function=probe)
prompt.set_tool('inputtool', function=inputtool)
def snapshot(event):
    request = shell.edit_request
    with open('snapshots', 'a') as output:
        print(json.dumps({'commands': request.commands, 'id': request.id, 'boundary': request.boundary,
            'text': prompt.default_buffer.text, 'semantic': shell._semantic_enabled}), file=output)
prompt.set_key('f8', handler=snapshot)
"""

DELAY_RAW = """
import time
from ish.shell.handoff import Handoff, InputKind
receive = Handoff.receive
def delayed_receive(self, request):
    if request.kind is InputKind.RAW and Path('delay-raw').exists():
        deadline = time.monotonic() + 8
        while not Path('native-finished').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Native completion witness did not arrive')
            time.sleep(.001)
        Path('delay-raw').unlink()
    return receive(self, request)
Handoff.receive = delayed_receive
"""

DELAY_ACK = """
import os
from ish.shell.adapter.handlers import ZshInterrupt
acknowledge = ZshInterrupt.acknowledge
previous_ack = None
def held_acknowledge(self, data):
    if not Path('delay-ack').exists():
        return acknowledge(self, data)
    Path('ack-held').write_bytes(data)
    release_fd = os.open('release-ack', os.O_RDONLY | os.O_NONBLOCK)
    def release():
        global previous_ack
        if not os.read(release_fd, 32):
            return
        self.shell.loop.remove_reader(release_fd)
        os.close(release_fd)
        if previous_ack is not None:
            acknowledge(self, previous_ack)
        acknowledge(self, data)
        acknowledge(self, data)
        previous_ack = data
    self.shell.loop.add_reader(release_fd, release)
ZshInterrupt.acknowledge = held_acknowledge
"""

STALE_READER = """
from ish.shell.adapter.handlers import ZshInterrupt
ready = ZshInterrupt.ready
def stale_reader(self, data):
    if data.endswith(b';0'):
        Path('reader-stale').write_text(json.dumps({
            'frame': data.decode(), 'shell_pid': self.shell.shell_pid,
            'ready_id': self.state.ready_id,
            'continuation': self.shell.continuation_active.is_set()}))
        return
    return ready(self, data)
ZshInterrupt.ready = stale_reader
"""

SIGINT_READER = """import json
import os
from pathlib import Path
import signal
import sys
def interrupted(signum, frame):
    with open('signal-received', 'a') as output:
        print(signum, file=output)
signal.signal(signal.SIGINT, interrupted)
Path('program-ready').write_text(json.dumps([os.getpid(), os.getpgrp(), os.tcgetpgrp(0)]))
value = sys.stdin.readline()
Path('program-input').write_text(value)
"""


class ZshEditCLITests(unittest.TestCase):
    """Assert real editor ownership and final consumers, not just event state."""

    @contextlib.contextmanager
    def session(
        self, *, delay_raw=False, delay_ack=False, stale_reader=False, extra_rc=""
    ):
        """Run an isolated CLI with the default adapter and a public key witness."""
        executable = shutil.which("zsh")
        if not executable:
            self.skipTest("zsh is not installed")
        with tempfile.TemporaryDirectory(prefix="ish-zsh-edit-") as directory:
            root = Path(directory)
            (root / ".zshrc").write_text("PS1='READY> '\n" + extra_rc)
            (root / "ish").mkdir()
            (root / "witnesses.py").write_text(
                "from pathlib import Path\ndef probe(*args):\n"
                "    Path('tool-executed').write_text('unexpected')\n"
                "def inputtool():\n"
                "    Path('python-reply').write_text(input() + '\\n')\n"
            )
            (root / "ish/.ishrc.py").write_text(
                RC
                + (DELAY_RAW if delay_raw else "")
                + (DELAY_ACK if delay_ack else "")
                + (STALE_READER if stale_reader else "")
            )
            ack_fd = None
            if delay_ack:
                os.mkfifo(root / "release-ack")
                ack_fd = os.open(root / "release-ack", os.O_RDWR | os.O_NONBLOCK)
            self.ack_fd = ack_fd
            env = {
                "HOME": directory,
                "ZDOTDIR": directory,
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), directory)),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(root)
                os.execve(
                    sys.executable,
                    [sys.executable, "-B", "-m", "ish.main", executable],
                    env,
                )
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            self.pid, self.fd, self.output, self.cpr = pid, fd, bytearray(), b""
            self.snapshots = 0
            try:
                self.require(self.primary, STARTUP_TIMEOUT)
                self.assertTrue(self.snapshot(root)["semantic"])
                yield root
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + DEADLINE
                while True:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                    if reaped:
                        break
                    if time.monotonic() >= deadline:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                        break
                    self.receive()
                os.close(fd)
                if ack_fd is not None:
                    os.close(ack_fd)

    def send(self, data):
        """Send one ordered terminal batch, including intentional immediate typeahead."""
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def receive(self):
        """Collect output and answer cursor queries as a terminal would."""
        if select.select([self.fd], [], [], 0.02)[0]:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            self.output.extend(chunk)
            combined = self.cpr + chunk
            self.cpr = combined[-3:]
            for _ in range(combined.count(b"\x1b[6n")):
                self.send(b"\x1b[1;1R")

    def require(self, predicate, timeout=DEADLINE):
        """Use time only as the deadline for a failed test observation."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.receive()
        self.assertTrue(predicate(), repr(bytes(self.output[-3500:])))

    def primary(self):
        """Observe a rendered primary editor for test synchronization only."""
        return EDIT_ON in self.output and b"READY>" in self.output

    def editor(self):
        """Observe a newly enabled PTK before asking its binding for ownership."""
        return EDIT_ON in self.output

    def snapshot(self, root):
        """Read the live PTK buffer and dispatch policy through a registered key."""
        self.snapshots += 1
        self.send(SNAPSHOT)
        path = root / "snapshots"
        self.require(
            lambda: (
                path.exists() and len(path.read_text().splitlines()) == self.snapshots
            )
        )
        return json.loads(path.read_text().splitlines()[-1])

    def submit(self, text):
        """Begin observing a fresh submission and type its Enter key."""
        self.output.clear()
        self.send(text + "\r")

    def test_fast_continuation_input_uses_real_ptk_and_cannot_dispatch_a_tool(self):
        """Enter plus immediate body text reaches the continuation editor once."""
        with self.session() as root:
            self.output.clear()
            self.send("cat > result <<'END'\rprobe")
            self.require(self.editor)
            state = self.snapshot(root)
            self.assertFalse(state["commands"])
            self.assertEqual(state["text"], "probe")
            self.output.clear()
            self.send(" 한 글\r")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit("END")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "probe 한 글\n")
            self.assertFalse((root / "tool-executed").exists())

    def test_fast_raw_and_immediate_primary_input_keep_their_consumers(self):
        """A waiting builtin consumes raw input, whereas blank/error returns use PTK."""
        with self.session() as root:
            self.output.clear()
            self.send('read -r value; print -r -- "$value" > reply\r  native 한 글  \r')
            self.require(lambda: (root / "reply").exists() and self.primary())
            self.assertEqual((root / "reply").read_text(), "native 한 글\n")
            self.output.clear()
            self.send("\rprint empty >> result\r")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "empty\n")
            self.output.clear()
            self.send(")\rprint error >> result\r")
            self.require(
                lambda: (
                    (root / "result").read_text() == "empty\nerror\n" and self.primary()
                )
            )

    def test_multiline_paste_keeps_heredoc_and_following_commands(self):
        """Already edited paste remains shell text across several syntax permissions."""
        with self.session() as root:
            block = "cat > body <<'END'\n  probe 한 글  \nEND\nprint after > result"
            self.output.clear()
            self.send(PASTE_START + block.encode() + PASTE_END + b"\r")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "body").read_text(), "  probe 한 글  \n")
            self.assertEqual((root / "result").read_text(), "after\n")
            self.assertFalse((root / "tool-executed").exists())

    def test_continuation_cancel_and_eof_return_to_confirmed_primary(self):
        """Real PTK cancellation and EOF retain the shell's LI and parser behavior."""
        with self.session() as root:
            for control in (b"\x03", b"\x04"):
                with self.subTest(control=control):
                    self.submit("for value in A B; do")
                    self.require(self.editor)
                    self.assertFalse(self.snapshot(root)["commands"])
                    self.output.clear()
                    self.send(control)
                    self.require(self.primary)
                    self.assertTrue(self.snapshot(root)["commands"])
            self.submit("print intact > result")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "intact\n")

    def test_delayed_raw_permission_reclaims_typeahead_into_primary_ptk(self):
        """An actual native completion before R handling cannot consume future keys."""
        with self.session(delay_raw=True) as root:
            (root / "delay-raw").touch()
            self.output.clear()
            self.send(
                b"print completed > native-finished\r"
                + b"print saf >> result"
                + b"\x01"
                + b"\x06" * 9
                + b"e\r"
            )
            self.require(lambda: (root / "result").exists() and self.primary())
            current = self.snapshot(root)
            records = [
                json.loads(line)
                for line in (root / "snapshots").read_text().splitlines()
            ]
            self.assertEqual(len(records), 2)
            self.assertTrue(current["commands"])
            self.assertGreater(current["id"], records[0]["id"])
            self.assertEqual((root / "result").read_text(), "safe\n")

    def test_promptsubst_opt_out_retains_native_continuation_then_recovers(self):
        """A disabled capability preserves existing native input until explicitly restored."""
        with self.session() as root:
            self.submit("unsetopt promptsubst")
            self.require(self.primary)
            self.assertFalse(self.snapshot(root)["semantic"])
            self.output.clear()
            self.send("for value in A B; do\rprint $value >> result\rdone\r")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "A\nB\n")
            self.submit("setopt promptsubst")
            self.require(self.primary)
            self.assertTrue(self.snapshot(root)["semantic"])

    def test_loop_body_uses_the_shell_even_when_a_python_tool_has_the_same_name(self):
        """A continuation reply bypasses the Python dispatcher and runs as shell syntax."""
        with self.session(extra_rc="probe() { print shell >> result; }\n") as root:
            self.submit("for value in once; do")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit("probe")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit("done")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "shell\n")
            self.assertFalse((root / "tool-executed").exists())

    def test_python_input_retains_its_native_worker_pty(self):
        """Primary tool dispatch consumes immediate input in its existing Python PTY."""
        with self.session() as root:
            self.output.clear()
            self.send("inputtool\r  Python 한 글  \r")
            self.require(lambda: (root / "python-reply").exists() and self.primary())
            self.assertEqual((root / "python-reply").read_text(), "  Python 한 글  \n")
            self.assertTrue(self.snapshot(root)["commands"])

    def test_external_program_receives_fast_input_and_eof(self):
        """The external program receives typeahead and Ctrl+D through its native PTY."""
        with self.session() as root:
            self.output.clear()
            self.send("cat > result\r  external 한 글  \r".encode() + b"\x04")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "  external 한 글  \n")
            self.assertTrue(self.snapshot(root)["commands"])

    def test_raw_permission_overrides_stale_reader_hint_after_exec(self):
        """A same-process-group exec receives Ctrl+C and stdin despite delayed LR(false)."""
        with self.session(stale_reader=True) as root:
            (root / "shell-exec.py").write_text(SIGINT_READER)
            self.submit("if true; then")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit(f"exec {shlex.quote(sys.executable)} shell-exec.py")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit("fi")
            self.require(
                lambda: (
                    (root / "program-ready").exists()
                    and (root / "reader-stale").exists()
                )
            )
            stale = json.loads((root / "reader-stale").read_text())
            self.assertGreater(stale["ready_id"], 0)
            self.assertTrue(stale["continuation"])
            self.assertEqual(
                json.loads((root / "program-ready").read_text()),
                [stale["shell_pid"]] * 3,
            )
            self.send(b"\x03" + "  native 한 글  \n".encode())
            self.require(lambda: (root / "program-input").exists())
            self.assertEqual((root / "program-input").read_text(), "  native 한 글  \n")
            self.assertEqual(
                (root / "signal-received").read_text(), f"{signal.SIGINT}\n"
            )
            observed = None

            def exited():
                nonlocal observed
                observed = os.waitid(
                    os.P_PID, self.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                )
                return observed is not None

            self.require(exited)
            self.assertEqual((observed.si_code, observed.si_status), (os.CLD_EXITED, 0))

    def test_backslash_and_quoted_syntax_use_continuation_editing(self):
        """Lexical continuation preserves literal lines across separate PTK requests."""
        with self.session() as root:
            self.submit("print -r -- one \\")
            state = self.snapshot(root)
            self.assertTrue(state["commands"])
            self.assertEqual(state["text"], "print -r -- one \\\n")
            self.submit("two > result")
            self.require(lambda: (root / "result").exists() and self.primary())
            self.assertEqual((root / "result").read_text(), "one two\n")
            self.output.clear()
            self.send(PASTE_START + b'print -r -- "first' + PASTE_END + b"\r")
            self.require(self.editor)
            self.assertFalse(self.snapshot(root)["commands"])
            self.submit('probe" >> result')
            self.require(
                lambda: (
                    (root / "result").read_text() == "one two\nfirst\nprobe\n"
                    and self.primary()
                )
            )
            self.assertFalse((root / "tool-executed").exists())

    def test_delayed_duplicate_and_old_li_acknowledgements_release_only_one_reader(
        self,
    ):
        """A FIFO-controlled delayed LI response preserves pending keys and request order."""
        with self.session(delay_ack=True) as root:
            (root / "delay-ack").touch()
            boundary = self.snapshot(root)["boundary"]
            for index in range(2):
                self.submit("for value in once; do")
                self.require(self.editor)
                self.assertFalse(self.snapshot(root)["commands"])
                (root / "ack-held").unlink(missing_ok=True)
                self.output.clear()
                self.send(b"\x03")
                self.require(lambda: (root / "ack-held").exists())
                self.send(f"print {index} >> result\r")
                os.write(self.ack_fd, b"release\n")
                expected = "".join(f"{value}\n" for value in range(index + 1))
                self.require(
                    lambda expected=expected: (
                        (root / "result").exists()
                        and (root / "result").read_text() == expected
                        and self.primary()
                    )
                )
                current = self.snapshot(root)
                self.assertTrue(current["commands"])
                self.assertEqual(current["boundary"], boundary + 2)
                boundary = current["boundary"]


if __name__ == "__main__":
    unittest.main()
