"""Verify native terminal transport completion and failure using actual PTYs."""

import asyncio
import fcntl
import os
import select
import sys
import termios
import unittest
from unittest.mock import patch

from ish.lang import i18n
from ish.runtime.fdio import FDWriter
from ish.terminal.session import Session

DEADLINE = 5
OVERSIZED_INPUT = 5 * 1024 * 1024
FINAL_OUTPUT = b"x" * 10000 + b"FINAL_MARKER"
HOST_PIPE_BYTES = 4096
HOST_QUEUE_BYTES = 32768


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

    async def test_language_pack_controls_transport_failures(self):
        """Report translated queue errors without altering native bytes or delivery."""
        messages = dict(i18n.message)
        self.addCleanup(setattr, i18n, "message", messages)
        i18n.update(
            {
                "writer_closed": "닫힌 출력 경로",
                "input_limit": "시작 입력 한도 초과",
            }
        )
        writer = FDWriter(asyncio.get_running_loop(), -1)
        writer.close()
        with self.assertRaisesRegex(RuntimeError, "닫힌 출력 경로"):
            writer.write(b"untouched native bytes")
        self.session = Session(
            [], os.getcwd(), self.received, self.exited, self.errors.append
        )
        self.session.send(b"x" * OVERSIZED_INPUT)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(str(self.errors[0]), "시작 입력 한도 초과")
        self.assertEqual(self.session.pending_input, b"")

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

    async def require_child_exit(self):
        """Observe the real child's exit separately from output completion."""

        async def wait():
            while self.session.returncode is None:
                await asyncio.sleep(0)

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
            "size=os.get_terminal_size(0)\n"
            "print(f'{size.columns}x{size.lines} READY',flush=True)\n"
            "value=sys.stdin.readline()\n"
            "os.write(1,b'FINAL:'+value.encode())\n"
        )
        await self.require_output(b"80x24 READY\r\n")
        self.session.send("native 한 글\n".encode())
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertIn("FINAL:native 한 글\r\n".encode(), self.output)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])

    async def test_immediate_resize_survives_delayed_child_setup(self):
        """The parent sees the initial size and the shell inherits the newest size."""
        reader, release = os.pipe()
        chdir = os.chdir

        def delayed_chdir(path):
            os.close(release)
            os.read(reader, 1)
            os.close(reader)
            chdir(path)

        try:
            with patch("ish.terminal.session.os.chdir", delayed_chdir):
                self.start(
                    "import os\n"
                    "assert os.getsid(0) == os.getpid() == os.tcgetpgrp(0)\n"
                    "print('SIZE', *os.get_terminal_size(0))\n"
                )
            self.assertEqual(termios.tcgetwinsize(self.session.fd), (24, 80))
            self.session.resize(101, 33)
        finally:
            os.close(reader)
            os.close(release)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertEqual(self.output, b"SIZE 101 33\r\n")
        self.assertEqual(self.exits, [0])

    async def test_fullscreen_burst_reannounces_the_final_real_pty_size_once(self):
        """A foreground job can resample a final size whose first notification it missed."""
        self.start(
            "import os,signal\n"
            "signals={signal.SIGWINCH}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK,signals)\n"
            "print('READY',flush=True)\n"
            "for n in range(3):\n"
            " signal.sigwait(signals)\n"
            " print(n,*os.get_terminal_size(),flush=True)\n"
        )
        await self.require_output(b"READY\r\n")
        self.session.resize(90, 26, confirm_burst=True)
        await self.require_output(b"0 90 26\r\n")
        self.session.resize(101, 33, confirm_burst=True)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertEqual(
            self.output.splitlines(), [b"READY", b"0 90 26", b"1 101 33", b"2 101 33"]
        )
        self.assertEqual(self.exits, [0])

    async def test_settled_resize_does_not_signal_a_replacement_foreground_job(self):
        """A new job can acquire the controlling PTY before the resize burst settles."""
        self.start(
            "import os,signal\n"
            "signal.signal(signal.SIGWINCH,lambda *_: print('OLD',flush=True))\n"
            "print('READY',flush=True)\n"
            "input()\n"
            "pid=os.fork()\n"
            "if pid: os.waitpid(pid,0)\n"
            "else:\n"
            " signal.signal(signal.SIGTTOU,signal.SIG_IGN)\n"
            " os.setpgid(0,0)\n"
            " signal.signal(signal.SIGWINCH,lambda *_: print('NEW',flush=True))\n"
            " os.tcsetpgrp(0,os.getpgrp())\n"
            " print('SWITCHED',flush=True)\n"
            " input()\n"
        )
        await self.require_output(b"READY\r\n")
        self.session.resize(88, 25, confirm_burst=True)
        await self.require_output(b"OLD\r\n")
        self.session.resize(96, 26, confirm_burst=True)
        self.session.send(b"\n")
        await self.require_output(b"SWITCHED\r\n")
        await asyncio.sleep(self.session.RESIZE_SETTLE_SECONDS + 0.05)
        self.session.send(b"\n")
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        tail = self.output.split(b"SWITCHED\r\n")[1]
        self.assertNotIn(b"OLD", tail)
        self.assertNotIn(b"NEW", tail)
        self.assertEqual(self.exits, [0])

    async def test_failed_fork_closes_setup_pipe_and_can_be_retried(self):
        """A spawn failure releases both ends of the initialization boundary."""
        reader, writer = os.pipe()
        self.session = Session(
            [sys.executable, "-c", "print('started')"],
            os.getcwd(),
            self.received,
            self.exited,
            self.errors.append,
        )
        with (
            patch("ish.terminal.session.os.pipe", return_value=(reader, writer)),
            patch("ish.terminal.session.pty.fork", side_effect=BlockingIOError),
            self.assertRaises(BlockingIOError),
        ):
            self.session.start(80, 24)
        for fd in (reader, writer):
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.session.start(80, 24)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertEqual(self.output, b"started\r\n")
        self.assertEqual(self.exits, [0])

    async def test_failed_initial_size_delivers_child_error_and_exit(self):
        """A child exiting before geometry is ready must release its parent."""
        with patch(
            "ish.terminal.session.termios.tcsetwinsize",
            side_effect=OSError("size error"),
        ):
            self.start("raise AssertionError('child should not exec')")
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertIn(b"size error", self.output)
        self.assertNotIn(b"AssertionError", self.output)
        self.assertEqual(self.exits, [127])

    async def test_immediate_close_reaps_child_before_exec(self):
        """The child's foreground group already exists when start returns."""
        reader, release = os.pipe()

        def delayed_chdir(_):
            os.close(release)
            os.read(reader, 1)
            os._exit(99)

        try:
            with patch("ish.terminal.session.os.chdir", delayed_chdir):
                self.start("raise AssertionError('child should not exec')")
            await asyncio.wait_for(self.session.close(), DEADLINE)
            self.assertLess(self.session.returncode, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(self.session.pid, os.WNOHANG)
        finally:
            os.close(reader)
            os.close(release)

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

    async def test_paused_final_output_precedes_completion(self):
        """Backpressure cannot discard final bytes when the real child exits."""
        self.start("import os\nos.write(1," + repr(FINAL_OUTPUT) + ")\n")

        def pause(data):
            self.received(data)
            self.session.set_output_enabled(False)

        self.session.output = pause
        await self.require_child_exit()
        self.assertFalse(self.completed.is_set())
        self.assertLess(len(self.output), len(FINAL_OUTPUT))
        self.session.output = self.received
        self.session.set_output_enabled(True)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertEqual(bytes(self.output), FINAL_OUTPUT)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])

    async def test_start_paused_retains_final_output_without_readable_spin(self):
        """A pre-start geometry wait must not repeatedly dispatch a readable PTY."""
        self.session = Session(
            [sys.executable, "-c", "import os; os.write(1, b'PAUSED_FINAL')"],
            os.getcwd(),
            self.received,
            self.exited,
            self.errors.append,
        )
        reads = []
        original = self.session._read

        def read():
            reads.append(None)
            original()

        self.session._read = read
        self.session.set_output_enabled(False)
        self.session.start(80, 24)
        await self.require_child_exit()
        self.assertLessEqual(len(reads), 1)  # The child-exit final pass may probe once.
        self.assertEqual(self.output, b"")
        self.assertFalse(self.completed.is_set())
        self.session.set_output_enabled(True)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertEqual(self.output, b"PAUSED_FINAL")
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])

    async def test_host_writer_backpressure_preserves_final_output(self):
        """A real blocked host pipe resumes without losing the child's last bytes."""
        loop = asyncio.get_running_loop()
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        os.set_blocking(writer, False)
        fcntl.fcntl(writer, fcntl.F_SETPIPE_SZ, HOST_PIPE_BYTES)
        payload = FINAL_OUTPUT * 3
        delivered = bytearray()
        host_done = asyncio.Event()
        child_done = asyncio.Event()
        flow = []

        def changed(paused):
            flow.append(paused)
            self.session.set_output_enabled(not paused)

        host = FDWriter(
            loop,
            writer,
            self.errors.append,
            max_pending_bytes=HOST_QUEUE_BYTES,
            on_flow=changed,
        )

        def receive(data):
            self.received(data)
            host.write(data)

        def consume():
            while True:
                try:
                    data = os.read(reader, 65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                delivered.extend(data)
            if len(delivered) >= len(payload):
                host_done.set()

        try:
            self.start("import os\nos.write(1," + repr(payload) + ")\n")
            self.session.output = receive
            self.session.child_exited = lambda code: child_done.set()
            await asyncio.wait_for(child_done.wait(), DEADLINE)
            self.assertIn(True, flow)
            self.assertFalse(self.completed.is_set())
            loop.add_reader(reader, consume)
            await asyncio.wait_for(self.completed.wait(), DEADLINE)
            await asyncio.wait_for(host.drain(), DEADLINE)
            await asyncio.wait_for(host_done.wait(), DEADLINE)
            self.assertEqual(bytes(delivered), payload)
            self.assertEqual(bytes(self.output), payload)
            self.assertEqual(self.exits, [0])
            self.assertEqual(self.errors, [])
        finally:
            loop.remove_reader(reader)
            host.close()
            os.close(reader)
            os.close(writer)

    async def test_paused_exit_with_open_slave_drains_without_waiting_for_eof(self):
        """A retained slave cannot prolong completion after final bytes are delivered."""
        self.start(
            "import os,sys\n"
            "print('SLAVE='+os.ttyname(0),flush=True)\n"
            "sys.stdin.readline()\n"
            "os.write(1," + repr(FINAL_OUTPUT) + ")\n"
        )
        await self.require_output(b"\r\n")
        line = bytes(self.output).split(b"\r\n", 1)[0]
        self.slave = os.open(
            os.fsdecode(line.removeprefix(b"SLAVE=")), os.O_RDWR | os.O_NOCTTY
        )
        self.session.set_output_enabled(False)
        self.session.send(b"finish\n")
        await self.require_child_exit()
        self.assertFalse(self.completed.is_set())
        self.session.set_output_enabled(True)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertTrue(self.output.endswith(FINAL_OUTPUT))
        self.assertEqual(bytes(self.output).count(FINAL_OUTPUT), 1)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.errors, [])
        os.fstat(self.slave)

    async def test_close_releases_paused_exit_without_reporting_completion(self):
        """Forced close cancels final delivery instead of hanging on paused output."""
        self.start("import os\nos.write(1," + repr(FINAL_OUTPUT) + ")\n")

        def pause(data):
            self.received(data)
            self.session.set_output_enabled(False)

        self.session.output = pause
        await self.require_child_exit()
        await asyncio.wait_for(self.session.close(), DEADLINE)
        self.assertEqual(self.exits, [])
        self.assertEqual(self.errors, [])

    async def test_child_exit_discards_input_queued_for_a_retained_slave(self):
        """Host draining must not deliver pre-exit queued input to another consumer."""
        self.start(
            "import os,sys,tty\n"
            "tty.setraw(0)\n"
            "print('SLAVE='+os.ttyname(0),flush=True)\n"
            "sys.stdin.readline()\n"
            "os.write(1," + repr(FINAL_OUTPUT) + ")\n"
        )
        await self.require_output(b"\n")
        line = bytes(self.output).split(b"\n", 1)[0]
        self.slave = os.open(
            os.fsdecode(line.removeprefix(b"SLAVE=")), os.O_RDWR | os.O_NOCTTY
        )
        self.session.set_output_enabled(False)
        self.session.send(b"finish\n" + b"q" * (1024 * 1024))
        await self.require_child_exit()
        self.assertFalse(self.completed.is_set())
        self.assertTrue(self.session.writer.closed)
        os.set_blocking(self.slave, False)
        # Empty bytes already accepted by the terminal before process exit.
        while select.select((self.slave,), (), (), 0)[0]:
            os.read(self.slave, 65536)
        # Let ready callbacks run: an uncancelled native writer would now send
        # its queued suffix into the independently retained slave.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with self.assertRaises(BlockingIOError):
            os.read(self.slave, 65536)
        self.session.set_output_enabled(True)
        await asyncio.wait_for(self.completed.wait(), DEADLINE)
        self.assertTrue(self.output.endswith(FINAL_OUTPUT))
        self.assertEqual(self.exits, [0])

    async def test_child_exit_callback_failure_releases_completion_wait(self):
        """A lifecycle observer failure reports failure without a hanging drain."""

        def failed_observer(code):
            raise RuntimeError("broken child observer")

        self.start("print('FINAL')")
        self.session.child_exited = failed_observer
        await asyncio.wait_for(self.session.waiter, DEADLINE)
        self.assertEqual(self.exits, [])
        self.assertEqual(
            [str(error) for error in self.errors], ["broken child observer"]
        )

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
