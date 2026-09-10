"""Compare tcsh history and hook behavior with and without the ish UI."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from test_foreach_typeahead import STARTUP_TIMEOUT, Terminal


class TcshHistoryTests(unittest.TestCase):
    """Compare history, editor options, and runtime hook changes with native tcsh."""

    def exercise(self, ish, commands, files, rc="", fixtures=None):
        """Run the shell scenario with an isolated rc and PTY and observe its results."""
        path = os.environ.get("ISH_TEST_TCSH") or shutil.which("tcsh")
        if not path:
            self.skipTest("tcsh not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ish-history-") as directory:
            scratch = Path(directory)
            (scratch / ".tcshrc").write_text(
                "set prompt = 'READY> '\nset history = 100\n" + rc
            )
            (scratch / "run.sh").write_text("printf 'run\\n' >> result\n")
            for name, contents in (fixtures or {}).items():
                (scratch / name).write_text(contents)
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
            prompt_text = b"READY>"

            def ready():
                """Check that the primary prompt and required editor signals have been
                emitted.
                """
                if ish:
                    return (
                        prompt_text in terminal.output
                        and b"\x1b[?2004h" in terminal.output
                    )
                # A command defining the prompt can echo that text before it
                # executes. Wait for the actual prompt at the output tail.
                return terminal.output.endswith(prompt_text + b" ")

            try:
                self.assertTrue(
                    terminal.until(ready, STARTUP_TIMEOUT), bytes(terminal.output)
                )
                for command in ["history -c", *commands]:
                    pending_input = []
                    if isinstance(command, tuple):
                        command, prompt, *pending_input = command
                        prompt_text = prompt.encode()
                    terminal.output.clear()
                    if ish:
                        # Submit the exact text without UI auto-pairing edits.
                        terminal.send(b"\x1b[200~" + command.encode() + b"\x1b[201~\r")
                    else:
                        terminal.send(command + "\r")
                    if pending_input:
                        self.assertTrue(
                            terminal.until(
                                lambda: b"READ_WAITING\r\n" in terminal.output
                            ),
                            bytes(terminal.output),
                        )
                        # Longer than the old _recover inactivity threshold.
                        terminal.until(lambda: False, 1.5)
                        self.assertFalse(
                            (scratch / "answer").exists(), bytes(terminal.output)
                        )
                        terminal.send(pending_input[0] + "\r")
                    self.assertTrue(
                        terminal.until(ready), (command, bytes(terminal.output))
                    )
                    if ish:
                        self.assertNotIn(b"_ish_restore_editor", terminal.output)
                self.assertTrue(
                    terminal.until(
                        lambda: all((scratch / name).exists() for name in files)
                    ),
                    bytes(terminal.output),
                )
                return {name: (scratch / name).read_text() for name in files}
            finally:
                terminal.close()

    def test_repeat_and_history_match_native_tcsh(self):
        """Keep ish restoration commands out of history and repeated !! execution."""
        commands = ["/bin/sh run.sh", "!!", "!!", "history -h > saved-history"]
        native = self.exercise(False, commands, ["result", "saved-history"])
        self.assertEqual(native["result"], "run\n" * 3)
        self.assertEqual(self.exercise(True, commands, list(native)), native)

    def test_history_argument_selector_matches_native_tcsh(self):
        """Generate the same commands as native tcsh with history argument selectors."""
        commands = [
            "echo first second",
            "echo !!:1 > selected",
            "history -h > saved-history",
        ]
        native = self.exercise(False, commands, ["selected", "saved-history"])
        self.assertEqual(native["selected"], "first\n")
        self.assertEqual(self.exercise(True, commands, list(native)), native)

    def test_existing_postcmd_receives_native_history_and_status(self):
        """Let existing postcmd hooks observe the original command and exit status."""
        rc = r"""alias postcmd 'echo $status >> hook-status; echo \!#:q >> hook-commands'
alias precmd 'true'
"""
        commands = [
            "rm -f hook-status hook-commands; history -c",
            "false",
            "echo $status > last-status",
            "/bin/sh run.sh",
            "!!",
            "history -h > saved-history",
        ]
        files = [
            "hook-status",
            "hook-commands",
            "last-status",
            "result",
            "saved-history",
        ]
        native = self.exercise(False, commands, files, rc)
        self.assertEqual(native["last-status"], "1\n")
        self.assertEqual(self.exercise(True, commands, files, rc), native)

    def test_skipped_postcmd_keeps_editor_setting(self):
        """Preserve user editor settings when postcmd is skipped."""
        commands = [
            "echo ok",
            "!!:p",
            "!987654321",
            "echo (",
            "echo $?edit > edit-state",
            "history -h > saved-history",
        ]
        files = ["edit-state", "saved-history"]
        native = self.exercise(False, commands, files)
        self.assertEqual(native["edit-state"], "1\n")
        self.assertEqual(self.exercise(True, commands, files), native)

    def test_user_can_change_editor_setting(self):
        """Preserve tcsh edit option changes made while ish is running."""
        commands = [
            "unset edit",
            "echo $?edit >> edit-state",
            "echo ok",
            "!!:p",
            "echo $?edit >> edit-state",
            "set edit",
            "echo $?edit >> edit-state",
            "history -h > saved-history",
        ]
        files = ["edit-state", "saved-history"]
        native = self.exercise(False, commands, files)
        self.assertEqual(native["edit-state"], "0\n0\n1\n")
        self.assertEqual(self.exercise(True, commands, files), native)

    def test_replace_precmd_at_runtime(self):
        """Preserve user hooks and repair integration after precmd is redefined at runtime."""
        commands = [
            ("alias precmd \"set prompt='ish > '\"", "ish >"),
            "false",
            "echo $status > last-status",
            "/bin/sh run.sh",
            "!!",
            "history -h > saved-history",
        ]
        files = ["last-status", "result", "saved-history"]
        native = self.exercise(False, commands, files)
        self.assertEqual(self.exercise(True, commands, files), native)

    def test_replace_and_remove_postcmd_at_runtime(self):
        """Continue handling command boundaries after postcmd replacement or removal."""
        commands = [
            "rm -f hooks; history -c",
            "alias postcmd 'echo new:$status >> hooks'",
            "false",
            "echo $status > last-status",
            "unalias postcmd",
            "/bin/sh run.sh",
            "!!",
            "history -h > saved-history",
        ]
        files = ["hooks", "last-status", "result", "saved-history"]
        rc = "alias postcmd 'echo old:$status >> hooks'\n"
        native = self.exercise(False, commands, files, rc)
        self.assertEqual(self.exercise(True, commands, files, rc), native)

    def test_replace_both_hooks_and_remove_precmd(self):
        """Preserve native behavior when replacing both hooks and removing precmd."""
        commands = [
            (
                "alias precmd \"set prompt='changed > '\"; alias postcmd 'echo $status >> hooks'",
                "changed >",
            ),
            "/bin/sh run.sh",
            "unalias precmd",
            "/bin/sh run.sh",
            "echo $?edit > edit-state",
            "history -h > saved-history",
        ]
        files = ["hooks", "result", "edit-state", "saved-history"]
        native = self.exercise(False, commands, files)
        self.assertEqual(self.exercise(True, commands, files), native)

    def test_source_can_replace_both_hooks(self):
        """Return to the primary prompt after an external rc replaces both hooks."""
        commands = [
            ("source changed.tcsh", "sourced >"),
            "false",
            "echo $status > last-status",
            "/bin/sh run.sh",
            "!!",
            "history -h > saved-history",
        ]
        fixtures = {
            "changed.tcsh": "alias precmd \"set prompt='sourced > '\"\n"
            "alias postcmd 'echo new:$status >> hooks'\n"
        }
        files = ["hooks", "result", "last-status", "saved-history"]
        native = self.exercise(False, commands, files, fixtures=fixtures)
        self.assertEqual(
            self.exercise(True, commands, files, fixtures=fixtures), native
        )

    def test_existing_periodic_hook_and_nested_postcmd_are_preserved(self):
        """Preserve the processing order of existing periodic and nested postcmd hooks."""
        rc = "alias periodic 'source periodic.tcsh'\nalias postcmd 'echo post:$status >> hooks'\n"
        fixtures = {"periodic.tcsh": "echo periodic:$status >> periodic-calls\n"}
        commands = [
            "rm -f hooks periodic-calls; history -c",
            "false",
            "echo $status > last-status",
            ("alias precmd \"set prompt='periodic > '\"", "periodic >"),
            "true",
            "cp hooks saved-hooks; cp periodic-calls saved-periodic",
        ]
        # Snapshot inside the command, before the next periodic callback can
        # append. A native line-editor redraw can otherwise race file reads.
        files = ["saved-hooks", "saved-periodic", "last-status"]
        native = self.exercise(False, commands, files, rc, fixtures)
        self.assertEqual(self.exercise(True, commands, files, rc, fixtures), native)

    def test_existing_periodic_interval_is_preserved(self):
        """Keep integration monitoring from changing the user's periodic interval
        semantics.
        """
        rc = "set tperiod = 600\nalias periodic 'echo periodic >> periodic-calls'\n"
        commands = [
            "printf '' > periodic-calls",
            "false",
            ("alias precmd \"set prompt='interval > '\"", "interval >"),
            "true",
        ]
        files = ["periodic-calls"]
        native = self.exercise(False, commands, files, rc)
        self.assertEqual(native["periodic-calls"], "")
        self.assertEqual(self.exercise(True, commands, files, rc), native)

    def test_hook_recovery_does_not_inject_into_dollar_less(self):
        """Do not inject recovery commands as user values while tcsh waits for $< input."""
        commands = [
            ("alias precmd \"set prompt='read > '\"; alias postcmd 'true'", "read >"),
            ("source read.tcsh", "read >", "typed response"),
            "history -h > saved-history",
        ]
        fixtures = {
            "read.tcsh": 'echo READ_WAITING\nset read_value = "$<"\n'
            "printf '%s\\n' \"$read_value\" > answer\n"
        }
        files = ["answer", "saved-history"]
        native = self.exercise(False, commands, files, fixtures=fixtures)
        self.assertEqual(native["answer"], "typed response\n")
        self.assertEqual(
            self.exercise(True, commands, files, fixtures=fixtures), native
        )


if __name__ == "__main__":
    unittest.main()
