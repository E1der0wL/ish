"""Compare stock tcsh's interactive input with a persistent source-FIFO experiment.

These are native consumer and semantic-difference tests, not PTK ownership tests.
All state/results come from the real shell. No Python shell grammar parser is used.
"""

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tcsh_channel import CONTINUATION, PRIMARY, NativeChannel

TCSH = os.environ.get("ISH_TEST_TCSH") or shutil.which("tcsh")
PAYLOAD = "  한 글 🙂  \nish_exit\nprobe\n"


@unittest.skipUnless(TCSH, "tcsh is required")
class TcshChannelTests(unittest.TestCase):
    """Pin down what FD separation preserves and what source execution changes."""

    @contextlib.contextmanager
    def session(self, fifo):
        """Use the same shell setup for the native baseline and source experiment."""
        with tempfile.TemporaryDirectory(prefix="ish-tcsh-channel-") as directory:
            root = Path(directory)
            channel = NativeChannel(TCSH, root)
            try:
                if fifo:
                    channel.start_source()
                yield root, channel
            finally:
                channel.close()

    def submit(self, channel, fifo, data):
        """Choose the experimentally assigned syntax transport, not a guessed phase."""
        if fifo:
            channel.write_syntax(data)
        else:
            channel.write_stdin(data)

    def result(self, channel, root, name, expected):
        """Require the actual consuming command to produce exactly the expected bytes."""
        path = root / name
        channel.require(lambda: path.exists() and path.read_bytes() == expected)

    def finish(self, channel, fifo):
        """Observe return to the real interactive parser after the submitted script."""
        if fifo:
            channel.close_syntax()
        channel.require(lambda: channel.output.endswith(PRIMARY))

    def test_dollar_input_consumes_pty_while_syntax_uses_its_channel(self):
        """The shell's dollar-less-than reads stdin, preserving spaces and Unicode."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                self.submit(
                    channel,
                    fifo,
                    'echo waiting > entered; set value = "$<"; echo "$value" > result\n',
                )
                self.result(channel, root, "entered", b"waiting\n")
                channel.write_stdin("  native 한 글  \n")
                self.result(channel, root, "result", "  native 한 글  \n".encode())
                self.finish(channel, fifo)

    def test_foreground_program_reads_only_pty_stdin(self):
        """An external program receives native data rather than following FIFO syntax."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                (root / "reader.sh").write_text(
                    "printf waiting > entered\nIFS= read -r value\n"
                    'printf "%s" "$value" > result\n'
                )
                self.submit(channel, fifo, "/bin/sh reader.sh\n")
                self.result(channel, root, "entered", b"waiting")
                channel.write_stdin("  probe native 한 글  \n")
                self.result(channel, root, "result", "  probe native 한 글  ".encode())
                self.finish(channel, fifo)

    def test_command_substitution_consumes_native_input_then_more_syntax(self):
        """One foreach command requires raw stdin before its loop syntax can proceed."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                (root / "reader.sh").write_text(
                    "printf waiting > entered\nIFS= read -r value\n"
                    'printf "%s\\n" "$value" > raw-result\n'
                    'printf "%s\\n" "$value"\n'
                )
                self.submit(channel, fifo, "foreach value (`/bin/sh reader.sh`)\n")
                self.result(channel, root, "entered", b"waiting")
                channel.write_stdin("first second\n")
                self.result(channel, root, "raw-result", b"first second\n")
                self.submit(channel, fifo, "echo $value >> result\nend\n")
                self.result(channel, root, "result", b"first\nsecond\n")
                self.finish(channel, fifo)

    def test_source_executes_nested_loop_body_before_end_is_submitted(self):
        """FIFO source loses interactive prereading, even though final results match."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                lines = (
                    "set i = 0\n",
                    "while ( $i < 1 )\n",
                    "foreach value ( A B )\n",
                    "echo $value >> result\n",
                )
                for index, line in enumerate(lines):
                    channel.output.clear()
                    self.submit(channel, fifo, line)
                    if not fifo:
                        prompt = PRIMARY if index == 0 else CONTINUATION
                        channel.require(
                            lambda prompt=prompt: channel.output.endswith(prompt)
                        )
                if fifo:
                    self.result(channel, root, "result", b"A\n")
                else:
                    self.assertFalse((root / "result").exists())
                channel.output.clear()
                self.submit(channel, fifo, "end\n@ i++\nend\n")
                self.result(channel, root, "result", b"A\nB\n")
                self.finish(channel, fifo)

    def test_heredoc_keeps_spaces_unicode_and_tool_names_as_data(self):
        """The shell consumes the full heredoc; this does not test an ish dispatcher."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                self.submit(channel, fifo, "cat << END > result\n" + PAYLOAD + "END\n")
                self.result(channel, root, "result", PAYLOAD.encode())
                self.finish(channel, fifo)

    def test_source_and_eval_use_native_stdin_inside_their_own_syntax(self):
        """Nested source/eval syntax is not supplied again by the outer command input."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                (root / "inner.csh").write_text(
                    'echo source > entered\nset value = "$<"\necho "$value" > sourced\n'
                )
                self.submit(channel, fifo, "source inner.csh\n")
                self.result(channel, root, "entered", b"source\n")
                channel.write_stdin("  source data  \n")
                self.result(channel, root, "sourced", b"  source data  \n")
                channel.output.clear()
                self.submit(
                    channel,
                    fifo,
                    'eval \'echo eval > entered; set value = "$<"; echo "$value" > evaluated\'\n',
                )
                self.result(channel, root, "entered", b"eval\n")
                channel.write_stdin("  eval data  \n")
                self.result(channel, root, "evaluated", b"  eval data  \n")
                self.finish(channel, fifo)

    def test_variable_environment_alias_cwd_and_status_persist(self):
        """Sourcing keeps the same process and shell state, including a failing status."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                (root / "child").mkdir()
                self.submit(
                    channel,
                    fifo,
                    "set value = kept\nsetenv CHANNEL_VALUE exported\n"
                    "alias channel_alias 'echo alias-value'\ncd child\n"
                    '/bin/false\necho "$status" > status\n'
                    'echo "${value}:$CHANNEL_VALUE" > state\nchannel_alias > aliased\n'
                    "pwd > cwd\necho $$ > pid\n",
                )
                self.result(channel, root, "child/pid", f"{channel.pid}\n".encode())
                self.finish(channel, fifo)
                self.assertEqual((root / "child/status").read_bytes(), b"1\n")
                self.assertEqual(
                    (root / "child/state").read_bytes(), b"kept:exported\n"
                )
                self.assertEqual(
                    (root / "child/aliased").read_bytes(), b"alias-value\n"
                )
                self.assertEqual(
                    (root / "child/cwd").read_text(), str(root / "child") + "\n"
                )
                channel.output.clear()
                channel.write_stdin('echo "${value}:$CHANNEL_VALUE" > returned-state\n')
                self.result(channel, root, "child/returned-state", b"kept:exported\n")

    def test_source_eof_returns_queued_native_input_to_the_original_parser(self):
        """FD separation alone does not stop late program input becoming a command."""
        with self.session(True) as (root, channel):
            channel.write_syntax("echo completed > result\n")
            self.result(channel, root, "result", b"completed\n")
            channel.write_stdin("echo late > late-result\n")
            channel.close_syntax()
            self.result(channel, root, "late-result", b"late\n")
            channel.require(lambda: channel.output.endswith(PRIMARY))

    def test_raw_typeahead_becomes_a_command_after_source_eof(self):
        """Bytes left after an ordinary stdin read can cross the source-return boundary."""
        with self.session(True) as (root, channel):
            (root / "reader.sh").write_text(
                "printf waiting > entered\nIFS= read -r value\n"
                'printf "%s" "$value" > raw-result\n'
            )
            channel.write_syntax("/bin/sh reader.sh\necho continued > source-result\n")
            self.result(channel, root, "entered", b"waiting")
            channel.write_stdin("native data\necho escaped > late-result\n")
            self.result(channel, root, "raw-result", b"native data")
            self.result(channel, root, "source-result", b"continued\n")
            channel.close_syntax()
            self.result(channel, root, "late-result", b"escaped\n")
            channel.require(lambda: channel.output.endswith(PRIMARY))

    def test_source_changes_prompt_hooks_and_interactive_history(self):
        """State survives, but source omits inner precmd/history and adds outer postcmd."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(False) as (root, channel):
                channel.write_stdin(
                    "set history = 100; "
                    "alias precmd 'echo PRE >> hooks-pre'; "
                    "alias postcmd 'echo POST >> hooks-post'\n"
                )
                channel.require(lambda: channel.output.endswith(PRIMARY))
                (root / "hooks-pre").write_text("")
                (root / "hooks-post").write_text("")
                channel.output.clear()
                if fifo:
                    channel.start_source()
                self.submit(
                    channel,
                    fifo,
                    "echo history-one > first\necho history-two > second\n"
                    '/bin/false\necho "$status" > last-status\n',
                )
                self.result(channel, root, "last-status", b"1\n")
                self.finish(channel, fifo)
                self.assertEqual(
                    (root / "hooks-pre").read_text(), "PRE\n" * (1 if fifo else 4)
                )
                self.assertEqual(
                    (root / "hooks-post").read_text(), "POST\n" * (5 if fifo else 4)
                )
                channel.output.clear()
                channel.write_stdin("history -h > history-output\n")
                channel.require(
                    lambda: (
                        (root / "history-output").exists()
                        and channel.output.endswith(PRIMARY)
                    )
                )
                history = (root / "history-output").read_text()
                if fifo:
                    self.assertNotIn("echo history-one > first", history)
                    self.assertIn("source " + str(root / "syntax.fifo"), history)
                else:
                    self.assertIn("echo history-one > first", history)

    def test_fifo_crlf_is_file_data_while_pty_normalizes_carriage_return(self):
        """The syntax transport cannot silently inherit terminal newline conversion."""
        for fifo in (False, True):
            with self.subTest(fifo=fifo), self.session(fifo) as (root, channel):
                self.submit(channel, fifo, b"echo value > crlf\r\n")
                target = "crlf\r" if fifo else "crlf"
                self.result(channel, root, target, b"value\n")
                self.finish(channel, fifo)

    def test_source_error_abandons_remaining_syntax_and_recovers_on_pty(self):
        """Source error unwinds its reader and skips a trailing completion command."""
        with self.session(True) as (root, channel):
            channel.write_syntax('echo "unterminated\necho wrong > skipped\n')
            channel.require(lambda: channel.output.endswith(PRIMARY))
            channel.close_syntax()
            channel.output.clear()
            channel.write_stdin("echo recovered > result\n")
            self.result(channel, root, "result", b"recovered\n")
            self.assertFalse((root / "skipped").exists())

    def test_ctrl_c_with_fifo_close_allows_a_fresh_source(self):
        """Closing the syntax stream lets a pending terminal interrupt unwind source."""
        with self.session(True) as (root, channel):
            channel.write_syntax("set value = kept; echo waiting > entered\n")
            self.result(channel, root, "entered", b"waiting\n")
            channel.output.clear()
            channel.write_stdin(b"\x03")
            channel.close_syntax()
            channel.require(lambda: channel.output.endswith(PRIMARY))
            channel.start_source()
            channel.write_syntax('echo "$value" > result\n')
            self.result(channel, root, "result", b"kept\n")
            self.finish(channel, True)

    def test_ctrl_d_goes_to_native_program_and_fifo_eof_ends_heredoc(self):
        """PTY EOF and syntax EOF affect different consumers in separated mode."""
        with self.session(True) as (root, channel):
            channel.write_syntax("echo waiting > entered; cat > native\n")
            self.result(channel, root, "entered", b"waiting\n")
            channel.write_stdin(b"native data\n\x04")
            channel.write_syntax(
                "echo resumed > resumed\ncat << END > heredoc\nprobe\n"
            )
            self.result(channel, root, "resumed", b"resumed\n")
            self.assertEqual((root / "native").read_bytes(), b"native data\n")
            channel.close_syntax()
            self.result(channel, root, "heredoc", b"probe\n")
            channel.require(lambda: channel.output.endswith(PRIMARY))

    def test_partial_fifo_writes_preserve_unicode_and_backslash_continuation(self):
        """A physical line may span writes without becoming multiple editor requests."""
        with self.session(True) as (root, channel):
            payload = "printf '%s\\n' '한 글' \\\n'probe' > result\n".encode()
            for byte in payload:
                channel.write_syntax(bytes((byte,)))
            self.result(channel, root, "result", "한 글\nprobe\n".encode())
            self.finish(channel, True)


if __name__ == "__main__":
    unittest.main()
