"""Regressions for critical I/O, initialization, protocol, and Python worker defects."""

import asyncio
import base64
import errno
import fcntl
import multiprocessing
import os
import pty
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import Completion, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from ish.app.pytool import ProcessHandler
from ish.config import config
from ish.fdio import FDWriter
from ish.parser.shell import dict_parser
from ish.plugin.manager import PluginManager
from ish.shell.base import InteractiveShell
from ish.shell.integration import SHELL_INTEGRATION, TTY_FORWARD
from ish.shell.protocol import EOT, RS, SOH, US, VERSION, FrameDecoder
from ish.ui.prompt import Prompt
from p1_workers import exit_seven, large_output, terminal_echo, wait_forever


def frame(category, data, versioned=True):
    """Encode a test payload as a legacy or base64 ISH2 frame."""
    if versioned:
        return (
            SOH + VERSION + RS + category.encode() + US + base64.b64encode(data) + EOT
        )
    return SOH + category.encode() + US + data + EOT


class WriterTests(unittest.IsolatedAsyncioTestCase):
    """Verify FDWriter progress and ordering with partial writes, I/O errors, and slow
    consumers.
    """

    async def test_partial_eintr_and_eagain(self):
        """Retry remaining bytes in order after partial writes and EINTR or EAGAIN."""
        loop = Mock()
        loop.create_future = asyncio.get_running_loop().create_future
        writer = FDWriter(loop, 99)
        with patch(
            "ish.fdio.os.write",
            side_effect=[2, InterruptedError(), 1, BlockingIOError()],
        ) as write:
            writer.write(b"abcdef")
            self.assertEqual(
                [bytes(c.args[1]) for c in write.call_args_list],
                [b"abcdef", b"cdef", b"cdef", b"def"],
            )
        loop.add_writer.assert_called_once()
        with patch("ish.fdio.os.write", return_value=3):
            loop.add_writer.call_args.args[1]()
        await writer.drain()
        self.assertFalse(writer.pending)
        writer.close()

    async def test_bad_fd_does_not_retry(self):
        """Report permanent errors on a closed FD without retrying indefinitely."""
        writer = FDWriter(asyncio.get_running_loop(), 99)
        with patch(
            "ish.fdio.os.write", side_effect=OSError(errno.EBADF, "bad fd")
        ) as write:
            with self.assertRaises(OSError):
                writer.write(b"abc")
            self.assertEqual(write.call_count, 1)
        with self.assertRaises(OSError):
            await writer.drain()
        writer.close()

    async def test_zero_write_is_error(self):
        """Treat a zero-byte write as an error that prevents progress."""
        writer = FDWriter(asyncio.get_running_loop(), 99)
        with patch("ish.fdio.os.write", return_value=0):
            with self.assertRaises(OSError):
                writer.write(b"abc")
        writer.close()

    async def test_redirected_output_to_regular_file(self):
        """Write all output to regular files that cannot be watched with epoll."""
        with tempfile.TemporaryFile() as file:
            writer = FDWriter(asyncio.get_running_loop(), file.fileno())
            try:
                writer.write(b"x" * 200000)
                await writer.drain()
                file.seek(0)
                self.assertEqual(file.read(), b"x" * 200000)
            finally:
                writer.close()

    async def test_backpressure_keeps_loop_alive_and_preserves_order(self):
        """Keep the event loop responsive and preserve output order with a slow consumer."""
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        loop = asyncio.get_running_loop()
        writer = FDWriter(loop, w)
        received = bytearray()
        payload = bytes(range(256)) * 1000
        ticked = []

        def read():
            """Collect received bytes in the asynchronous output test's observation buffer."""
            received.extend(os.read(r, 65536))

        try:
            writer.write(payload[:100000])
            writer.write(payload[100000:])
            self.assertTrue(writer.pending)
            loop.call_soon(ticked.append, True)
            await asyncio.sleep(0.01)
            self.assertTrue(ticked)
            loop.add_reader(r, read)
            await asyncio.wait_for(writer.drain(), 2)
            while len(received) < len(payload):
                await asyncio.sleep(0.001)
            self.assertEqual(bytes(received), payload)
        finally:
            loop.remove_reader(r)
            writer.close()
            os.close(r)
            os.close(w)


class ProtocolTests(unittest.TestCase):
    """Verify legacy and ISH2 frame merging, splitting, encoding, and environment
    preservation.
    """

    def test_coalesced_legacy_frames(self):
        """Consume all legacy frames combined in a single read."""
        decoder = FrameDecoder()
        self.assertEqual(
            decoder.feed(
                frame("exitcode", b"1", False) + frame("exitcode", b"2", False)
            ),
            [("exitcode", b"1"), ("exitcode", b"2")],
        )
        self.assertFalse(decoder.buffer)

    def test_every_split_and_control_bytes(self):
        """Preserve separators and newlines in environment values at every receive split."""
        payload = b"A=line1\nline2\x01\x04\x1e\x1f\0B=other\0"
        packet = frame("environ", payload) + frame("exitcode", b"0")
        for size in (1, 2, 7, len(packet)):
            decoder = FrameDecoder()
            result = []
            for i in range(0, len(packet), size):
                result.extend(decoder.feed(packet[i : i + size]))
            self.assertEqual(result, [("environ", payload), ("exitcode", b"0")])

    def test_partial_tail_is_preserved(self):
        """Retain the incomplete tail after a complete frame for the next read."""
        a, b = frame("exitcode", b"1"), frame("exitcode", b"2")
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(a + b[:-1]), [("exitcode", b"1")])
        self.assertEqual(bytes(decoder.buffer), b[:-1])
        self.assertEqual(decoder.feed(b[-1:]), [("exitcode", b"2")])

    def test_size_limit_and_invalid_encoding(self):
        """Reject oversized frames and invalid base64 instead of silently accepting them."""
        with self.assertRaises(ValueError):
            FrameDecoder(max_frame_bytes=8).feed(SOH + b"x" * 9)
        with self.assertRaises(ValueError):
            FrameDecoder().feed(SOH + VERSION + RS + b"exitcode" + US + b"!!!" + EOT)

    def test_bash_emits_unambiguous_environment(self):
        """Round-trip newlines and control characters in environment values from real Bash."""
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "integration.sh"
            output = Path(d) / "context"
            script.write_text(SHELL_INTEGRATION["shellIntegraion.sh"])
            value = "line1\nline2\x01\x04\x1e\x1f"
            subprocess.run(
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    'PS1="test$ "; source "$1" "$2" /dev/null; _ish_update 7',
                    "test",
                    str(script),
                    str(output),
                ],
                env={**os.environ, "P1_VALUE": value},
                check=True,
                timeout=3,
            )
            records = dict(FrameDecoder().feed(output.read_bytes()))
            self.assertEqual(records["exitcode"], b"7")
            self.assertEqual(
                dict_parser(records["environ"], "utf-8", "=", "\0")["P1_VALUE"], value
            )


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    """Verify event-loop progress during initialization and resource restoration after
    failure or cancellation.
    """

    async def test_init_yields_and_waits_for_prompt(self):
        """Run scheduled event-loop callbacks while waiting for the initial prompt."""
        shell = InteractiveShell("bash")
        shell.loop = asyncio.get_running_loop()
        shell._init_event = asyncio.Event()
        shell._fatal_error = shell.loop.create_future()
        shell.proc = SimpleNamespace(wait=AsyncMock(side_effect=asyncio.Event().wait))
        ticks = []
        shell.loop.call_later(0.005, ticks.append, True)
        shell.loop.call_later(0.02, shell._init_event.set)
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        with (
            patch.object(InteractiveShell, "_send", new=AsyncMock()),
            patch.object(InteractiveShell, "_writer", return_value=writer),
        ):
            await shell._init(b"source test\n", 0.2)
        self.assertTrue(ticks)
        self.assertFalse(shell._initializing)

    async def test_init_timeout_and_child_exit(self):
        """Report initialization timeout and early shell exit as distinct, explainable
        failures.
        """
        for exited in (False, True):
            shell = InteractiveShell("bash")
            shell.loop = asyncio.get_running_loop()
            shell._init_event = asyncio.Event()
            shell._fatal_error = shell.loop.create_future()
            shell.proc = SimpleNamespace(
                wait=AsyncMock(return_value=3)
                if exited
                else AsyncMock(side_effect=asyncio.Event().wait)
            )
            with patch.object(InteractiveShell, "_send", new=AsyncMock()):
                with self.assertRaisesRegex(
                    RuntimeError, "exited" if exited else "timed out"
                ):
                    await shell._init(b"test\n", 0.02)

    async def test_cleanup_on_spawn_failure_init_failure_and_cancel(self):
        """Restore FDs, terminal settings, and resize state after failure or cancellation
        at each acquisition stage.
        """
        for stage in ("fifo", "spawn", "init", "cancel"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                master, slave = pty.openpty()
                flags = fcntl.fcntl(slave, fcntl.F_GETFL)
                attrs = termios.tcgetattr(slave)
                app = SimpleNamespace(_on_resize=Mock())
                original_resize = app._on_resize
                shell = InteractiveShell(
                    "bash",
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    prompt=SimpleNamespace(app=app, set_prompt=Mock()),
                )
                shell._environ = {"HOME": d, "PATH": "/usr/bin:/bin", "TERM": "xterm"}
                entered = asyncio.Event()

                async def init(*args, entered=entered, stage=stage, **kwargs):
                    """Fail initialization at the selected stage or wait at the
                    cancellation barrier.
                    """
                    entered.set()
                    if stage == "cancel":
                        await asyncio.Event().wait()
                    raise RuntimeError("injected init failure")

                try:
                    with (
                        patch("ish.shell.base.build_binary", return_value=True),
                        patch("ish.shell.base.install_scripts", return_value=Path(d)),
                        patch.object(InteractiveShell, "_init", side_effect=init),
                    ):
                        if stage == "fifo":
                            original_mkfifo = os.mkfifo
                            calls = []

                            def mkfifo(
                                path, mode, calls=calls, original_mkfifo=original_mkfifo
                            ):
                                """Fail the second FIFO creation to verify partial resource
                                cleanup.
                                """
                                calls.append(path)
                                if len(calls) == 2:
                                    raise OSError("injected FIFO failure")
                                return original_mkfifo(path, mode)

                            with patch("ish.shell.base.os.mkfifo", side_effect=mkfifo):
                                with self.assertRaises(OSError):
                                    await shell.main()
                        elif stage == "spawn":
                            with patch.object(
                                InteractiveShell,
                                "_spawn",
                                new=AsyncMock(
                                    side_effect=RuntimeError("injected spawn failure")
                                ),
                            ):
                                with self.assertRaises(RuntimeError):
                                    await shell.main()
                        elif stage == "init":
                            with self.assertRaises(RuntimeError):
                                await shell.main()
                        else:
                            task = asyncio.create_task(shell.main())
                            await asyncio.wait_for(entered.wait(), 2)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                    for name in (
                        "master_fd",
                        "slave_fd",
                        "dupin_fd",
                        "shell_pipe",
                        "tty_pipe",
                    ):
                        self.assertIsNone(getattr(shell, name), name)
                    self.assertFalse(Path(shell.shell_pipe_path).exists())
                    self.assertFalse(Path(shell.tty_pipe_path).exists())
                    self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL), flags)
                    self.assertEqual(termios.tcgetattr(slave), attrs)
                    self.assertIs(app._on_resize, original_resize)
                    if shell.proc:
                        self.assertIsNotNone(shell.proc.returncode)
                finally:
                    os.close(master)
                    os.close(slave)


class AlreadyFixedTests(unittest.IsolatedAsyncioTestCase):
    """Guard previously fixed Tab-completion and plugin-handling behavior against
    regressions.
    """

    async def test_tab_starts_async_completion_and_applies_selection(self):
        """Start asynchronous candidate generation with Tab and apply the selected
        candidate correctly.
        """
        owner = SimpleNamespace(_usage_counter=Counter())
        bindings = Prompt._create_key_binding(owner)
        tab = next(b.handler for b in bindings.bindings if b.keys == (Keys.Tab,))
        buffer = Buffer(completer=WordCompleter(["ls"]))
        event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
        tab(event)
        for _ in range(50):
            if buffer.complete_state:
                break
            await asyncio.sleep(0.005)
        self.assertIsNotNone(buffer.complete_state)
        buffer.set_document(Document("l"))
        buffer.complete_state = CompletionState(
            Document("l"), [Completion("ls ", start_position=-1)], complete_index=0
        )
        tab(event)
        self.assertEqual(buffer.text, "ls ")

    async def test_plugin_parse_and_registration_and_unload(self):
        """Parse requirements, register, unload, and reload a minimal plugin."""
        manager = PluginManager()
        self.assertEqual(
            manager._parse_requirement("pkg>=1|import_name"),
            ("pkg", ">=1", "import_name"),
        )
        with tempfile.TemporaryDirectory() as d:
            for entry in ("__init__.py", "p1_plugin.py"):
                plugin = Path(d) / "p1_plugin"
                plugin.mkdir(exist_ok=True)
                source = plugin / entry
                source.write_text(
                    'PLUGIN_META = {"version": "1.0"}\ndef tool(): return 42\n'
                )
                info = manager._resolve(plugin)
                self.assertEqual(manager.get("p1_plugin").tool(), 42)
                module_name = info.module.__name__
                manager._unload_plugin("p1_plugin")
                self.assertIsNone(manager.get("p1_plugin"))
                self.assertNotIn(module_name, sys.modules)
                source.unlink()

    async def test_bad_plugin_does_not_hide_original_exception(self):
        """Preserve the original exception while cleaning up a failed plugin."""
        original_home = config._ish_home
        original_path = sys.path[:]
        try:
            with tempfile.TemporaryDirectory() as d:
                config.ISH_HOME = d
                config.ensure_directories()
                plugin = config.PLUGIN_SCRIPT_DIR / "p1_broken"
                plugin.mkdir()
                (plugin / "__init__.py").write_text(
                    'PLUGIN_META = {}\nraise RuntimeError("original failure")\n'
                )
                manager = PluginManager()
                manager.logger = Mock()
                self.assertEqual(manager.load(), [])
                self.assertIn(
                    "original failure", manager.logger.error.call_args.args[0]
                )
        finally:
            config._ish_home = original_home
            sys.path[:] = original_path


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    """Verify spawned workers' controlling terminals, output draining, cancellation, and FD
    lifetimes.
    """

    async def run_tool(self, func, data=b"", cancel=False):
        """Connect a separate PTY and output collector and return the Python tool's
        results.
        """
        input_r, input_w = os.pipe()
        output_r, output_w = os.pipe()
        os.set_blocking(output_r, False)
        if data:
            os.write(input_w, data)
        if not cancel:
            os.close(input_w)
            input_w = None
        flags = fcntl.fcntl(input_r, fcntl.F_GETFL)
        received = bytearray()
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()

        def read():
            """Collect received bytes in the asynchronous output test's observation buffer."""
            chunk = os.read(output_r, 65536)
            received.extend(chunk)
            if b"WORKER_READY" in received:
                ready.set()

        loop.add_reader(output_r, read)
        task = asyncio.create_task(
            ProcessHandler(stdin=input_r, stdout=output_w).run(func)
        )
        try:
            if cancel:
                await asyncio.wait_for(ready.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                result = None
            else:
                result = await asyncio.wait_for(task, 8)
            await asyncio.sleep(0.01)
            self.assertEqual(fcntl.fcntl(input_r, fcntl.F_GETFL), flags)
            self.assertFalse(multiprocessing.active_children())
            return result, bytes(received)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            loop.remove_reader(output_r)
            for fd in (input_r, input_w, output_r, output_w):
                if fd is not None:
                    os.close(fd)

    async def test_spawn_receives_controlling_terminal_and_input(self):
        """Give a spawned worker a real controlling PTY and user input."""
        code, output = await self.run_tool(terminal_echo, b"hello\n")
        self.assertEqual(code, 0)
        self.assertIn(b"TTY True True", output)
        self.assertIn(b"INPUT:hello", output)

    async def test_final_output_is_drained_after_process_exit(self):
        """Retain the tail of large output even when it becomes readable after worker exit."""
        code, output = await self.run_tool(large_output)
        self.assertEqual(code, 0)
        self.assertEqual(output.count(b"x"), 200000)
        self.assertIn(b"END_OF_TOOL", output)

    async def test_exit_status_and_cancellation(self):
        """Propagate tool exit codes and reap cancelled workers."""
        code, _ = await self.run_tool(exit_seven)
        self.assertEqual(code, 7)
        await self.run_tool(wait_forever, cancel=True)

    async def test_local_callable_fails_before_allocating_pty(self):
        """Reject unpickleable local functions before acquiring PTY resources."""
        with patch("ish.app.pytool.pty.openpty") as openpty:
            with self.assertRaisesRegex(TypeError, "importable module"):
                await ProcessHandler().run(lambda: None)
            openpty.assert_not_called()

    async def test_start_failure_closes_resources_and_restores_flags(self):
        """Clean up open FDs and modified flags after worker startup failure."""
        r, w = os.pipe()
        try:
            flags = fcntl.fcntl(r, fcntl.F_GETFL)
            before = len(os.listdir("/proc/self/fd"))
            with patch(
                "multiprocessing.process.BaseProcess.start",
                side_effect=RuntimeError("spawn failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "spawn failure"):
                    await ProcessHandler(stdin=r, stdout=w).run(print)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
            self.assertEqual(fcntl.fcntl(r, fcntl.F_GETFL), flags)
        finally:
            os.close(r)
            os.close(w)


class CForwardTests(unittest.TestCase):
    """Verify that the real C forwarding tool preserves typeahead when the FIFO is full."""

    def test_full_fifo_is_retried_without_losing_input(self):
        """Retry a full FIFO in the C helper without losing typeahead bytes."""
        with tempfile.TemporaryDirectory() as d:
            source, binary, fifo = [
                Path(d) / name for name in ("forward.c", "forward", "input.fifo")
            ]
            source.write_text(TTY_FORWARD)
            subprocess.run(
                [
                    "gcc",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-O2",
                    "-o",
                    str(binary),
                    str(source),
                ],
                check=True,
                timeout=10,
            )
            os.mkfifo(fifo)
            pipe = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            capacity = fcntl.fcntl(pipe, fcntl.F_GETPIPE_SZ)
            os.write(pipe, b"a" * capacity)
            master, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ICANON
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            os.write(master, b"pending-input")
            process = subprocess.Popen(
                [str(binary), str(fifo)], stdin=slave, stdout=subprocess.DEVNULL
            )
            try:
                time.sleep(0.05)
                self.assertIsNone(process.poll())
                self.assertEqual(os.read(pipe, capacity), b"a" * capacity)
                process.wait(timeout=2)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(os.read(pipe, 4096), b"pending-input")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for fd in (pipe, master, slave):
                    os.close(fd)


if __name__ == "__main__":
    unittest.main()
