"""Verify the native helper's literal socket contract in separate Python processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/ish/stream/request.py"
DEADLINE = 10
NONCE = "standalone-helper-0123456789abcdef"
NATIVE_INPUT = b"native stdin remains unread\x03\x04\n"


@unittest.skipUnless(os.name == "posix", "requires Unix sockets")
class HelperTests(unittest.TestCase):
    """Keep file/module startup, response bytes and native stdin independently visible."""

    def exchange(self, mode, response):
        """Act as a literal socket peer, without using the product wire definitions."""
        with tempfile.TemporaryDirectory(prefix="ish-helper-") as temporary:
            directory = Path(temporary)
            path = directory / "control"
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            environment.pop("PYTHONPATH", None)
            if mode == "file":
                # No installed packages or package search path may be required by
                # the command that a native shell integration actually executes.
                argv = [sys.executable, "-S", str(HELPER)]
            else:
                environment["PYTHONPATH"] = str(ROOT / "src")
                argv = [sys.executable, "-S", "-m", "ish.stream.request"]
            argv.extend(("--socket", str(path), "--nonce", NONCE, "--serial", "17"))
            with (
                socket.socket(socket.AF_UNIX) as listener,
                tempfile.TemporaryFile() as native,
            ):
                listener.bind(str(path))
                listener.listen()
                listener.settimeout(DEADLINE)
                native.write(NATIVE_INPUT)
                native.seek(0)
                process = subprocess.Popen(
                    argv,
                    cwd=directory,
                    env=environment,
                    stdin=native,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    with listener.accept()[0] as connection:
                        connection.settimeout(DEADLINE)
                        request = bytearray()
                        while not request.endswith(b"\n"):
                            part = connection.recv(4096)
                            self.assertTrue(
                                part, "helper closed before sending its request"
                            )
                            request.extend(part)
                        self.assertEqual(
                            json.loads(request), {"nonce": NONCE, "serial": 17}
                        )
                        connection.sendall(response)
                        connection.shutdown(socket.SHUT_WR)
                    stdout, stderr = process.communicate(timeout=DEADLINE)
                    self.assertEqual(stdout, b"")
                    self.assertEqual(stderr, b"")
                    self.assertEqual(native.tell(), 0)
                    self.assertEqual(native.read(), NATIVE_INPUT)
                    return process.returncode
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_completion_and_cancellation_release_the_native_helper(self):
        """Both documented replies end the native wait without touching its stdin."""
        for mode in ("file", "module"):
            for response in (b"done\n", b"cancel\n"):
                with self.subTest(mode=mode, response=response):
                    self.assertEqual(self.exchange(mode, response), 0)

    def test_unknown_partial_and_missing_responses_are_rejected(self):
        """A closed or invalid socket response cannot count as a normal release."""
        for mode in ("file", "module"):
            for response in (b"unknown\n", b"done", b"", b"\xff\n"):
                with self.subTest(mode=mode, response=response):
                    self.assertEqual(self.exchange(mode, response), 1)


if __name__ == "__main__":
    unittest.main()
