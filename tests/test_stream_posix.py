"""Exercise Bash and zsh through the real public CLI, PTY and PTK popup."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pyte

from stream_fixture import RAW_OUTPUT, shell_executable
from test_stream_cli import F2, FIXTURE, PROMPT, StreamProcess

BAR = b"ish stream | native shell | F2 popup"
MORE = b"STREAM_MORE> "
POPUP = b"Plugin popup"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class QueryScreen(pyte.Screen):
    """Let Vim's private SGR query pass through the limited test observer."""

    def select_graphic_rendition(self, *attributes, private=False):
        """Observe real SGR while ignoring a query which does not paint cells."""
        if not private:
            super().select_graphic_rendition(*attributes)


class PosixChecks:
    """Share observable expectations while each native shell owns its grammar."""

    shell: str

    def setUp(self):
        """Give every CLI an isolated home and observable file outputs."""
        temporary = tempfile.TemporaryDirectory(prefix=f"ish-stream-{self.shell}-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def start(self, *, demo=True, startup=""):
        """Launch one shell and wait for its actual primary and optional panel."""
        process = StreamProcess(
            self.directory, demo=demo, startup=startup, shell=self.shell
        )
        self.addCleanup(process.stop)
        process.expect(PROMPT)
        if demo:
            process.expect(BAR)
        return process

    def test_default_output_and_input_have_no_ui(self):
        """The unadorned entry point preserves native protocols and function keys."""
        process = self.start(demo=False)
        offset = process.fixture("output")
        process.expect(b"STREAM_RAW_DONE", offset)
        self.assertIn(RAW_OUTPUT, bytes(process.output[offset:]))
        result = self.directory / "plain-input"
        process.fixture("input", result)
        payload = b"native" + F2 + b"\x03\x04END"
        process.send(payload)
        process.expect_file(result, payload)
        self.assertNotIn(BAR, process.output)
        self.assertNotIn(POPUP, process.output)

    def test_empty_enter_preserves_natural_rows_and_short_output(self):
        """Blank submissions advance one row and retain the preceding result."""
        process = self.start()
        offset = len(process.output)
        process.command("printf 'VISIBLE_RESULT\\n'")
        process.expect(BAR, offset)
        prompt = PROMPT.decode().rstrip()
        for count in range(1, 5):
            with self.subTest(submissions=count):
                offset = len(process.output)
                process.send(b"\r")
                process.expect(PROMPT, offset)
                process.expect(BAR, offset)
                lines = [line.rstrip() for line in process.screen.display]
                rows = [row for row, line in enumerate(lines) if line == prompt]
                self.assertEqual(len(rows), count + 1, "\n".join(lines))
                self.assertEqual(rows, list(range(rows[0], rows[0] + count + 1)))
                self.assertIn("VISIBLE_RESULT", lines)

    def test_popup_preserves_native_buffer_cursor_and_following_input(self):
        """Popup text stays private and the native insertion point survives."""
        process = self.start()
        command = b"printf '%s\\n' abcd > native-buffer"
        insertion = command.index(b"abcd") + 2
        offset = len(process.output)
        process.send(command + b"\x01" + b"\x06" * insertion + F2)
        process.expect(POPUP, offset)
        process.send(b"touch popup-leak\rXY\r")
        process.expect_file(self.directory / "native-buffer", "abXYcd\n")
        process.command("printf 'done\\n' > popup-complete")
        process.expect_file(self.directory / "popup-complete", "done\n")
        self.assertFalse((self.directory / "popup-leak").exists())

    def test_popup_cancel_keys_leave_native_shell_usable(self):
        """Popup Ctrl+C and Ctrl+D cancel the popup without exiting the shell."""
        process = self.start()
        for index, key in enumerate((b"\x03", b"\x04", b"\x1b")):
            with self.subTest(key=key):
                # A previous popup's bar can occur before the next command's
                # prompt in the raw log. Observe the current visible panel.
                process.wait(
                    lambda: BAR.decode() in "\n".join(process.screen.display),
                    "visible primary panel before popup shortcut",
                )
                offset = len(process.output)
                process.send(F2)
                process.expect(POPUP, offset)
                offset = len(process.output)
                process.send(key)
                process.expect(BAR, offset)
                offset = len(process.output)
                process.send(
                    f"printf 'alive\\n' > cancel-{index}; "
                    f"printf 'CANCEL_%s_DONE\\n' {index}\r".encode()
                )
                completed = f"CANCEL_{index}_DONE".encode()
                process.expect(completed, offset)
                # A popup-return redisplay can precede the queued command.
                # Observe a primary after independent command output instead.
                after = process.output.index(completed, offset) + len(completed)
                process.expect(PROMPT, after)
                process.expect(BAR, after)
                process.expect_file(self.directory / f"cancel-{index}", "alive\n")

    def test_popup_shortcut_and_text_in_one_batch(self):
        """A popup burst cannot become a native command before the grant arrives."""
        process = self.start()
        process.send(F2 + b"touch burst-leak\r" + b"printf 'once\\n' >> burst-result\r")
        process.expect_file(self.directory / "burst-result", "once\n")
        process.command("printf 'done\\n' > burst-complete")
        process.expect_file(self.directory / "burst-complete", "done\n")
        self.assertEqual((self.directory / "burst-result").read_text(), "once\n")
        self.assertFalse((self.directory / "burst-leak").exists())

    def test_continuation_and_heredoc_remain_native(self):
        """Loop syntax and tool-looking heredoc lines become only shell data."""
        process = self.start()
        offset = len(process.output)
        process.send(b"for value in one two; do\r")
        process.expect(MORE, offset)
        self.assertNotIn(BAR.decode(), "\n".join(process.screen.display))
        self.assertNotIn(POPUP, process.output[offset:])
        process.send(b"printf '%s\\n' \"$value\" >> loop-result\rdone\r")
        process.expect_file(self.directory / "loop-result", "one\ntwo\n")
        process.expect(PROMPT, offset)
        offset = len(process.output)
        process.send(b"cat > heredoc-result << 'END'\r")
        process.expect(MORE, offset)
        self.assertNotIn(BAR.decode(), "\n".join(process.screen.display))
        body = "  spaced 한글\npython\nish_exit\n"
        process.send(body.encode() + b"END\r")
        process.expect_file(self.directory / "heredoc-result", body)
        process.expect(PROMPT, offset)
        self.assertNotIn(POPUP, process.output[offset:])

    def test_multiline_bracketed_paste_executes_once(self):
        """The native editor receives the complete pasted script unchanged."""
        process = self.start()
        script = (
            "printf '%s\\n' 'first 한글' > paste-result\n"
            "printf '%s\\n' 'second ; quoted' >> paste-result"
        )
        process.send(PASTE_START + script.encode() + PASTE_END + b"\r")
        process.expect_file(
            self.directory / "paste-result", "first 한글\nsecond ; quoted\n"
        )

    def test_external_stdin_receives_function_and_control_keys(self):
        """An executing program consumes F2, Ctrl+C and Ctrl+D itself."""
        process = self.start()
        result = self.directory / "native-input"
        offset = process.fixture("input", result)
        payload = b"native" + F2 + b"\x1b[12~\x03\x04END"
        process.send(payload)
        process.expect_file(result, payload)
        self.assertNotIn(POPUP, process.output[offset:])

    def test_fast_typeahead_after_enter_reaches_native_program(self):
        """The same input batch's F2 is not stolen after native submission."""
        process = self.start()
        result = self.directory / "typeahead-input"
        command = shlex.join([sys.executable, str(FIXTURE), "input", str(result)])
        payload = b"fast" + F2 + b"END"
        offset = len(process.output)
        process.send(command.encode() + b"\r" + payload)
        process.expect_file(result, payload)
        self.assertNotIn(POPUP, process.output[offset:])

    def test_user_editor_widget_keeps_native_stdin(self):
        """A native user widget's program must receive F2 and all following bytes."""
        result = self.directory / "widget-input"
        command = shlex.join([sys.executable, str(FIXTURE), "input", str(result)])
        if self.shell == "zsh":
            # ZLE gives user widgets /dev/null as stdin. Real terminal tools
            # explicitly reopen their controlling TTY, in plain zsh as well.
            command += " < /dev/tty"
        startup = f"stream_native_widget() {{ {command}; }}\n"
        if self.shell == "bash":
            startup += "bind -x '\"\\C-x\":stream_native_widget'\n"
        else:
            startup += (
                "zle -N stream_native_widget\nbindkey '^X' stream_native_widget\n"
            )
        process = self.start(startup=startup)
        offset = len(process.output)
        process.send(b"\x18")
        process.expect(b"STREAM_NATIVE_READY", offset)
        payload = F2 + b"END"
        process.send(payload)
        process.expect_file(result, payload)
        self.assertNotIn(POPUP, process.output[offset:])

    def test_builtin_read_and_continuation_interrupt_remain_native(self):
        """Builtin stdin and cancellation preserve their native consumers."""
        process = self.start()
        offset = len(process.output)
        process.send(
            b"printf 'BUILTIN_%s_READY\\n' READ; IFS= read -r value; "
            b"printf '%s\\n' \"$value\" > read-result\r"
        )
        # Native line editors retain different terminal/input state during
        # accept-line. Observe command execution before testing ordinary Enter;
        # separate burst tests verify raw typeahead without translating bytes.
        process.expect(b"BUILTIN_READ_READY\r\n", offset)
        process.send("stdin 한글\r".encode())
        process.expect_file(self.directory / "read-result", "stdin 한글\n")
        process.expect(PROMPT, offset)
        offset = len(process.output)
        process.send(b"for value in one two; do\r")
        process.expect(MORE, offset)
        offset = len(process.output)
        process.send(b"\x03")
        process.expect(PROMPT, offset)
        process.command("printf 'survived\\n' > interrupt-result")
        process.expect_file(self.directory / "interrupt-result", "survived\n")

    @unittest.skipUnless(shutil.which("vim"), "requires native Vim")
    def test_vim_edits_a_file_and_returns_to_the_panel(self):
        """A real foreground full-screen editor owns input and survives resize."""
        process = self.start()
        # The observer is not the host implementation. Vim's CSI ? m query
        # must not fail this test merely because pyte cannot model its reply.
        process.screen = QueryScreen(80, 24)
        process.stream = pyte.Stream(process.screen)
        offset = len(process.output)
        process.send(b"vim -Nu NONE -n vim-result\r")
        process.expect(b"\x1b[?1049h", offset)
        process.expect(b"vim-result", offset)
        self.assertNotIn(BAR.decode(), "\n".join(process.screen.display))
        process.resize(101, 31)
        process.send("iVIM_NATIVE 한글".encode() + b"\x1b:wq\r")
        process.expect_file(self.directory / "vim-result", "VIM_NATIVE 한글\n")
        process.expect(PROMPT, offset)
        process.expect(BAR, offset)

    def test_resize_reaches_native_program_and_primary(self):
        """Native programs get the complete dimensions and decoration can return."""
        process = self.start()
        result = self.directory / "sizes.json"
        offset = process.fixture("size", result)
        process.wait(result.exists, "initial native size")
        self.assertEqual(json.loads(result.read_text())[-1], [80, 24])
        process.resize(101, 31)

        def resized():
            try:
                return json.loads(result.read_text())[-1] == [101, 31]
            except (json.JSONDecodeError, IndexError):
                return False

        process.wait(resized, "native SIGWINCH and size")
        process.send(b"q")
        process.expect(b"STREAM_NATIVE_DONE", offset)
        process.expect(PROMPT, offset)
        process.expect(BAR, offset)
        process.command("printf 'resized\\n' > resize-result")
        process.expect_file(self.directory / "resize-result", "resized\n")

    def test_ctrl_d_exits_native_primary_normally(self):
        """Empty native-primary EOF still terminates the shell and relay."""
        process = self.start()
        process.send(b"\x04")
        process.wait(lambda: process.poll() is not None, "native Ctrl+D exit")
        self.assertEqual(process.status, 0)


@unittest.skipUnless(
    os.name == "posix" and shutil.which(shell_executable("bash")),
    "requires native Bash",
)
class BashCliTests(PosixChecks, unittest.TestCase):
    """Apply the public terminal contract to native Bash."""

    shell = "bash"

    def test_reserved_popup_key_is_inert_in_native_continuation(self):
        """The demo's reserved key neither opens a popup nor inserts its old macro."""
        process = self.start(startup='bind \'"\\eOQ":"OLD_F2"\'\n')
        offset = len(process.output)
        process.send(b"for value in one; do\r")
        process.expect(MORE, offset)
        process.send(F2 + b"printf 'native\\n' > reserved-continuation\rdone\r")
        process.expect_file(self.directory / "reserved-continuation", "native\n")
        self.assertNotIn(POPUP, process.output[offset:])

    def test_reserved_popup_key_is_inert_in_builtin_readline(self):
        """A running read-e retains its stdin while the demo key stays reserved."""
        process = self.start(startup='bind \'"\\eOQ":"OLD_F2"\'\n')
        offset = len(process.output)
        process.send(
            b"IFS= read -e -r -p 'READ_EDIT> ' value; "
            b"printf '%s' \"$value\" > reserved-read\r"
        )
        process.expect(b"READ_EDIT> ", offset)
        process.send(F2 + "native 한글\r".encode())
        process.expect_file(self.directory / "reserved-read", "native 한글")
        self.assertNotIn(POPUP, process.output[offset:])
        process.command("printf 'returned\\n' > read-completed")
        process.expect_file(self.directory / "read-completed", "returned\n")
        offset = len(process.output)
        process.send(F2)
        process.expect(POPUP, offset)
        process.send(b"\r")

    def test_completion_reader_gets_function_key_bytes(self):
        """Completion's ordinary stdin receives the key without a PTK focus grant."""
        startup = r"""stream_completion() {
    IFS= read -r -p 'COMPLETION_INPUT> ' value
    printf '%s' "$value" > completion-input
    COMPREPLY=(done)
}
complete -F stream_completion stream_fixture
"""
        process = self.start(startup=startup)
        offset = len(process.output)
        process.send(b"stream_fixture \t")
        process.expect(b"COMPLETION_INPUT> ", offset)
        self.assertNotIn(BAR.decode(), "\n".join(process.screen.display))
        # This ordinary reader runs inside Readline's raw terminal mode. LF is
        # its record delimiter; a CR keypress is not translated by native Bash.
        process.send(F2 + b"native\n")
        process.expect_file(self.directory / "completion-input", F2 + b"native")
        self.assertNotIn(POPUP, process.output[offset:])
        offset = len(process.output)
        process.send(b"\x03")
        process.expect(PROMPT, offset)
        process.expect(BAR, offset)
        process.command("printf 'returned\\n' > completion-return")
        process.expect_file(self.directory / "completion-return", "returned\n")

    def test_plain_and_demo_preserve_carriage_return_typeahead(self):
        """An explicit native ICRNL-off mode preserves CR data in both relay modes."""
        for demo in (False, True):
            with self.subTest(demo=demo):
                directory = self.directory / ("demo" if demo else "plain")
                directory.mkdir()
                # Readline uses this mode while editing. Keep it explicit in
                # the fixture so scheduler timing cannot change CR translation
                # between accept-line and the builtin reader.
                process = StreamProcess(
                    directory,
                    shell=self.shell,
                    demo=demo,
                    startup="stty -icrnl\n",
                )
                self.addCleanup(process.stop)
                process.expect(PROMPT)
                if demo:
                    process.expect(BAR)
                process.send(
                    b"IFS= read -r value; printf '%s' \"$value\" > typeahead\r"
                    b"first\rsecond\n"
                )
                process.expect_file(directory / "typeahead", b"first\rsecond")


@unittest.skipUnless(
    os.name == "posix" and shutil.which(shell_executable("zsh")), "requires native zsh"
)
class ZshCliTests(PosixChecks, unittest.TestCase):
    """Apply the same public terminal contract to native zsh."""

    shell = "zsh"


if __name__ == "__main__":
    unittest.main()
