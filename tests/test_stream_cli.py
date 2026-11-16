"""Actual outer PTY, native tcsh and PTK-path checks; not host pixel tests."""

from __future__ import annotations

import codecs
import errno
import hashlib
import json
import os
import pty
import select
import shlex
import shutil
import signal
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

import pyte

from stream_fixture import RAW_OUTPUT, configure_shell, shell_executable
from stream_plugins import plugin_options
from stream_state import INPUT_BYTES, failure, snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/stream_fixture.py"
DEADLINE = 15
PROMPT = b"STREAM_PRIMARY> "
BAR = b"ish stream | native shell | F2 popup"
F2 = b"\x1bOQ"
PASTE_BYTES = 6 * 1024 * 1024
PRESSURE_BYTES = 2 * 1024 * 1024
WRITE_BYTES = 65536
READ_BYTES = 65536
SHORT_READ_BYTES = 32
CPR_QUERY = b"\x1b[6n"


class StreamProcess:
    """Drive the public CLI through its controlling terminal and observe output."""

    def __init__(
        self,
        directory: Path,
        *,
        demo=False,
        startup="",
        shell="tcsh",
        integration_dir=None,
        plugins=(),
        plugin_dir=None,
        library_dir=None,
        extra_args=(),
        read_bytes=READ_BYTES,
    ):
        """Start an isolated user configuration and a real PTY child process."""
        self.directory = directory
        self.demo = demo
        self.read_bytes = read_bytes
        self.output = bytearray()
        self.screen = pyte.Screen(80, 24)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.stream = pyte.Stream(self.screen)
        self.query_pending = b""
        self.status = None
        self.sent = bytearray()
        self.pty_end = None
        self.cleanup_started = False
        self.before_cleanup = None
        configure_shell(directory, shell, startup)
        argv = [
            sys.executable,
            "-m",
            "ish.stream",
            "--shell",
            shell_executable(shell),
            "--cwd",
            str(directory),
        ]
        argv.extend(
            plugin_options(directory, demo=demo, names=plugins, directory=plugin_dir)
        )
        if integration_dir is not None:
            argv.extend(("--integration-dir", str(integration_dir)))
        if library_dir is not None:
            argv.extend(("--plugin-lib-dir", str(library_dir)))
        argv.extend(extra_args)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            termios.tcsetwinsize(0, (24, 80))
            env = dict(os.environ)
            for name in ("ZDOTDIR", "BASH_ENV", "ENV"):
                env.pop(name, None)
            env.update(
                HOME=str(directory),
                TERM="xterm-256color",
                LANG="C.UTF-8",
                PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
            )
            os.execvpe(argv[0], argv, env)
        os.set_blocking(self.fd, False)

    def read(self, timeout=0.1):
        """Read observable output and answer only the test host's CPR requests."""
        if not select.select([self.fd], [], [], timeout)[0]:
            return b""
        try:
            data = os.read(self.fd, self.read_bytes)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            if self.pty_end is None:
                self.pty_end = {"kind": "EIO", "state": self.state()}
            return b""
        if not data and self.pty_end is None:
            self.pty_end = {"kind": "EOF", "state": self.state()}
        self.output.extend(data)
        self._display(data)
        return data

    def state(self):
        """Observe the CLI without reaping or altering the PTY's input state."""
        return snapshot(
            self.pid, self.fd, returncode=self.status, cleanup=self.cleanup_started
        )

    def diagnostic(self, reason):
        """Keep first PTY closure evidence separate from later fixture teardown."""
        return failure(
            reason, self.state(), self.output, self.sent, pty_end=self.pty_end
        )

    def _display(self, data: bytes):
        """Answer a split CPR at its exact stream position in the test host."""
        # The CLI is the only possible requester at these test boundaries.
        pending = self.query_pending + data
        self.query_pending = b""
        while pending:
            offset = pending.find(CPR_QUERY)
            if offset >= 0:
                self.stream.feed(self.decoder.decode(pending[:offset]))
                self.send(
                    f"\x1b[{self.screen.cursor.y + 1};{self.screen.cursor.x + 1}R".encode()
                )
                pending = pending[offset + len(CPR_QUERY) :]
                continue
            keep = next(
                (
                    size
                    for size in range(min(len(pending), len(CPR_QUERY) - 1), 0, -1)
                    if CPR_QUERY.startswith(pending[-size:])
                ),
                0,
            )
            if keep:
                self.stream.feed(self.decoder.decode(pending[:-keep]))
                self.query_pending = pending[-keep:]
            else:
                self.stream.feed(self.decoder.decode(pending))
            break

    def wait(self, predicate, description):
        """Use elapsed time solely to fail a test, never to infer readiness."""
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline:
            if predicate():
                return
            self.read()
        raise AssertionError(self.diagnostic(f"Timed out: {description}"))

    def expect(self, data: bytes, after=0):
        """Wait for a fixture byte marker, allowing a caller to reject older output."""
        self.wait(lambda: data in self.output[after:], repr(data))

    def expect_file(self, path: Path, content: bytes | str):
        """Wait for final content, not merely an opened but not yet written file."""
        expected = content.encode() if isinstance(content, str) else content

        def complete():
            try:
                return path.read_bytes() == expected
            except FileNotFoundError:
                return False

        self.wait(complete, f"{path.name}: {expected!r}")

    def send(self, data: bytes):
        """Write literal external input without using internal Application methods."""
        self.sent.extend(data[-INPUT_BYTES:])
        del self.sent[:-INPUT_BYTES]
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd, view) :]
            except BlockingIOError:
                select.select([], [self.fd], [], 1)
            except OSError as error:
                raise AssertionError(
                    self.diagnostic(f"PTY write failed: {error}")
                ) from error

    def command(self, text: str):
        """Submit one command and wait for the next actual shell prompt output."""
        offset = len(self.output)
        self.send(text.encode() + b"\r")
        self.expect(PROMPT, offset)
        if self.demo:
            self.expect(BAR, offset)
            self.wait(
                lambda: BAR.decode() in "\n".join(self.screen.display),
                "current primary panel after native command",
            )

    def fixture(self, kind: str, path: Path | None = None):
        """Run an independent native stdin/output/size fixture from the shell."""
        parts = [sys.executable, str(FIXTURE), kind]
        if path is not None:
            parts.append(str(path))
        offset = len(self.output)
        self.send(shlex.join(parts).encode() + b"\r")
        if kind != "output":
            self.expect(b"STREAM_NATIVE_READY", offset)
        return offset

    def resize(self, cols: int, rows: int):
        """Resize the real outer PTY, including its SIGWINCH notification."""
        self.screen.resize(rows, cols)
        termios.tcsetwinsize(self.fd, (rows, cols))

    def poll(self):
        """Observe and reap the real CLI exit without inventing an idle deadline."""
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def stop(self):
        """Close and reap only the CLI process created by this fixture."""
        if self.cleanup_started:
            return
        self.before_cleanup = self.state()
        self.cleanup_started = True
        os.close(self.fd)
        self.fd = None
        if self.status is not None:
            return
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
                return
            time.sleep(0.02)
        os.kill(self.pid, signal.SIGKILL)
        _, status = os.waitpid(self.pid, 0)
        self.status = os.waitstatus_to_exitcode(status)


@unittest.skipUnless(
    os.name == "posix" and shutil.which("tcsh"), "requires native tcsh"
)
class StreamCliTests(unittest.TestCase):
    """Validate actual input consumers and file results through the public CLI."""

    def setUp(self):
        """Create test-local startup configuration and observable result files."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-stream-cli-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def start(self, *, demo=False, startup="", extra_args=(), read_bytes=READ_BYTES):
        """Register cleanup before waiting for the native primary prompt."""
        process = StreamProcess(
            self.directory,
            demo=demo,
            startup=startup,
            extra_args=extra_args,
            read_bytes=read_bytes,
        )
        self.addCleanup(process.stop)
        process.expect(PROMPT)
        return process

    def test_default_output_is_exact_and_no_ui_is_added(self):
        """Image, clipboard and other escape strings survive byte for byte."""
        process = self.start()
        offset = process.fixture("output")
        process.expect(b"STREAM_RAW_DONE", offset)
        self.assertIn(RAW_OUTPUT, bytes(process.output[offset:]))
        self.assertNotIn(b"F2", bytes(process.output[:offset]))
        self.assertNotIn(b"\x1b[?1049h", bytes(process.output[:offset]))

    def test_empty_submissions_keep_natural_prompt_rows(self):
        """Empty Enter adds one adjacent native prompt without consuming extra history."""
        process = self.start(demo=True)
        bar = b"ish stream | native shell | F2 popup"
        process.expect(bar)
        prompt = PROMPT.decode().rstrip()
        for count in range(1, 5):
            with self.subTest(submissions=count):
                offset = len(process.output)
                process.send(b"\r")
                process.expect(PROMPT, offset)
                process.expect(bar, offset)
                rows = [
                    row
                    for row, line in enumerate(process.screen.display)
                    if line.rstrip() == prompt
                ]
                self.assertEqual(
                    len(rows), count + 1, "\n".join(process.screen.display)
                )
                self.assertEqual(rows, list(range(rows[0], rows[0] + count + 1)))

    def test_short_command_output_survives_prompt_return(self):
        """A short command's result remains visible across subsequent prompt returns."""
        process = self.start(demo=True)
        bar = b"ish stream | native shell | F2 popup"
        process.expect(bar)
        marker = "RETAIN_SHORT_COMMAND_OUTPUT"
        offset = len(process.output)
        process.command("echo " + marker)
        process.expect(bar, offset)
        for count in range(4):
            with self.subTest(empty_submissions=count):
                self.assertIn(
                    marker,
                    [line.rstrip() for line in process.screen.display],
                    "\n".join(process.screen.display),
                )
                if count != 3:
                    offset = len(process.output)
                    process.send(b"\r")
                    process.expect(PROMPT, offset)
                    process.expect(bar, offset)

    def test_popup_close_preserves_visible_history(self):
        """Closing a popup does not scroll away the existing native output above it."""
        startup = "".join(f"echo RETAIN_START_{number}\n" for number in range(8))
        process = self.start(demo=True, startup=startup)
        bar = b"ish stream | native shell | F2 popup"
        process.expect(bar)
        before = [
            line.rstrip()
            for line in process.screen.display
            if line.startswith("RETAIN_START_")
        ]
        self.assertTrue(before, "Startup fixture did not leave visible history")
        offset = len(process.output)
        process.send(F2)
        process.expect(b"Plugin popup", offset)
        offset = len(process.output)
        process.send(b"\x1b")
        process.expect(PROMPT, offset)
        process.expect(bar, offset)
        after = [
            line.rstrip()
            for line in process.screen.display
            if line.startswith("RETAIN_START_")
        ]
        self.assertEqual(after, before, "\n".join(process.screen.display))

    def test_native_continuation_heredoc_and_typeahead(self):
        """Rapid multiline input produces only the intended native file content."""
        for demo in (False, True):
            with self.subTest(demo=demo):
                child_dir = self.directory / str(demo)
                child_dir.mkdir()
                process = StreamProcess(child_dir, demo=demo)
                try:
                    process.expect(PROMPT)
                    result = child_dir / "result"
                    script = (
                        "foreach value (one two)\n"
                        "echo $value >> loop\n"
                        "end\n"
                        "cat > result << END\n"
                        "  spaced 한글\n"
                        "ish_exit\n"
                        "python\n"
                        "END\n"
                        "echo done > complete\n"
                    )
                    process.send(script.encode())
                    process.wait((child_dir / "complete").exists, "native paste")
                    self.assertEqual((child_dir / "loop").read_text(), "one\ntwo\n")
                    self.assertEqual(
                        result.read_text(), "  spaced 한글\nish_exit\npython\n"
                    )
                finally:
                    process.stop()

    def test_external_stdin_receives_popup_keys_while_running(self):
        """A program consumes F2 and control bytes even with the demo loaded."""
        process = self.start(demo=True)
        result = self.directory / "stdin"
        process.fixture("input", result)
        payload = b"native\x1bOQ\x1b[12~\x03\x04END"
        process.send(payload)
        process.expect_file(result, payload)
        self.assertEqual(result.read_bytes(), payload)

    def test_native_typeahead_immediately_after_enter(self):
        """F2 typed immediately after submission belongs to the native program."""
        process = self.start(demo=True)
        result = self.directory / "typeahead-input"
        command = shlex.join([sys.executable, str(FIXTURE), "input", str(result)])
        payload = b"fast\x1bOQEND"
        process.send(command.encode() + b"\r" + payload)
        process.expect_file(result, payload)
        self.assertEqual(result.read_bytes(), payload)

    def test_large_paste_waits_for_a_blocked_native_reader(self):
        """A gated native reader receives all six MiB after real writer backpressure."""
        process = self.start()
        payload = bytes(range(256)) * (PASTE_BYTES // 256)
        expected_hash = hashlib.sha256(payload).hexdigest()
        (self.directory / "input-config.json").write_text(
            json.dumps({"bytes": len(payload)})
        )
        offset = process.fixture("gated-input", self.directory)
        self.assertEqual((self.directory / "input-blocked").read_text(), "ready")
        self.assertFalse((self.directory / "read-gate").exists())
        pressure = threading.Event()
        finished = threading.Event()
        stopping = threading.Event()
        errors = []
        written = [0]

        def produce():
            view = memoryview(payload)
            try:
                while view and not stopping.is_set():
                    try:
                        size = os.write(process.fd, view[:WRITE_BYTES])
                    except BlockingIOError:
                        if written[0] >= PRESSURE_BYTES:
                            pressure.set()
                        select.select([], [process.fd], [], 0.05)
                    else:
                        written[0] += size
                        view = view[size:]
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        writer = threading.Thread(target=produce, daemon=True)

        def stop_writer():
            stopping.set()
            writer.join(timeout=2)
            self.assertFalse(writer.is_alive(), "Input producer did not stop")

        writer.start()
        self.addCleanup(stop_writer)
        process.wait(
            lambda: pressure.is_set() or finished.is_set(),
            "observed EAGAIN after two MiB while native stdin is gated",
        )
        self.assertFalse(errors, errors)
        self.assertTrue(pressure.is_set(), written)
        self.assertFalse(
            finished.is_set(), "Input was unexpectedly accepted without a reader"
        )
        self.assertFalse((self.directory / "native-input.bin").exists())
        # Only the observed blocked write opens the explicit consumer gate;
        # no quiet-output period or elapsed timer grants read permission.
        (self.directory / "read-gate").write_text("read")
        process.expect(b"STREAM_NATIVE_DONE", offset)
        process.wait(finished.is_set, "complete input producer")
        self.assertFalse(errors, errors)
        self.assertEqual(written[0], len(payload))
        result = self.directory / "native-input.bin"
        self.assertEqual(result.stat().st_size, len(payload))
        self.assertEqual(hashlib.sha256(result.read_bytes()).hexdigest(), expected_hash)
        self.assertEqual(
            json.loads((self.directory / "input-result.json").read_text()),
            {"bytes": len(payload), "sha256": expected_hash},
        )

    def test_dollar_read_and_interrupt_stay_native(self):
        """tcsh's stdin builtin and continuation cancellation keep their meaning."""
        process = self.start(demo=True)
        read_offset = len(process.output)
        process.send(b"echo $< > read-result\r")
        process.send("stdin 한글\r".encode())
        result = self.directory / "read-result"
        process.expect_file(result, "stdin 한글\n")
        self.assertEqual(result.read_text(), "stdin 한글\n")
        process.expect(PROMPT, read_offset)
        offset = len(process.output)
        process.send(b"foreach x (one two)\r")
        process.expect(b"STREAM_MORE", offset)
        cancel_offset = len(process.output)
        process.send(b"\x03")
        process.expect(PROMPT, cancel_offset)
        process.command("echo survived > interrupt-result")
        process.expect_file(self.directory / "interrupt-result", "survived\n")
        self.assertEqual(
            (self.directory / "interrupt-result").read_text(), "survived\n"
        )

    def test_resize_reaches_foreground_program(self):
        """The running program sees the full host dimensions without UI rows."""
        process = self.start(demo=True)
        result = self.directory / "sizes.json"
        process.fixture("size", result)
        process.wait(result.exists, "initial size")
        self.assertEqual(json.loads(result.read_text())[-1], [80, 24])
        process.resize(101, 31)
        process.wait(
            lambda: json.loads(result.read_text())[-1] == [101, 31], "resized child PTY"
        )
        process.send(b"q")
        process.expect(b"STREAM_NATIVE_DONE")

    def test_popup_input_is_not_a_shell_command(self):
        """The actual PTK popup consumes its text and Escape without executing it."""
        log = self.directory / "popup.log"
        process = self.start(
            demo=True,
            extra_args=("--lang", "en", "--log-file", str(log), "--log-level", "DEBUG"),
            read_bytes=SHORT_READ_BYTES,
        )
        process.expect(b"ish stream | native shell | F2 popup")
        offset = len(process.output)
        process.send(F2)
        process.expect(b"Plugin popup", offset)
        process.send(b"touch popup-leak")
        process.expect(b"popup-leak", offset)
        log_offset = len(log.read_text())
        process.send(b"\x1b")

        # Short reads leave the editing frame's bar unread when its text arrives.
        # That bar cannot prove Escape was handled. Observe focus release and the
        # subsequent native return before sending a distinct command, not Alt+e.
        def returned():
            events = log.read_text()[log_offset:]
            _, released, events = events.partition("Popup focus=False\n")
            _, primary, events = events.partition("Shell event kind=P serial=")
            return bool(
                released
                and primary
                and "Popup request stage=complete serial=" in events
            )

        process.wait(
            returned, "popup focus release and completed native primary return"
        )
        process.command("echo done > after-popup")
        process.expect_file(self.directory / "after-popup", "done\n")
        self.assertEqual((self.directory / "after-popup").read_text(), "done\n")
        self.assertFalse((self.directory / "popup-leak").exists())

    def test_popup_burst_is_consumed_before_native_editing_resumes(self):
        """One F2/text/Enter read cannot leak a popup command into the shell."""
        process = self.start(demo=True)
        process.expect(b"ish stream | native shell | F2 popup")
        offset = len(process.output)
        process.send(F2 + b"touch burst-leak\r")
        process.expect(PROMPT, offset)
        process.command("echo done > after-burst")
        process.expect_file(self.directory / "after-burst", "done\n")
        self.assertFalse((self.directory / "burst-leak").exists())

    def test_user_editor_command_keeps_native_stdin(self):
        """A native bindkey -c program must receive F2 and its following input."""
        process = self.start(demo=True)
        result = self.directory / "widget-input"
        command = shlex.join([sys.executable, str(FIXTURE), "input", str(result)])
        process.command("bindkey -c '^X' " + shlex.quote(command))
        offset = len(process.output)
        process.send(b"\x18")
        process.expect(b"STREAM_NATIVE_READY", offset)
        payload = F2 + b"END"
        process.send(payload)
        process.expect_file(result, payload)
        self.assertEqual(result.read_bytes(), payload)

    def test_input_after_popup_enter_waits_for_native_return(self):
        """Bytes after popup acceptance survive the native return handshake once."""
        process = self.start(demo=True)
        process.expect(b"ish stream | native shell | F2 popup")
        process.send(
            F2 + b"touch popup-accepted-leak\r" + b"echo after >> popup-remainder\r"
        )
        result = self.directory / "popup-remainder"
        process.expect_file(result, "after\n")
        process.command("echo complete > remainder-complete")
        process.expect_file(self.directory / "remainder-complete", "complete\n")
        self.assertEqual(result.read_text(), "after\n")
        self.assertFalse((self.directory / "popup-accepted-leak").exists())

    def test_ctrl_d_exits_the_native_shell(self):
        """EOF at an empty native primary prompt exits normally with the demo enabled."""
        process = self.start(demo=True)
        process.send(b"\x04")
        process.wait(lambda: process.poll() is not None, "native Ctrl+D exit")
        self.assertEqual(process.status, 0)

    def test_shell_exit_during_popup_does_not_wait_for_user_input(self):
        """A dead shell revokes popup focus and completes without a manual close."""
        for shell in ("tcsh", "bash", "zsh"):
            with self.subTest(shell=shell):
                directory = self.directory / shell
                directory.mkdir()
                process = StreamProcess(
                    directory, demo=True, shell=shell, startup="echo $$ > shell-pid"
                )
                try:
                    process.expect(BAR)
                    process.send(F2)
                    process.expect(b"Plugin popup")
                    shell_pid = int((directory / "shell-pid").read_text())
                    os.kill(shell_pid, signal.SIGKILL)
                    process.wait(
                        lambda process=process: process.poll() is not None,
                        "shell exit while popup owns input",
                    )
                    self.assertEqual(process.status, 128 + signal.SIGKILL)
                    self.assertNotIn(b"Traceback", process.output)
                finally:
                    process.stop()


if __name__ == "__main__":
    unittest.main()
