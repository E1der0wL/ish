"""Observe native shell consumers; these tests do not grant PTK ownership.

Prompt strings and deadlines are only test witnesses. No production readiness
policy is implemented here. Each scenario checks resulting files independently
of the application's protocol validator.
"""

import contextlib
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
import unittest
from pathlib import Path

PRIMARY = b"__ISH_OBS_PRIMARY__> "
CONTINUATION = b"__ISH_OBS_CONTINUATION__> "
RAW = b"__ISH_OBS_RAW__\r\n"
DEADLINE = 8


class NativeShell:
    """Run an unmodified shell under its own controlling PTY."""

    def __init__(self, executable, root, kind):
        """Start with isolated startup state and test-only prompt witnesses."""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            os.execve(
                executable,
                [executable, "-i"] if kind == "dash" else [executable, "-f"],
                {
                    "HOME": str(root),
                    "TERM": "xterm",
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
            )
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        self.output = bytearray()
        self.continuation = b"? " if kind == "csh" else CONTINUATION
        if kind == "dash":
            self.send(f"PS1='{PRIMARY.decode()}'; PS2='{CONTINUATION.decode()}'\n")
        else:
            command = f"set prompt = '{PRIMARY.decode()}'"
            if kind == "tcsh":
                command += f"; set prompt2 = '{CONTINUATION.decode()}'"
                command += "; alias postcmd 'echo POST >> events'"
            self.send(command + "\n")
        try:
            self.require(lambda: self.output.endswith(PRIMARY))
        except BaseException:
            self.close()
            raise
        self.output.clear()

    def send(self, data):
        """Write exact test bytes, preserving their order."""
        data = data.encode() if isinstance(data, str) else data
        while data:
            data = data[os.write(self.fd, data) :]

    def receive(self):
        """Collect currently arriving output without using silence as readiness."""
        if select.select([self.fd], [], [], 0.02)[0]:
            try:
                self.output.extend(os.read(self.fd, 65536))
            except OSError:
                pass

    def require(self, condition):
        """Fail at a deadline; the predicate itself supplies the test witness."""
        deadline = time.monotonic() + DEADLINE
        while not condition() and time.monotonic() < deadline:
            self.receive()
        if not condition():
            raise AssertionError(repr(bytes(self.output[-3500:])))

    def close(self):
        """Reap the isolated native shell without affecting any user shell."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)
        os.close(self.fd)


class HandoffBoundaryTests(unittest.TestCase):
    """Locate consumer changes using actual execution and final file contents."""

    @contextlib.contextmanager
    def session(self, kind):
        """Select explicit BSD csh separately from a csh symlink to tcsh."""
        executable = os.environ.get("ISH_TEST_" + kind.upper()) or shutil.which(
            "bsd-csh" if kind == "csh" else kind
        )
        if not executable:
            self.skipTest(kind + " is not installed; set ISH_TEST_" + kind.upper())
        with tempfile.TemporaryDirectory(prefix="ish-native-boundaries-") as directory:
            root = Path(directory)
            shell = NativeShell(executable, root, kind)
            try:
                yield root, shell
            finally:
                shell.close()

    def test_command_substitution_reads_stdin_before_loop_syntax(self):
        """One foreach submission consumes raw stdin and then requests loop syntax."""
        for kind in ("tcsh", "csh"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                (root / "read.sh").write_text(
                    "printf '__ISH_OBS_RAW__\\n' >&2\n"
                    "IFS= read -r answer\n"
                    "printf '%s\\n' \"$answer\" > raw-input\n"
                    "printf '%s\\n' \"$answer\"\n"
                )
                shell.send("foreach value (`/bin/sh read.sh`)\n")
                shell.require(lambda: RAW in shell.output)
                self.assertNotIn(CONTINUATION, shell.output)
                shell.output.clear()
                shell.send("first second\n")
                shell.require(lambda: shell.output.endswith(shell.continuation))
                self.assertEqual((root / "raw-input").read_bytes(), b"first second\n")
                shell.output.clear()
                shell.send("echo $value >> loop-output\nend\n")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertEqual(
                    (root / "loop-output").read_bytes(), b"first\nsecond\n"
                )

    def test_dollar_input_precedes_additional_syntax(self):
        """The shell's own dollar-less-than input also precedes foreach collection."""
        with self.session("tcsh") as (root, shell):
            (root / "events").write_text("")
            shell.send("foreach value ( $< )\n")
            shell.require(lambda: (root / "events").read_text() == "POST\n")
            shell.send("native-value\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send("echo $value > answer\nend\n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertEqual((root / "answer").read_bytes(), b"native-value\n")

    def test_source_syntax_stays_in_file_while_stdin_stays_native(self):
        """A sourced loop needs no user syntax; dollar-less-than still consumes the TTY."""
        for kind in ("tcsh", "csh"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                (root / "source.csh").write_text(
                    "printf '__ISH_OBS_RAW__\\n'\n"
                    'set answer = "$<"\n'
                    'echo "$answer" > source-input\n'
                    "foreach value ( first second )\n"
                    "echo $value >> source-loop\nend\n"
                )
                shell.send("source source.csh\n")
                shell.require(lambda: RAW in shell.output)
                shell.output.clear()
                shell.send("  native source input  \n")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertNotIn(CONTINUATION, shell.output)
                self.assertEqual(
                    (root / "source-input").read_bytes(), b"  native source input  \n"
                )
                self.assertEqual(
                    (root / "source-loop").read_bytes(), b"first\nsecond\n"
                )

    def test_heredoc_keeps_spaces_unicode_and_tool_names_literal(self):
        """Observe the shell, rather than an ish dispatcher, consuming every data line."""
        for kind in ("tcsh", "csh", "dash"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                payload = "  한 글 🙂  \nish_exit\nprobe\n"
                if kind == "csh":
                    # BSD csh has no heredoc prompt; a completed file is the
                    # witness. Absence of output is never treated as readiness.
                    shell.send("cat << END > answer\n" + payload + "END\n")
                else:
                    shell.send("cat << END > answer\n")
                    shell.require(lambda: shell.output.endswith(shell.continuation))
                    shell.output.clear()
                    shell.send(payload + "END\n")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertEqual((root / "answer").read_bytes(), payload.encode())
                if kind == "csh":
                    self.assertNotIn(b"? ", shell.output)

    def test_completed_command_marker_does_not_reserve_input(self):
        """Input delivered after a completed raw command is consumed by the next parser."""
        for kind in ("tcsh", "csh", "dash"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                shell.send("printf '__ISH_OBS_RAW__\\n'\n")
                shell.require(
                    lambda: RAW in shell.output and shell.output.endswith(PRIMARY)
                )
                shell.output.clear()
                shell.send("echo late > answer\n")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertEqual((root / "answer").read_bytes(), b"late\n")

    def test_backslash_and_nested_loop_consumers(self):
        """Lexical continuation and loop prereading preserve their final command text."""
        with self.session("tcsh") as (root, shell):
            shell.send("printf '%s\\n' first \\\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send("second > joined\n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertEqual((root / "joined").read_bytes(), b"first\nsecond\n")
            shell.output.clear()
            shell.send("set i = 0\n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            shell.output.clear()
            shell.send("while ( $i < 1 )\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send(
                "foreach value ( A B )\necho $value >> nested\nend\n@ i++\nend\n"
            )
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertEqual((root / "nested").read_bytes(), b"A\nB\n")

    def test_if_and_switch_search_more_syntax(self):
        """Conditional search paths consume syntax and execute only selected branches."""
        with self.session("tcsh") as (root, shell):
            shell.send("if ( 0 ) then\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send(
                "echo wrong > conditional\nelse\necho correct > conditional\nendif\n"
            )
            shell.require(
                lambda: (
                    (root / "conditional").exists() and shell.output.endswith(PRIMARY)
                )
            )
            self.assertEqual((root / "conditional").read_bytes(), b"correct\n")
            shell.output.clear()
            shell.send("switch ( choice )\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send(
                "case choice:\necho selected > switch-result\nbreaksw\ndefault:\necho wrong > switch-result\nendsw\n"
            )
            shell.require(
                lambda: (
                    (root / "switch-result").exists() and shell.output.endswith(PRIMARY)
                )
            )
            self.assertEqual((root / "switch-result").read_bytes(), b"selected\n")

    def test_eval_keeps_syntax_in_memory_and_reads_native_stdin(self):
        """eval syntax comes from its argument while its dollar input uses the TTY."""
        with self.session("tcsh") as (root, shell):
            shell.send(
                'eval \'printf "__ISH_OBS_RAW__\\n"; set value = "$<"; echo "$value" > eval-input\'\n'
            )
            shell.require(lambda: RAW in shell.output)
            shell.output.clear()
            shell.send("  eval input  \n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertEqual((root / "eval-input").read_bytes(), b"  eval input  \n")
            self.assertNotIn(CONTINUATION, shell.output)

    def test_true_if_body_reuses_the_primary_prompt(self):
        """The displayed primary prompt does not describe the enclosing conditional."""
        with self.session("tcsh") as (root, shell):
            shell.send("if ( 1 ) then\n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertNotIn(CONTINUATION, shell.output)
            shell.output.clear()
            shell.send("echo selected > true-if\nendif\n")
            shell.require(
                lambda: (root / "true-if").exists() and shell.output.endswith(PRIMARY)
            )
            self.assertEqual((root / "true-if").read_bytes(), b"selected\n")

    def test_external_stdin_stays_with_the_program(self):
        """Foreground stdin preserves whitespace and Unicode for all candidate shells."""
        for kind in ("tcsh", "csh", "dash"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                (root / "read.sh").write_text(
                    "printf '__ISH_OBS_RAW__\\n'\n"
                    "IFS= read -r value\n"
                    "printf '%s\\n' \"$value\" > program-input\n"
                )
                shell.send("/bin/sh read.sh\n")
                shell.require(lambda: RAW in shell.output)
                shell.output.clear()
                shell.send("  한 글 native  \n")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertEqual(
                    (root / "program-input").read_bytes(), "  한 글 native  \n".encode()
                )

    def test_blank_error_and_interrupt_return_to_a_fresh_command(self):
        """Blank input, syntax errors, and Ctrl+C do not consume the following command."""
        with self.session("tcsh") as (root, shell):
            for command in ("\n", 'echo "unterminated\n'):
                shell.send(command)
                shell.require(lambda: shell.output.endswith(PRIMARY))
                shell.output.clear()
            shell.send("foreach value ( A B )\n")
            shell.require(lambda: shell.output.endswith(CONTINUATION))
            shell.output.clear()
            shell.send(b"\x03")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            shell.output.clear()
            shell.send("echo after > answer\n")
            shell.require(lambda: shell.output.endswith(PRIMARY))
            self.assertEqual((root / "answer").read_bytes(), b"after\n")

    def test_heredoc_eof_returns_to_primary(self):
        """Ctrl+D ends an unfinished heredoc without treating its payload as commands."""
        for kind in ("tcsh", "dash"):
            with self.subTest(kind=kind), self.session(kind) as (root, shell):
                shell.send("cat << END > answer\n")
                shell.require(lambda: shell.output.endswith(CONTINUATION))
                shell.output.clear()
                shell.send("probe\n")
                shell.require(lambda: shell.output.endswith(CONTINUATION))
                shell.output.clear()
                shell.send(b"\x04")
                shell.require(lambda: shell.output.endswith(PRIMARY))
                self.assertEqual((root / "answer").read_bytes(), b"probe\n")


if __name__ == "__main__":
    unittest.main()
