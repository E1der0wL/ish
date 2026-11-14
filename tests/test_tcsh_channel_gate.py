"""Test a separate, opt-in syntax reader gate; never exercise a live ish adapter.

The shell has a real controlling PTY. PTK uses its real key processor with pipe
input and dummy rendering; this is not an ish CLI ownership/dispatcher test.
The controller explicitly chooses syntax versus stdin. Automatic typeahead
classification and production reclaim are deliberately not implemented here.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import termios
import tty
import unittest
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from tcsh_channel import NativeChannel

DEADLINE = 8
CHUNK_LIMIT = 256
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
GATE_SOURCE = Path(__file__).with_name("channel_gate.c")


def receive_line(connection):
    """Receive one protocol witness; deadlines only fail the experiment."""
    data = bytearray()
    while not data.endswith(b"\n"):
        piece = connection.recv(1)
        if not piece:
            raise EOFError(bytes(data))
        data.extend(piece)
    return bytes(data)


@dataclass
class ReadRequest:
    """Identify a blocked syscall, independently of logical shell submission."""

    connection: socket.socket
    token: str
    inode: int
    serial: int
    capacity: int

    def frame(self, length, *, token=None, inode=None, serial=None):
        """Encode an answer with optional independent invalid test identities."""
        values = (
            self.token if token is None else token,
            self.inode if inode is None else inode,
            self.serial if serial is None else serial,
            length,
        )
        return "A {} {} {} {}\n".format(*values).encode()

    def exchange(self, frame):
        """Observe explicit acceptance/rejection without waiting for silence."""
        self.connection.sendall(frame)
        return receive_line(self.connection)

    def close(self):
        """Release the controller side of this request."""
        self.connection.close()


class Gate:
    """Serve the test-only shim using a private socket and registered FIFO."""

    def __init__(self, root, library):
        """Create a fresh session identity before the unmodified shell starts."""
        self.token = secrets.token_hex(16)
        self.fifo = root / "syntax.fifo"
        self.socket_path = root / "gate.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen()
        self.listener.settimeout(DEADLINE)
        self.environment = {
            "LD_PRELOAD": str(library),
            "ISH_TEST_FIFO": str(self.fifo),
            "ISH_TEST_SOCKET": str(self.socket_path),
            "ISH_TEST_TOKEN": self.token,
        }
        self.requests = []

    def accept(self):
        """Observe the native reader already blocked on the control channel."""
        connection, _ = self.listener.accept()
        connection.settimeout(DEADLINE)
        fields = receive_line(connection).decode().split()
        if len(fields) != 5 or fields[:2] != ["Q", self.token]:
            connection.close()
            raise AssertionError(fields)
        request = ReadRequest(connection, fields[1], *map(int, fields[2:]))
        self.requests.append(request)
        return request

    def reply(self, shell, request, data):
        """Stage a bounded chunk, then grant exactly that blocked read."""
        if len(data) > min(request.capacity, CHUNK_LIMIT):
            raise ValueError("A test response must fit one bounded read chunk")
        if data:
            shell.write_syntax(data)
        answer = request.exchange(request.frame(len(data)))
        if answer != b"G\n":
            raise AssertionError(answer)
        request.close()

    def line(self, shell, request, text):
        """Serve one literal line; partial reads use credits, not extra editors."""
        pending = text.encode() if isinstance(text, str) else text
        if not pending.endswith(b"\n") or b"\n" in pending[:-1]:
            raise ValueError("Exactly one complete physical line is required")
        while pending:
            count = min(len(pending), request.capacity, CHUNK_LIMIT)
            self.reply(shell, request, pending[:count])
            pending = pending[count:]
            request = self.accept()
        return request

    def close(self):
        """Close all test connections after the shell has been reaped."""
        for request in self.requests:
            request.close()
        self.listener.close()


async def edit(keys):
    """Run actual PTK editing/key bindings and return its un-dispatched text."""
    with create_pipe_input() as keyboard:
        prompt = PromptSession(input=keyboard, output=DummyOutput())
        active = asyncio.Event()
        task = asyncio.create_task(prompt.prompt_async("> ", pre_run=active.set))
        await asyncio.wait_for(active.wait(), DEADLINE)
        keyboard.send_text(keys)
        return await asyncio.wait_for(task, DEADLINE)


@unittest.skipUnless(
    shutil.which("tcsh") and shutil.which("cc"), "tcsh and cc required"
)
class ChannelGateTests(unittest.TestCase):
    """Check native gating and real PTK values with independent file outcomes."""

    @classmethod
    def setUpClass(cls):
        """Compile only the experiment shim into an isolated temporary folder."""
        cls.build = tempfile.TemporaryDirectory(prefix="ish-channel-build-")
        cls.library = Path(cls.build.name) / "gate.so"
        subprocess.run(
            [
                "cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-shared",
                "-fPIC",
                str(GATE_SOURCE),
                "-o",
                str(cls.library),
                "-ldl",
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        """Remove the isolated native build after all child processes exit."""
        cls.build.cleanup()

    @contextlib.contextmanager
    def session(self):
        """Keep stdin on the controlling PTY while source reads the gated FIFO."""
        with tempfile.TemporaryDirectory(prefix="ish-channel-gate-") as directory:
            root = Path(directory)
            gate = Gate(root, self.library)
            shell = NativeChannel(shutil.which("tcsh"), root, env=gate.environment)
            try:
                shell.start_source()
                yield root, shell, gate, gate.accept()
            finally:
                shell.close()
                gate.close()

    def test_gate_rejects_foreign_stale_and_duplicate_responses(self):
        """Invalid wire replies never release staged commands into the parser."""
        with self.session() as (root, shell, gate, request):
            command = b"echo first >> results\n"
            shell.write_syntax(command)
            for fields in (
                {"token": "foreign"},
                {"inode": request.inode + 1},
                {"serial": request.serial + 1},
                {"serial": 0},
            ):
                self.assertEqual(
                    request.exchange(request.frame(len(command), **fields)), b"N\n"
                )
                self.assertFalse((root / "results").exists())
            accepted = request.frame(len(command))
            self.assertEqual(request.exchange(accepted), b"G\n")
            request.close()
            following = gate.accept()
            self.assertEqual((root / "results").read_bytes(), b"first\n")
            self.assertGreater(following.serial, request.serial)
            self.assertEqual(following.exchange(accepted), b"N\n")
            following = gate.line(shell, following, "echo second >> results\n")
            self.assertEqual((root / "results").read_bytes(), b"first\nsecond\n")

    def test_ptk_multiline_paste_and_unicode_heredoc(self):
        """PTK edits literal body lines; the shell file preserves their exact bytes."""
        with self.session() as (root, shell, gate, request):
            text = asyncio.run(edit("cat << END > literalX\x7f\r"))
            self.assertEqual(text, "cat << END > literal")
            request = gate.line(shell, request, text + "\n")
            body = "  한 글 🙂  \nish_exit\nprobe\n" + "가" * 700 + "\nEND"
            text = asyncio.run(edit(PASTE_START + body + PASTE_END + "\r"))
            self.assertEqual(text, body)
            for line in (text + "\n").splitlines(keepends=True):
                request = gate.line(shell, request, line)
            self.assertEqual(
                (root / "literal").read_bytes(), (body.removesuffix("END")).encode()
            )
            self.assertGreater(len(gate.requests), 8)

    def test_raw_stdin_then_gated_syntax_in_same_foreach(self):
        """The PTY supplies dollar-less-than data, then PTK supplies loop syntax."""
        with self.session() as (root, shell, gate, request):
            gate.reply(shell, request, b"foreach value ( $< )\n")
            shell.write_stdin("first second\n")
            request = gate.accept()
            text = asyncio.run(edit("echo $value >> values\r"))
            request = gate.line(shell, request, text + "\n")
            # FIFO/source mode executes its first iteration before 'end'.
            self.assertEqual((root / "values").read_bytes(), b"first\n")
            request = gate.line(shell, request, "end\n")
            self.assertEqual((root / "values").read_bytes(), b"first\nsecond\n")

    def test_external_python_input_keeps_native_pty(self):
        """An exec child uses ordinary stdin and never participates in the gate."""
        with self.session() as (root, shell, gate, request):
            (root / "reader.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "assert os.isatty(0)\n"
                "assert 'LD_PRELOAD' not in os.environ\n"
                "Path('native').write_text(input())\n"
            )
            gate.reply(shell, request, b"/usr/bin/python3 reader.py\n")
            shell.write_stdin("  네이티브 🙂  \n")
            request = gate.accept()
            self.assertEqual((root / "native").read_text(), "  네이티브 🙂  ")
            request = gate.line(shell, request, "echo syntax > syntax-result\n")
            self.assertEqual((root / "syntax-result").read_bytes(), b"syntax\n")

    def test_late_pty_input_cannot_enter_fifo_parser_but_remains_queued(self):
        """A syntax gate isolates queued native bytes; it does not reclaim them."""
        with self.session() as (root, shell, gate, request):
            request = gate.line(shell, request, "echo complete > completed\n")
            self.assertEqual((root / "completed").read_bytes(), b"complete\n")
            shell.write_stdin("echo wrong > escaped\n")
            request = gate.line(shell, request, "echo syntax > safe\n")
            self.assertFalse((root / "escaped").exists())
            self.assertEqual((root / "safe").read_bytes(), b"syntax\n")
            # The same bytes are still native stdin. They are not automatically
            # PTK typeahead merely because a syntax request was received.
            request = gate.line(shell, request, 'set answer = "$<"\n')
            request = gate.line(shell, request, 'echo "$answer" > consumed\n')
            self.assertEqual(
                (root / "consumed").read_bytes(), b"echo wrong > escaped\n"
            )
            self.assertFalse((root / "escaped").exists())

    def test_eof_exposes_unprotected_return_to_original_pty_parser(self):
        """Reproduce why FIFO EOF alone cannot grant safe primary editing."""
        with self.session() as (root, shell, gate, request):
            shell.write_stdin("echo leaked > escaped\n")
            gate.reply(shell, request, b"")
            shell.require(lambda: (root / "escaped").exists())
            self.assertEqual((root / "escaped").read_bytes(), b"leaked\n")

    def test_ptk_eof_returns_source_without_closing_shell_stdin(self):
        """Map an empty PTK Ctrl+D to source EOF; subsequent PTY input still works."""
        with self.session() as (root, shell, gate, request):
            with self.assertRaises(EOFError):
                asyncio.run(edit("\x04"))
            gate.reply(shell, request, b"")
            shell.write_stdin("echo alive > alive\n")
            shell.require(lambda: (root / "alive").exists())
            self.assertEqual((root / "alive").read_bytes(), b"alive\n")

    def test_reclaim_under_a_blocked_reader_replays_bytes_in_ptk(self):
        """With our sole writer stopped, reclaim a native suffix before editing."""
        with self.session() as (root, shell, gate, request):
            request = gate.line(shell, request, "echo finished > finished\n")
            pending = "echo 회수 > reclaimed"
            shell.write_stdin(pending)
            # Test-only access to this isolated child's known slave terminal.
            # Q proves the syntax reader is stopped; this controller owns the
            # only writer, and no other job/reader is alive in this scenario.
            descriptor = os.open(
                shell.tty_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY
            )
            saved = termios.tcgetattr(descriptor)
            recovered = bytearray()
            try:
                tty.setraw(descriptor, when=termios.TCSANOW)
                while True:
                    try:
                        chunk = os.read(descriptor, 4096)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    recovered.extend(chunk)
            finally:
                termios.tcsetattr(descriptor, termios.TCSANOW, saved)
                os.close(descriptor)
            self.assertEqual(bytes(recovered), pending.encode())
            text = asyncio.run(edit(recovered.decode() + "\r"))
            self.assertEqual(text, pending)
            request = gate.line(shell, request, text + "\n")
            self.assertEqual((root / "reclaimed").read_bytes(), "회수\n".encode())

    def test_cancel_retires_staged_fifo_and_rejects_old_ack(self):
        """Abort before grant, verify source unwind, then use a fresh FIFO identity."""
        with self.session() as (root, shell, gate, request):
            staged = b"echo cancelled > cancelled\n"
            shell.write_syntax(staged)
            cancelled_ack = request.frame(len(staged))
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(edit("unfinished\x03"))
            # This experimental cancellation explicitly aborts the control
            # connection and retires the writer as well as delivering VINTR.
            # It does NOT claim that a bare Ctrl+C always unwinds source FIFO.
            shell.write_stdin(b"\x03")
            request.close()
            shell.close_syntax()
            # Closing a control connection does NOT finish source: stock tcsh
            # retries the syntax read. Observe that request, reject the cancelled
            # ACK, and explicitly terminate this stream before recovery.
            retry = gate.accept()
            self.assertGreater(retry.serial, request.serial)
            self.assertEqual(retry.exchange(cancelled_ack), b"N\n")
            gate.reply(shell, retry, b"")
            shell.write_stdin("echo recovered > recovery\n")
            shell.require(lambda: (root / "recovery").exists())
            self.assertEqual((root / "recovery").read_bytes(), b"recovered\n")
            self.assertFalse((root / "cancelled").exists())
            gate.fifo.unlink()
            shell.start_source(name="syntax.fifo")
            following = gate.accept()
            self.assertGreater(following.serial, request.serial)
            self.assertEqual(following.exchange(cancelled_ack), b"N\n")
            following = gate.line(shell, following, "echo fresh > fresh\n")
            self.assertEqual((root / "fresh").read_bytes(), b"fresh\n")
            self.assertFalse((root / "cancelled").exists())


if __name__ == "__main__":
    unittest.main()
