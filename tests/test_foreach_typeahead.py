"""Compare real csh/tcsh with ish while a foreach substitution is blocked."""

import contextlib
import fcntl
import os
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

import psutil

from ish.shell.sequencer import Sequencer

# Cold imports and the helper C build are slower on WSL /mnt drives. Keep this
# separate from command-response deadlines so runtime stalls still fail quickly.
STARTUP_TIMEOUT = 30


class Terminal:
    """Integration-test terminal communicating with an isolated PTY child.

    This is not a screen emulator; CPR requests receive the fixed coordinates 1;1.
    """

    def __init__(self, argv, directory, env):
        """Start a PTY child and prepare a fixed window size and output buffer."""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(directory)
            os.execve(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        self.output = bytearray()

    def send(self, text):
        """Send string or byte input to the test PTY child."""
        os.write(self.fd, text.encode() if isinstance(text, str) else text)

    def until(self, predicate, timeout=6):
        """Read PTY output until the predicate succeeds or the timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            if not select.select([self.fd], [], [], 0.02)[0]:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.output.extend(chunk)
            if b"\x1b[6n" in chunk:
                self.send(b"\x1b[1;1R")
        return predicate()

    def close(self):
        """Terminate and reap the test child and its descendants, then close the PTY."""
        try:
            process = psutil.Process(self.pid)
            descendants = process.children(recursive=True)
            for child in reversed(descendants):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
        except psutil.NoSuchProcess:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)


class ForeachTypeaheadTests(unittest.TestCase):
    """Compare typeahead during delayed command substitution with native C shell behavior."""

    def exercise(self, shell, ish, scenario):
        """Run the shell scenario with an isolated rc and PTY and observe its results."""
        path = os.environ.get("ISH_TEST_" + shell.upper()) or shutil.which(shell)
        if not path:
            self.skipTest(shell + " not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ish-foreach-") as directory:
            scratch = Path(directory)
            # The substitution has a deterministic barrier: no secondary prompt
            # can appear until the test has submitted its typeahead.
            (scratch / "slow.sh").write_text(
                "printf 'SUBSTITUTION_STARTED\\n' >&2\n"
                "while [ ! -f release ]; do sleep .01; done\nprintf 'one two\\n'\n"
            )
            rc = "set prompt = 'READY> '\n"
            if shell == "tcsh":
                rc += "set prompt2 = 'LOOP> '\n"
                if scenario == "interrupt":
                    rc += "set history = 100\n"
                if scenario == "complete_no_edit":
                    rc += "unset edit\n"
            (scratch / (".tcshrc" if shell == "tcsh" else ".cshrc")).write_text(rc)
            env = {
                "HOME": directory,
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "XDG_DATA_HOME": str(scratch / "data"),
                "PYTHONPATH": str(root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            argv = (
                [sys.executable, "-B", "-m", "ish.main", path] if ish else [path, "-i"]
            )
            terminal = Terminal(argv, directory, env)

            def require(predicate, label, timeout=6):
                """Require the predicate within a deadline and include recent output on
                failure.
                """
                self.assertTrue(
                    terminal.until(predicate, timeout),
                    f"{shell} ish={ish} {scenario}: {label}; {bytes(terminal.output[-2500:])!r}",
                )

            def ready():
                """Check that the primary prompt and required editor signals have been
                emitted.
                """
                if ish:
                    return (
                        b"\x1b[?2004h" in terminal.output
                        and b"READY>" in terminal.output
                    )
                return terminal.output.endswith(b"READY> ")

            try:
                require(ready, "initial prompt", timeout=STARTUP_TIMEOUT)
                if scenario == "changed_hooks":
                    terminal.output.clear()
                    change = "alias precmd \"set prompt='READY> '\"; alias postcmd 'echo hooked >> hooks'"
                    terminal.send(
                        b"\x1b[200~" + change.encode() + b"\x1b[201~\r"
                        if ish
                        else change + "\r"
                    )
                    require(ready, "prompt after replacing both hooks")
                terminal.output.clear()
                header = "foreach x (`/bin/sh slow.sh`)"
                body = 'echo "$x" >> result'
                submitted = header + (
                    "\n" + body + "\nend" if scenario == "paste" else ""
                )
                if ish:
                    terminal.send(b"\x1b[200~" + submitted.encode() + b"\x1b[201~\r")
                else:
                    terminal.send(submitted.replace("\n", "\r") + "\r")
                require(
                    lambda: b"SUBSTITUTION_STARTED\r\n" in terminal.output,
                    "substitution starts",
                )
                if ish:
                    self.assertNotIn(b"_ish_restore_editor;", terminal.output)
                terminal.output.clear()
                if scenario == "interrupt":
                    terminal.send(b"\x03")
                    require(ready, "main prompt after Ctrl+C")
                    terminal.output.clear()
                    terminal.send("echo done >> after\r")
                    after = scratch / "after"
                    require(
                        lambda: after.exists() and after.read_text() == "done\n",
                        "command after Ctrl+C",
                    )
                    if shell == "tcsh":
                        require(ready, "prompt after recovery command")
                        terminal.output.clear()
                        terminal.send("history -h > saved-history\r")
                        saved = scratch / "saved-history"
                        require(
                            lambda: saved.exists() and ready(), "history after Ctrl+C"
                        )
                        self.assertNotIn("_ish_restore_editor", saved.read_text())
                        self.assertIn("echo done >> after\n", saved.read_text())
                    return not (scratch / "result").exists(), after.read_text()
                if scenario in ("complete", "complete_no_edit", "changed_hooks"):
                    terminal.send(body + "\rend\r")
                elif scenario == "partial":
                    terminal.send('echo "$x" >> res')
                # Make the bytes available in the PTY before releasing the child.
                terminal.until(lambda: False, 0.1)
                (scratch / "release").touch()
                if scenario in ("partial", "wait"):
                    prompt = b"LOOP> " if shell == "tcsh" else b"? "
                    require(lambda: prompt in terminal.output, "secondary prompt")
                    # Longer than the old automatic-recovery threshold.
                    terminal.until(lambda: False, 1.5)
                    terminal.send(
                        ("ult" if scenario == "partial" else body) + "\rend\r"
                    )
                result = scratch / "result"
                require(
                    lambda: result.exists() and result.read_text() == "one\ntwo\n",
                    "loop runs exactly twice",
                )
                require(ready, "main prompt returns")
                self.last_loop_output = bytes(terminal.output)
                terminal.output.clear()
                # Verify that leftover loop input is not replayed at the next UI.
                terminal.send("echo done >> after\r")
                after = scratch / "after"
                require(
                    lambda: after.exists() and after.read_text() == "done\n",
                    "next command",
                )
                require(ready, "next prompt")
                return result.read_text(), after.read_text()
            finally:
                terminal.close()

    def test_csh_complete_typeahead(self):
        """Preserve BSD csh's completed foreach typeahead result and same-line continuation
        display.
        """
        expected = self.exercise("csh", False, "complete")
        native_output = self.last_loop_output.split(b"READY> ")[0]
        self.assertEqual(expected, self.exercise("csh", True, "complete"))
        self.assertEqual(self.last_loop_output.split(b"\x1b[2K\r")[0], native_output)
        self.assertIn(b"? ? READY>", self.last_loop_output)

    def test_tcsh_complete_typeahead(self):
        """Match native tcsh results for complete foreach typeahead."""
        expected = self.exercise("tcsh", False, "complete")
        native_output = self.last_loop_output.split(b"READY> ")[0]
        self.assertEqual(expected, self.exercise("tcsh", True, "complete"))
        self.assertEqual(
            self.last_loop_output.split(b"\x1b[2K\r")[0],
            native_output,
            f"native={native_output!r}\nish={self.last_loop_output!r}",
        )

    def test_csh_partial_typeahead(self):
        """Allow later keystrokes to complete a partially typed BSD csh loop body."""
        self.assertEqual(
            self.exercise("csh", False, "partial"),
            self.exercise("csh", True, "partial"),
        )

    def test_tcsh_partial_typeahead(self):
        """Combine partial tcsh typeahead and later input into the same loop body."""
        self.assertEqual(
            self.exercise("tcsh", False, "partial"),
            self.exercise("tcsh", True, "partial"),
        )

    def test_csh_wait_at_secondary_prompt(self):
        """Keep recovery commands out of the loop body while BSD csh waits at a
        continuation prompt.
        """
        self.assertEqual(
            self.exercise("csh", False, "wait"), self.exercise("csh", True, "wait")
        )

    def test_tcsh_wait_at_secondary_prompt(self):
        """Let tcsh wait indefinitely for continuation input without ish interrupting it."""
        self.assertEqual(
            self.exercise("tcsh", False, "wait"), self.exercise("tcsh", True, "wait")
        )

    def test_csh_pasted_block(self):
        """Execute each BSD csh foreach iteration correctly when the whole block is pasted."""
        self.assertEqual(
            self.exercise("csh", False, "paste"), self.exercise("csh", True, "paste")
        )

    def test_tcsh_pasted_block(self):
        """Do not execute a pasted tcsh loop block again at the next prompt."""
        self.assertEqual(
            self.exercise("tcsh", False, "paste"), self.exercise("tcsh", True, "paste")
        )

    def test_csh_interrupt_substitution(self):
        """Accept the next command normally after interrupting a slow BSD csh substitution."""
        self.assertEqual(
            self.exercise("csh", False, "interrupt"),
            self.exercise("csh", True, "interrupt"),
        )

    def test_tcsh_interrupt_substitution(self):
        """Preserve tcsh history and subsequent input after Ctrl+C during command
        substitution.
        """
        self.assertEqual(
            self.exercise("tcsh", False, "interrupt"),
            self.exercise("tcsh", True, "interrupt"),
        )

    def test_tcsh_user_disabled_editor(self):
        """Handle typeahead without enabling a tcsh editor disabled by the user."""
        expected = self.exercise("tcsh", False, "complete_no_edit")
        native_output = self.last_loop_output.split(b"READY> ")[0]
        self.assertEqual(expected, self.exercise("tcsh", True, "complete_no_edit"))
        self.assertEqual(self.last_loop_output.split(b"\x1b[2K\r")[0], native_output)

    def test_tcsh_typeahead_after_replacing_both_hooks(self):
        """Handle foreach typeahead without contamination after both hooks are replaced."""
        expected = self.exercise("tcsh", False, "changed_hooks")
        native_output = self.last_loop_output.split(b"READY> ")[0]
        self.assertEqual(expected, self.exercise("tcsh", True, "changed_hooks"))
        self.assertEqual(self.last_loop_output.split(b"\x1b[2K\r")[0], native_output)


class PromptStreamTests(unittest.TestCase):
    """Verify caret prompt boundaries and continuation prompt output ordering."""

    def test_literal_prompt_markers_at_every_chunk_boundary(self):
        """Recognize a prompt once regardless of where its caret marker is split."""
        start, end = b"^[]633;S^G", b"^[]633;E^G"
        data = b"^C before " + start + b"prompt" + end + b" after ^x"
        for cut in range(len(data) + 1):
            seen = []
            sequencer = Sequencer()
            sequencer.between_sequence(start, end, seen.append)
            output = sequencer.interpret(data[:cut]) + sequencer.interpret(data[cut:])
            self.assertEqual(output, b"^C before  after ^x")
            self.assertEqual(seen, [b"prompt"])

    def test_secondary_prompt_replacement_stays_in_output_order(self):
        """Keep continuation replacement text between the surrounding ordinary output."""
        sequencer = Sequencer()
        start, end = b"\x1b]633;s\x07", b"\x1b]633;e\x07"
        sequencer.between_sequence(start, end, lambda prompt: prompt)
        data = b"echo body\r\n" + start + b"foreach? " + end + b"end\r\n"
        output = b"".join(sequencer.interpret(bytes([byte])) for byte in data)
        self.assertEqual(output, b"echo body\r\nforeach? end\r\n")


if __name__ == "__main__":
    unittest.main()
