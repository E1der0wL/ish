"""Verify native terminal transport completion and failure using actual PTYs."""

import asyncio
import os
import sys
import unittest

from ish.terminal.session import Session

DEADLINE = 5
OVERSIZED_INPUT = 5 * 1024 * 1024


@unittest.skipUnless(os.name == "posix", "A POSIX controlling PTY is required")
class TerminalSessionTests(unittest.IsolatedAsyncioTestCase):
    """Observe real child output and completion without requiring a VT binding."""

    async def asyncSetUp(self):
        """Keep each test's PTY, child, and optional retained slave independently owned."""
        self.output = bytearray()
        self.output_changed = asyncio.Event()
        self.completed = asyncio.Event()
        self.exits = []
        self.errors = []
        self.session = None
        self.slave = None
        self.addAsyncCleanup(self.stop)

    def start(self, script):
        """Execute one transient native process on the production controlling PTY."""
        self.session = Session(
            [sys.executable, "-u", "-c", script],
            os.getcwd(),
            self.received,
            self.exited,
            self.errors.append,
        )
        self.session.start(80, 24)

    def received(self, data):
        """Record only bytes actually returned by the child PTY."""
        self.output.extend(data)
        self.output_changed.set()

    def exited(self, code):
        """Record the externally delivered completion once it arrives."""
        self.exits.append(code)
        self.completed.set()

    async def require_output(self, marker):
        """Use a deadline solely to fail a missing observable child result."""

        async def wait():
            while marker not in self.output:
                self.output_changed.clear()
                await self.output_changed.wait()

        await asyncio.wait_for(wait(), DEADLINE)

    async def stop(self):
        """Close every extra slave handle before stopping and reaping the owned child."""
        if self.slave is not None:
            os.close(self.slave)
            self.slave = None
        if self.session is not None:
            await self.session.close()

    async def test_native_input_and_final_output_precede_completion(self):
        """Native stdin and the last output bytes survive immediate child termination."""
        self.start(
            "import os,sys\n"
            "print('READY',flush=True)\n"
            "value=sys.stdin.readline()\n"
            "os.write(1,b'FINAL:'+value.encode())\n"
        )
        await self.require_output(b"READY\r\n")
        self.session.send("native 한 글\n".encode())
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertIn("FINAL:native 한 글\r\n".encode(), self.output)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])

    async def test_direct_child_completion_does_not_wait_for_other_slave_handles(self):
        """A retained slave cannot keep the UI alive after its actual child exits."""
        self.start(
            "import os,sys\n"
            "print('SLAVE='+os.ttyname(0),flush=True)\n"
            "sys.stdin.readline()\n"
            "os.write(1,b'FINAL_WITH_OPEN_SLAVE\\n')\n"
        )
        await self.require_output(b"\r\n")
        line = bytes(self.output).split(b"\r\n", 1)[0]
        self.assertTrue(line.startswith(b"SLAVE=/dev/"), line)
        # The extra handle reproduces a background process retaining the PTY,
        # without creating an orphan that the test cannot itself reap.
        self.slave = os.open(
            os.fsdecode(line.removeprefix(b"SLAVE=")), os.O_RDWR | os.O_NOCTTY
        )
        self.session.send(b"finish\n")
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertIn(b"FINAL_WITH_OPEN_SLAVE\r\n", self.output)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])
        os.fstat(self.slave)
        # A retained slave may belong to a background process. While the UI
        # drains host protocol replies after child exit, it must not grant that
        # stale consumer fresh keyboard or clipboard-response input.
        os.set_blocking(self.slave, False)
        self.session.send(b"AFTER_CHILD_EXIT\n")
        with self.assertRaises(BlockingIOError):
            os.read(self.slave, 128)

    async def test_oversized_input_reports_transport_failure(self):
        """A rejected paste reaches the failure callback instead of escaping a key handler."""
        self.start("import sys\nprint('READY',flush=True)\nsys.stdin.readline()\n")
        await self.require_output(b"READY\r\n")
        self.session.send(b"x" * OVERSIZED_INPUT)
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], BufferError)

    async def test_typeahead_before_start_reaches_native_stdin_once(self):
        """Input during host negotiation is retained by the transport owner."""
        self.session = Session(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('CONSUMED:'+repr(sys.stdin.readline()))",
            ],
            os.getcwd(),
            self.received,
            self.exited,
            self.errors.append,
        )
        self.session.send(b"early ")
        self.session.send("한 글\n".encode())
        self.session.start(80, 24)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        expected = "CONSUMED:'early 한 글\\n'".encode()
        self.assertEqual(bytes(self.output).count(expected), 1)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
