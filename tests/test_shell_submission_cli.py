"""Verify literal primary submissions through Bash/zsh and the actual PTK CLI."""

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_semantic_edit_cli as semantic

SHELLS = ("bash", "zsh")
SHELL_COMMANDS = """
ish_exit() { printf 'ish_exit:%s\\n' "$*" >> shell-results; }
ish_reload() { printf 'ish_reload:%s\\n' "$*" >> shell-results; }
probe() { printf 'probe:%s\\n' "$*" >> shell-results; }
"""
OBSERVE_CONFIG = """
with open('config-loads', 'a') as output:
    print('loaded', file=output)
"""


class ShellSubmissionCLITests(unittest.TestCase):
    """Check shell side effects and lifecycle without any internal command API."""

    @contextlib.contextmanager
    def session(self, name):
        """Run each default adapter with shell-owned former internal command names."""
        if not shutil.which(name):
            self.skipTest(f"{name} is required")
        with tempfile.TemporaryDirectory(prefix="ish-shell-submit-") as directory:
            root = Path(directory)
            with patch.object(semantic, "RC", semantic.RC + OBSERVE_CONFIG):
                terminal = semantic.Terminal(
                    root, shell=name, extra_startup=SHELL_COMMANDS
                )
            try:
                terminal.require(
                    lambda: (
                        b"READY>" in terminal.output
                        and semantic.EDITOR_ON in terminal.output
                    ),
                    semantic.STARTUP_DEADLINE,
                )
                yield root, terminal
            finally:
                terminal.close()

    def test_former_internal_names_execute_as_shell_commands_on_enter(self):
        """Each Enter reaches its shell function, preserving the CLI and configuration."""
        for name in SHELLS:
            with self.subTest(shell=name), self.session(name) as (root, terminal):
                expected = ""
                for command, result in (
                    ("ish_exit", "ish_exit:\n"),
                    ("ish_reload", "ish_reload:\n"),
                    ("probe", "probe:\n"),
                    ("ish_exit 'still running'", "ish_exit:still running\n"),
                    ("ish_reload 'same session'", "ish_reload:same session\n"),
                    ("probe '  한 글  '", "probe:  한 글  \n"),
                ):
                    terminal.send(command + "\r")
                    expected += result
                    terminal.result("shell-results", expected.encode())
                    self.assertTrue(terminal.witness()["primary"])
                    self.assertEqual((root / "config-loads").read_text(), "loaded\n")
                terminal.send("printf alive > survived\r")
                terminal.result("survived", b"alive")
                self.assertTrue(terminal.witness()["primary"])

    def test_primary_ctrl_c_discards_only_the_current_edit(self):
        """Cancelling a primary edit preserves immediate following terminal keys."""
        for name in SHELLS:
            with self.subTest(shell=name), self.session(name) as (root, terminal):
                terminal.send(b"echo wrong > cancelled\x03echo kept > result\r")
                terminal.result("result", b"kept\n")
                self.assertFalse((root / "cancelled").exists())
                self.assertTrue(terminal.witness()["primary"])

    def test_primary_ctrl_d_exits_without_an_internal_text_command(self):
        """An empty primary editor still exits normally through its EOF key binding."""
        for name in SHELLS:
            with self.subTest(shell=name), self.session(name) as (_, terminal):
                self.assertTrue(terminal.witness()["primary"])
                terminal.send(b"\x04")
                observed = None

                def exited():
                    nonlocal observed
                    observed = os.waitid(
                        os.P_PID,
                        terminal.pid,
                        os.WEXITED | os.WNOWAIT | os.WNOHANG,
                    )
                    return observed is not None

                terminal.require(exited)
                self.assertEqual(
                    (observed.si_code, observed.si_status), (os.CLD_EXITED, 0)
                )


if __name__ == "__main__":
    unittest.main()
