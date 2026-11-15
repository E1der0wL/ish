"""Distinguish real PTY closure, process failure and a surviving input reader."""

from __future__ import annotations

import errno
import os
import pty
import resource
import select
import shutil
import signal
import tempfile
import time
import tty
import unittest
from pathlib import Path

from stream_state import failure, snapshot

DEADLINE = 3
READY = b"STATE_READY"


@unittest.skipUnless(hasattr(os, "WNOWAIT"), "requires non-reaping waitid")
class StateTests(unittest.TestCase):
    """Use independent process outcomes instead of synthesizing wait statuses."""

    def setUp(self):
        """Start a native PTY reader whose only actions come from explicit bytes."""
        self.reaped = False
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                signal.signal(signal.SIGSEGV, signal.SIG_DFL)
                tty.setraw(0)
                os.write(1, READY)
                action = os.read(0, 1)
                if action == b"x":
                    os._exit(0)
                if action == b"s":
                    os.kill(os.getpid(), signal.SIGSEGV)
                if action == b"d":
                    for fd in (0, 1, 2):
                        os.close(fd)
                    while True:
                        signal.pause()
            finally:
                os._exit(99)
        try:
            self.assertTrue(select.select([self.fd], [], [], DEADLINE)[0])
            self.assertEqual(os.read(self.fd, len(READY)), READY)
        except BaseException:
            self.tearDown()
            raise

    def tearDown(self):
        """Retain sole ownership of child reaping and close this fixture's PTY."""
        if not self.reaped:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
            self.reaped = True
        os.close(self.fd)

    def ended(self):
        """Observe the PTY's independent EOF or Linux EIO result."""
        self.assertTrue(select.select([self.fd], [], [], DEADLINE)[0])
        try:
            self.assertEqual(os.read(self.fd, 1024), b"")
            return "EOF"
        except OSError as error:
            self.assertEqual(error.errno, errno.EIO)
            return "EIO"

    def finished(self, *, cleanup=False):
        """Bound waiting for an actual exit event without consuming that event."""
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline:
            state = snapshot(self.pid, self.fd, cleanup=cleanup)
            if state["child"]["state"] in ("exited", "signaled"):
                return state
            time.sleep(0.01)
        self.fail(f"No exit event: {state}")

    def reap(self):
        """Prove repeated diagnostic observations left waitpid's status available."""
        pid, status = os.waitpid(self.pid, 0)
        self.reaped = True
        self.assertEqual(pid, self.pid)
        return status

    def test_normal_exit_is_not_a_signal_failure(self):
        """An ordinary zero exit has its own outcome even when PTY reads give EIO."""
        os.write(self.fd, b"x")
        kind = self.ended()
        state = self.finished()
        self.assertEqual(state["child"], {"state": "exited", "returncode": 0})
        repeated = snapshot(self.pid, self.fd)
        self.assertEqual(repeated["child"], state["child"])
        message = failure("Expected output missing", state, b"", pty_end={"kind": kind})
        self.assertIn('"returncode": 0', message)
        status = self.reap()
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)

    def test_sigsegv_preserves_the_actual_signal_for_the_owner(self):
        """A genuine SIGSEGV is reported before cleanup and remains independently reapable."""
        os.write(self.fd, b"s")
        self.ended()
        state = self.finished()
        self.assertEqual(state["phase"], "before-cleanup")
        self.assertEqual(state["child"]["state"], "signaled")
        self.assertEqual(state["child"]["signal"], "SIGSEGV")
        self.assertEqual(state["child"]["returncode"], -signal.SIGSEGV)
        status = self.reap()
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGSEGV)

    def test_surviving_reader_has_no_exit_event(self):
        """An unmet observation does not label a child blocked in input as exited."""
        state = snapshot(self.pid, self.fd)
        self.assertEqual(state["child"]["state"], "no-wait-event")
        self.assertEqual(state["foreground_pgid"], self.pid)
        self.assertIn("tty", state)
        self.assertEqual(state["phase"], "before-cleanup")
        os.write(self.fd, b"x")
        self.assertEqual(self.finished()["child"]["returncode"], 0)
        self.assertTrue(os.WIFEXITED(self.reap()))

    def test_closed_slave_does_not_prove_process_exit(self):
        """A living child can close its PTY; later cleanup's SIGKILL is separate evidence."""
        os.write(self.fd, b"d")
        kind = self.ended()
        before = snapshot(self.pid, self.fd)
        self.assertEqual(before["child"]["state"], "no-wait-event")
        os.kill(self.pid, signal.SIGKILL)
        after = self.finished(cleanup=True)
        self.assertEqual(after["phase"], "cleanup")
        self.assertEqual(after["child"]["signal"], "SIGKILL")
        self.assertEqual(before["phase"], "before-cleanup")
        self.assertEqual(before["child"]["state"], "no-wait-event")
        message = failure(
            "PTY ended", after, b"", pty_end={"kind": kind, "state": before}
        )
        self.assertIn('"no-wait-event"', message)
        self.assertIn('"SIGKILL"', message)
        self.assertEqual(os.WTERMSIG(self.reap()), signal.SIGKILL)


@unittest.skipUnless(shutil.which("bash"), "requires native Bash")
class StartupTests(unittest.TestCase):
    """Exercise cleanup when construction fails before a context manager yields."""

    def test_bash_exit_before_initial_prompt_does_not_leak_resources(self):
        """Keep pre-cleanup evidence while closing the socket and reaping the child."""
        from test_stream_bash import Shell

        with tempfile.TemporaryDirectory(prefix="ish-state-startup-") as directory:
            root = Path(directory)
            (root / ".bashrc").write_text("exit 17\n")
            shell = Shell.__new__(Shell)
            with self.assertRaisesRegex(AssertionError, "PTY ended") as raised:
                Shell.__init__(shell, root, enabled=False, popup=True)
            self.assertIn('"before-cleanup"', str(raised.exception))
            self.assertTrue(shell.cleanup_started)
            self.assertIsNone(shell.fd)
            self.assertEqual(shell.listener.fileno(), -1)
            with self.assertRaises(ChildProcessError):
                os.waitpid(shell.pid, os.WNOHANG)
            shell.close()  # Repeated cleanup must not act on a reused PID or FD.


if __name__ == "__main__":
    unittest.main()
