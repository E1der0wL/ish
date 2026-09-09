"""Command echo must not expose tcsh's internal editor restoration command."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from test_foreach_typeahead import Terminal
from ish.shell.sequencer import Sequencer


class CommandEchoTests(unittest.TestCase):
    def exercise(self, shell, command):
        path = os.environ.get('ISH_TEST_' + shell.upper()) or shutil.which(shell)
        if not path:
            self.skipTest(shell + ' not installed')
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ish-command-echo-') as directory:
            scratch = Path(directory)
            (scratch / ('.tcshrc' if shell == 'tcsh' else '.cshrc')).write_text(
                "set prompt = 'READY> '\n")
            # Keep the expected output outside the submitted command text.
            (scratch / 'list.sh').write_text("printf 'LISTING_RESULT\\n'\nprintf 'run\\n' >> result\n")
            (scratch / 'literal.sh').write_text("printf '_ish_restore_editor; literal output\\n'\n")
            env = {'HOME': directory, 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8',
                'PATH': '/usr/bin:/bin', 'XDG_DATA_HOME': str(scratch / 'data'),
                'PYTHONPATH': str(root / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
            terminal = Terminal([sys.executable, '-B', '-m', 'ish.main', path], directory, env)
            def ready():
                return b'READY>' in terminal.output and b'\x1b[?2004h' in terminal.output
            try:
                self.assertTrue(terminal.until(ready), bytes(terminal.output))
                terminal.output.clear()
                terminal.send(b'\x1b[200~' + command.encode() + b'\x1b[201~\r')
                result = scratch / 'result'
                self.assertTrue(terminal.until(lambda: result.exists() and
                    result.read_text() == 'run\n' * 3 and ready() and
                    terminal.output.count(b'LISTING_RESULT\r\n') == 3), bytes(terminal.output))
                output = bytes(terminal.output)
                self.assertEqual(output.count(b'LISTING_RESULT\r\n'), 3, output)
                self.assertNotIn(b'_ish_restore_editor;', output)
                # The UI already displayed the submitted block. There should
                # be no second copy between UI submission and command output.
                execution = output.split(b'\x1b[?2004l', 1)[1]
                self.assertTrue(execution.startswith(b'LISTING_RESULT\r\n'), execution)
                # An actual program printing the same string must remain visible.
                terminal.output.clear()
                terminal.send('/bin/sh literal.sh\r')
                self.assertTrue(terminal.until(lambda: ready() and
                    b'_ish_restore_editor; literal output\r\n' in terminal.output), bytes(terminal.output))
                self.assertEqual(terminal.output.count(b'_ish_restore_editor;'), 1, bytes(terminal.output))
            finally:
                terminal.close()

    def test_tcsh_multiple_lines(self):
        self.exercise('tcsh', '/bin/sh list.sh\n/bin/sh list.sh\n/bin/sh list.sh')

    def test_tcsh_multiple_lines_with_tabs(self):
        self.exercise('tcsh', '/bin/sh\tlist.sh\n/bin/sh list.sh\n/bin/sh list.sh')


class EchoStreamTests(unittest.TestCase):
    def test_multiline_echo_at_every_chunk_boundary(self):
        echo = b'ls\r\nls\r\nls\r\n'
        # Identical text in subsequent program output is not another echo.
        printed = b'_ish_restore_editor; ' + echo
        data = echo + printed
        for cut in range(len(data) + 1):
            sequencer = Sequencer()
            sequencer.at_masking(echo)
            output = sequencer.interpret(data[:cut]) + sequencer.interpret(data[cut:])
            self.assertEqual(output, printed)

    def test_mismatched_echo_does_not_filter_later_output(self):
        echo = b'ls\r\n'
        printed = echo
        data = b'ls changed\r\n' + printed
        for cut in range(len(data) + 1):
            sequencer = Sequencer()
            sequencer.at_masking(echo)
            output = sequencer.interpret(data[:cut]) + sequencer.interpret(data[cut:])
            self.assertEqual(output, b'ls changed\r\n' + printed)

    def test_control_sequence_cancels_echo_mask(self):
        sequencer = Sequencer()
        sequencer.at_masking(b'ls\r\n')
        data = b'l\x1b[0ms\r\n'
        output = b''.join(sequencer.interpret(bytes([byte])) for byte in data)
        self.assertEqual(output, b'l\x1b[0ms\r\n')

    def test_oversized_echo_is_not_retained(self):
        sequencer = Sequencer(max_sequence_bytes=64)
        body = b'x' * 65 + b'\r\n'
        sequencer.at_masking(body)
        self.assertEqual(sequencer.masking_bytes, b'')
        output = b''.join(sequencer.interpret(bytes([byte])) for byte in body)
        self.assertEqual(output, body)

    def test_echo_is_masked_only_once(self):
        sequencer = Sequencer()
        echo = b'ls\r\n'
        sequencer.at_masking(echo)
        self.assertEqual(sequencer.interpret(echo + echo), echo)


if __name__ == '__main__':
    unittest.main()
