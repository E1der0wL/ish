"""Command echo must not expose tcsh's internal editor restoration command."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ish.shell.sequencer import Sequencer
from test_foreach_typeahead import STARTUP_TIMEOUT, Terminal


class CommandEchoTests(unittest.TestCase):
    """Verify multiline submissions in real tcsh do not expose internal commands or
    duplicate echoes.
    """

    def exercise(self, shell, command):
        """Run the shell scenario with an isolated rc and PTY and observe its results."""
        path = os.environ.get("ISH_TEST_" + shell.upper()) or shutil.which(shell)
        if not path:
            self.skipTest(shell + " not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ish-command-echo-") as directory:
            scratch = Path(directory)
            (scratch / (".tcshrc" if shell == "tcsh" else ".cshrc")).write_text(
                "set prompt = 'READY> '\n"
            )
            # Keep the expected output outside the submitted command text.
            (scratch / "list.sh").write_text(
                "printf 'LISTING_RESULT\\n'\nprintf 'run\\n' >> result\n"
            )
            (scratch / "literal.sh").write_text(
                "printf '_ish_restore_editor; literal output\\n'\n"
            )
            env = {
                "HOME": directory,
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "XDG_DATA_HOME": str(scratch / "data"),
                "PYTHONPATH": str(root / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            terminal = Terminal(
                [sys.executable, "-B", "-m", "ish.main", path], directory, env
            )

            def ready():
                """Check that the primary prompt and required editor signals have been
                emitted.
                """
                return (
                    b"READY>" in terminal.output and b"\x1b[?2004h" in terminal.output
                )

            try:
                self.assertTrue(
                    terminal.until(ready, STARTUP_TIMEOUT), bytes(terminal.output)
                )
                terminal.output.clear()
                terminal.send(b"\x1b[200~" + command.encode() + b"\x1b[201~\r")
                result = scratch / "result"
                self.assertTrue(
                    terminal.until(
                        lambda: (
                            result.exists()
                            and result.read_text() == "run\n" * 3
                            and ready()
                            and terminal.output.count(b"LISTING_RESULT\r\n") == 3
                        )
                    ),
                    bytes(terminal.output),
                )
                output = bytes(terminal.output)
                self.assertEqual(output.count(b"LISTING_RESULT\r\n"), 3, output)
                self.assertNotIn(b"_ish_restore_editor;", output)
                # The UI already displayed the submitted block. There should
                # be no second copy between UI submission and command output.
                execution = output.split(b"\x1b[?2004l", 1)[1]
                self.assertTrue(execution.startswith(b"LISTING_RESULT\r\n"), execution)
                # An actual program printing the same string must remain visible.
                terminal.output.clear()
                terminal.send("/bin/sh literal.sh\r")
                self.assertTrue(
                    terminal.until(
                        lambda: (
                            ready()
                            and b"_ish_restore_editor; literal output\r\n"
                            in terminal.output
                        )
                    ),
                    bytes(terminal.output),
                )
                self.assertEqual(
                    terminal.output.count(b"_ish_restore_editor;"),
                    1,
                    bytes(terminal.output),
                )
            finally:
                terminal.close()

    def test_tcsh_multiple_lines(self):
        """Keep internal editor-restoration commands out of multiline command output."""
        self.exercise("tcsh", "/bin/sh list.sh\n/bin/sh list.sh\n/bin/sh list.sh")

    def test_tcsh_multiple_lines_with_tabs(self):
        """Execute multiline input containing tabs without duplicate echoes."""
        self.exercise("tcsh", "/bin/sh\tlist.sh\n/bin/sh list.sh\n/bin/sh list.sh")


class EchoStreamTests(unittest.TestCase):
    """Verify one-shot echo masking across chunk boundaries and mismatches."""

    def test_multiline_echo_at_every_chunk_boundary(self):
        """Remove only one submitted multiline echo at every possible chunk boundary."""
        echo = b"ls\r\nls\r\nls\r\n"
        # Identical text in subsequent program output is not another echo.
        printed = b"_ish_restore_editor; " + echo
        data = echo + printed
        for cut in range(len(data) + 1):
            sequencer = Sequencer()
            sequencer.at_masking(echo)
            output = sequencer.interpret(data[:cut]) + sequencer.interpret(data[cut:])
            self.assertEqual(output, printed)

    def test_mismatched_echo_does_not_filter_later_output(self):
        """Preserve similar program output after an echo mismatch."""
        echo = b"ls\r\n"
        printed = echo
        data = b"ls changed\r\n" + printed
        for cut in range(len(data) + 1):
            sequencer = Sequencer()
            sequencer.at_masking(echo)
            output = sequencer.interpret(data[:cut]) + sequencer.interpret(data[cut:])
            self.assertEqual(output, b"ls changed\r\n" + printed)

    def test_control_sequence_cancels_echo_mask(self):
        """Restore original bytes and stop masking when a control sequence interrupts an
        echo.
        """
        sequencer = Sequencer()
        sequencer.at_masking(b"ls\r\n")
        data = b"l\x1b[0ms\r\n"
        output = b"".join(sequencer.interpret(bytes([byte])) for byte in data)
        self.assertEqual(output, b"l\x1b[0ms\r\n")

    def test_oversized_echo_is_not_retained(self):
        """Avoid retaining an entire oversized command for echo masking."""
        sequencer = Sequencer(max_sequence_bytes=64)
        body = b"x" * 65 + b"\r\n"
        sequencer.at_masking(body)
        self.assertEqual(sequencer.masking_bytes, b"")
        output = b"".join(sequencer.interpret(bytes([byte])) for byte in body)
        self.assertEqual(output, body)

    def test_echo_is_masked_only_once(self):
        """Display later output identical to an echo that was already removed."""
        sequencer = Sequencer()
        echo = b"ls\r\n"
        sequencer.at_masking(echo)
        self.assertEqual(sequencer.interpret(echo + echo), echo)


if __name__ == "__main__":
    unittest.main()
