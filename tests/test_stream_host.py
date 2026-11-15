"""Verify host-test resize bookkeeping with real signals and kernel PTY sizes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_host import HostRun
from stream_matrix import AutomaticRun

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 5


@unittest.skipUnless(os.name == "posix", "requires native PTYs and SIGWINCH")
class HostResizeTests(unittest.TestCase):
    """Keep signal callbacks from overwriting evidence or delaying final resize."""

    def setUp(self):
        """Use isolated evidence files without an interactive outer terminal."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-host-resize-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def terminal(self):
        """Allocate a real kernel terminal and retain both endpoints for inspection."""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return master, slave

    def test_signal_during_atomic_publication_waits_for_next_pump(self):
        """A nested SIGWINCH cannot consume the outer writer's temporary file."""
        _, host = self.terminal()
        child, native = self.terminal()
        termios.tcsetwinsize(host, (24, 80))
        termios.tcsetwinsize(native, (24, 80))
        with patch(
            "stream_host.termios.tcgetattr", return_value=termios.tcgetattr(host)
        ):
            runner = HostRun(self.directory, False)
        runner.fd = child
        get_size = termios.tcgetwinsize
        replace = Path.replace
        injected = False

        def resize_during_replace(source, target):
            nonlocal injected
            if target.name == "host-size.json" and not injected:
                injected = True
                termios.tcsetwinsize(host, (31, 101))
                os.kill(os.getpid(), signal.SIGWINCH)
            return replace(source, target)

        previous = signal.signal(signal.SIGWINCH, runner.resize)
        try:
            with (
                patch(
                    "stream_host.termios.tcgetwinsize",
                    side_effect=lambda _: get_size(host),
                ),
                patch("stream_host.select.select", return_value=([], [], [])),
                patch.object(Path, "replace", resize_during_replace),
            ):
                os.kill(os.getpid(), signal.SIGWINCH)
                self.assertFalse((self.directory / "host-size.json").exists())
                runner.pump()
                self.assertTrue(injected)
                self.assertEqual(
                    json.loads((self.directory / "host-size.json").read_text()),
                    {"sizes": [[80, 24]]},
                )
                self.assertEqual(get_size(native), (24, 80))
                runner.pump()
                self.assertEqual(
                    json.loads((self.directory / "host-size.json").read_text()),
                    {"sizes": [[80, 24], [101, 31]]},
                )
                self.assertEqual(get_size(native), (31, 101))
                self.assertFalse((self.directory / "host-size.json.tmp").exists())
        finally:
            signal.signal(signal.SIGWINCH, previous)

    def test_native_size_fixture_records_burst_and_restores_terminal(self):
        """Rapid signals publish the final kernel size while q still exits normally."""
        master, slave = self.terminal()
        termios.tcsetwinsize(slave, (24, 80))
        saved = termios.tcgetattr(slave)
        output = bytearray()
        destination = self.directory / "sizes.json"
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/stream_fixture.py"),
                "size",
                str(destination),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )

        def stopped():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=DEADLINE)

        self.addCleanup(stopped)
        deadline = time.monotonic() + DEADLINE
        while b"STREAM_NATIVE_READY\r\n" not in output:
            self.assertLess(time.monotonic(), deadline, bytes(output))
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
        for offset in range(30):
            termios.tcsetwinsize(slave, (25 + offset, 81 + offset))
            os.kill(process.pid, signal.SIGWINCH)
        termios.tcsetwinsize(slave, (37, 117))
        os.kill(process.pid, signal.SIGWINCH)
        deadline = time.monotonic() + DEADLINE
        while json.loads(destination.read_text())[-1] != [117, 37]:
            self.assertLess(time.monotonic(), deadline, destination.read_text())
            time.sleep(0.01)
        os.write(master, b"q")
        self.assertEqual(process.wait(timeout=DEADLINE), 0)
        self.assertEqual(json.loads(destination.read_text())[-1], [117, 37])
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assertFalse(destination.with_name(destination.name + ".tmp").exists())

    def test_resize_confirmation_reads_final_kernel_size_without_a_signal(self):
        """Enter cannot release the next case using an earlier matching size pair."""
        _, host = self.terminal()
        child, native = self.terminal()
        termios.tcsetwinsize(host, (24, 80))
        with patch(
            "stream_host.termios.tcgetattr", return_value=termios.tcgetattr(host)
        ):
            runner = AutomaticRun(self.directory, False, require_resize=True)
        runner.fd = child
        get_size = termios.tcgetwinsize
        with (
            patch(
                "stream_host.termios.tcgetwinsize", side_effect=lambda _: get_size(host)
            ),
            patch.object(runner, "pump", side_effect=lambda: runner.host_input(b"\r")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runner.resize()
            runner._apply_resize()
            self.assertEqual(get_size(native), (24, 80))
            # Model a resize whose notification has not reached the host loop.
            termios.tcsetwinsize(host, (35, 110))
            runner.gate("resize-native-program")
        self.assertEqual(get_size(native), (35, 110))
        self.assertEqual(
            json.loads((self.directory / "host-size.json").read_text())["sizes"][-1],
            [110, 35],
        )
        self.assertFalse(runner.confirm_resize)


class HostMatrixCommandTests(unittest.TestCase):
    """Reject misleading host runs and preserve evidence through the public CLI."""

    def invoke(self, directory):
        """Use ordinary pipes so this invocation cannot masquerade as a real host."""
        return subprocess.run(
            [sys.executable, str(ROOT / "tests/stream_matrix.py"), str(directory)],
            input="",
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )

    def test_piped_execution_cannot_publish_host_success(self):
        """A nonterminal run fails before creating evidence or starting any shell."""
        with tempfile.TemporaryDirectory(prefix="ish-matrix-command-") as root:
            directory = Path(root) / "evidence"
            result = self.invoke(directory)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("real Linux/WSL host terminal", result.stderr)
            self.assertFalse(directory.exists())

    def test_retry_cannot_overwrite_previous_results(self):
        """Reusing a failed attempt's directory preserves all original evidence."""
        with tempfile.TemporaryDirectory(prefix="ish-matrix-command-") as root:
            directory = Path(root)
            previous = directory / "summary.json"
            content = b'[{"name":"zsh-plugins","status":"failed"}]\n'
            previous.write_bytes(content)
            result = self.invoke(directory)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("previous results are never overwritten", result.stderr)
            self.assertEqual(previous.read_bytes(), content)
            self.assertEqual(list(directory.iterdir()), [previous])


if __name__ == "__main__":
    unittest.main()
