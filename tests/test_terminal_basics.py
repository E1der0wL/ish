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


@unittest.skipUnless(
    os.name == "posix" and cli.TCSH, "A POSIX PTY and tcsh are required"
)
class TerminalLifecycleTests(unittest.TestCase):
    """Signals must restore outer modes and reap the shell while a program reads."""

    def test_hup_and_term_restore_tty_and_reap_native_shell(self):
        """Both CLI termination signals use the same application shutdown path."""
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with (
                self.subTest(signum=signum),
                tempfile.TemporaryDirectory(prefix="ish-signals-") as directory,
            ):
                root = Path(directory)
                terminal = cli.Terminal(root)
                try:
                    terminal.require(terminal.at_prompt, cli.STARTUP_DEADLINE)
                    shell_pid = terminal.state()["child_pid"]
                    (root / "reader.py").write_text(cli.KEY_READER)
                    terminal.submit(f"{shlex.quote(sys.executable)} reader.py")
                    terminal.result("keys-ready", b"raw")
                    os.kill(terminal.pid, signum)
                    terminal.require(terminal.exited)
                    terminal.receive()
                    self.assertEqual(terminal.status, 128 + signum)
                    self.assertEqual(termios.tcgetattr(terminal.fd), terminal.attrs)
                    self.assertIn(b"\x1b[?1049l", terminal.output)
                    self.assertIn(b"\x1b[?1004l", terminal.output)
                    self.assertIn(b"\x1b[?2004l", terminal.output)
                    self.assertFalse(psutil.pid_exists(shell_pid))
                finally:
                    terminal.close()


if __name__ == "__main__":
    unittest.main()
