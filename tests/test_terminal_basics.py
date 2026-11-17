"""Check graceful signal termination through the actual CLI and controlling PTY."""

import os
import shlex
import signal
import sys
import tempfile
import termios
import unittest
from pathlib import Path

import psutil

import test_terminal_cli as cli
from terminal_scenarios import SHELLS, HooklessTerminal


def assert_restored(case, terminal, expected, shell_pid):
    """Check actual outer modes, process status and reaping after a public exit."""
    terminal.require(terminal.exited)
    terminal.receive()
    case.assertEqual(terminal.status, expected)
    case.assertEqual(termios.tcgetattr(terminal.fd), terminal.attrs)
    case.assertIn(b"\x1b[?1049l", terminal.output)
    case.assertIn(b"\x1b[?1004l", terminal.output)
    case.assertIn(b"\x1b[?2004l", terminal.output)
    case.assertFalse(psutil.pid_exists(shell_pid))


@unittest.skipUnless(os.name == "posix", "A POSIX controlling PTY is required")
class TerminalLifecycleTests(unittest.TestCase):
    """Signals must restore outer modes and reap the shell while a program reads."""

    def test_hup_and_term_restore_tty_and_reap_native_shell(self):
        """Both CLI termination signals use the same application shutdown path."""
        for shell in SHELLS:
            for signum in (signal.SIGHUP, signal.SIGTERM):
                with (
                    self.subTest(shell=shell, signum=signum),
                    tempfile.TemporaryDirectory(prefix="ish-signals-") as directory,
                ):
                    root = Path(directory)
                    terminal = HooklessTerminal(root, shell=shell, plugins=False)
                    try:
                        terminal.require(terminal.at_prompt, cli.STARTUP_DEADLINE)
                        shell_pid = terminal.state()["child_pid"]
                        (root / "reader.py").write_text(cli.KEY_READER)
                        terminal.submit(f"{shlex.quote(sys.executable)} reader.py")
                        terminal.result("keys-ready", b"raw")
                        os.kill(terminal.pid, signum)
                        assert_restored(self, terminal, 128 + signum, shell_pid)
                    finally:
                        terminal.close()


if __name__ == "__main__":
    unittest.main()
