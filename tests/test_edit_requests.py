"""Verify editor grants independently and through the actual shell/PTK session."""

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ish.shell.edit import EditState
from ish.ui.prompt import Prompt

STARTUP_TIMEOUT = 30
INPUT_TIMEOUT = 8
SNAPSHOT = b"\x1b[19~"
STALE_REPLY = b"\x1b[20~"
PRIMARY = b"READY>"
EDIT_ON = b"\x1b[?2004h"
ROOT = Path(__file__).resolve().parents[1]


class EditImportTests(unittest.TestCase):
    """Verify contracts and the public runner work in independent cold processes."""

    def test_import_order_preserves_the_public_runner(self):
        """Contract, dispatch, and UI entry points must not depend on prior imports."""
        for module in ("ish.shell.request", "ish.app.dispatch", "ish.ui.prompt"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        f"import {module}\n"
                        "import ish.shell\n"
                        "assert 'InteractiveShell' in dir(ish.shell)\n"
                        "from ish.shell import InteractiveShell\n"
                        "from ish.shell.engine import InteractiveShell as Runner\n"
                        "assert InteractiveShell is Runner\n",
                    ],
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    timeout=STARTUP_TIMEOUT,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())


class EditStateTests(unittest.TestCase):
    """Use grant results, not implementation fields, to assert one-shot admission."""

    def test_replies_are_single_use_and_session_local(self):
        """Copies, foreign requests, and duplicates cannot release a grant."""
        state, other = EditState(), EditState()
        request = state.grant(7, commands=True)
        foreign = other.grant(7, commands=True)
        self.assertEqual((request.id, request.boundary, request.commands), (1, 7, True))
        self.assertFalse(state.take(replace(request)))
        self.assertFalse(state.take(foreign))
        self.assertIs(state.current, request)
        self.assertTrue(state.take(request))
        self.assertFalse(state.take(request))
        self.assertFalse(state.take(None))

    def test_cancelled_reply_cannot_consume_next_attempt_at_same_boundary(self):
        """A cancelled editor attempt differs from another attempt at the same prompt."""
        state = EditState()
        old = state.grant(7, commands=False)
        state.revoke()
        current = state.grant(7, commands=False)
        self.assertGreater(current.id, old.id)
        self.assertEqual(current.boundary, old.boundary)
        self.assertFalse(state.take(old))
        self.assertIs(state.current, current)
        self.assertTrue(state.take(current))

    def test_admission_and_policy_are_immutable(self):
        """No invalid boundary, overlapping editor, or mutated dispatch policy is admitted."""
        state = EditState()
        for boundary in (0, -1):
            with self.assertRaises(ValueError):
                state.grant(boundary, commands=True)
        request = state.grant(1, commands=False)
        with self.assertRaises(RuntimeError):
            state.grant(2, commands=True)
        with self.assertRaises(FrozenInstanceError):
            request.commands = True
        self.assertFalse(request.commands)


class EditAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real PTK replies against controlled session-boundary changes."""

    async def asyncSetUp(self):
        """Use a real editor and an explicitly established boundary fixture."""
        self.source = self.enterContext(create_pipe_input())
        self.enterContext(patch("ish.ui.prompt.get_builtins", return_value=[]))
        self.prompt = Prompt(shell="bash", input=self.source, output=DummyOutput())
        self.shell = self.prompt.interactive_shell
        self.shell._accepted_prompt_id = self.shell._prompt_id = (
            self.shell._context_id
        ) = 7
        self.shell.command_done_event = asyncio.Event()
        self.shell.command_done_event.set()
        self.shell.continuation_active = asyncio.Event()

    async def test_text_reply_consumes_its_grant(self):
        """The session accepts actual editor text and retires its request before return."""
        self.source.send_bytes(b"\x1b[200~  echo text  \x1b[201~\r")
        self.assertEqual(await self.shell._read_edit(), "  echo text  ")
        self.assertIsNone(self.shell.edit_request)

    async def test_invalidated_reply_cannot_dispatch(self):
        """A reply arriving after input revocation cannot execute an internal command."""

        def revoke():
            self.shell.invalidate_input()
            self.source.send_bytes(b"ish_exit\r")

        self.assertIsNone(await self.shell._read_edit(pre_run=revoke))
        self.assertIsNone(self.shell.edit_request)

    async def test_new_wire_boundary_rejects_previous_reply(self):
        """An accepted local request still needs its original shell boundary."""
        request = self.shell.begin_edit()
        self.shell._accepted_prompt_id = self.shell._prompt_id = (
            self.shell._context_id
        ) = 8
        self.assertFalse(self.shell.accept_edit(request))
        current = self.shell.begin_edit()
        self.assertEqual(current.boundary, 8)
        self.assertFalse(self.shell.accept_edit(request))
        self.assertTrue(self.shell.accept_edit(current))

    async def test_stale_editor_cleanup_preserves_a_new_request(self):
        """A late text reply and its cleanup cannot retire a replacement editor grant."""
        current = []

        def replace_request():
            self.shell.invalidate_input()
            current.append(self.shell.begin_edit())
            self.source.send_bytes(b"ish_exit\r")

        self.assertIsNone(await self.shell._read_edit(pre_run=replace_request))
        self.assertIs(self.shell.edit_request, current[0])
        self.assertTrue(self.shell.accept_edit(current[0]))

    async def test_prompt_needs_its_input_return_acknowledgement(self):
        """Prompt and context alone do not authorize editing before input is reclaimed."""
        self.shell.input_ack_pipe = 99
        self.shell._input_return_id = 6
        self.source.send_bytes(b"echo after-ack\r")
        with self.assertRaises(RuntimeError):
            await self.shell._read_edit()
        self.assertIsNone(self.shell.edit_request)
        self.shell._input_return_id = 7
        self.assertEqual(await self.shell._read_edit(), "echo after-ack")

    async def test_pending_writes_or_native_input_prevent_admission(self):
        """Matching prompt metadata alone cannot authorize an editor."""
        self.shell._writers[None] = SimpleNamespace(pending_bytes=1)
        with self.assertRaises(RuntimeError):
            self.shell.begin_edit()
        self.shell._writers.clear()
        self.shell._native_input_active = True
        with self.assertRaises(RuntimeError):
            self.shell.begin_edit()
        self.shell._native_input_active = False
        request = self.shell.begin_edit()
        self.shell._writers[None] = SimpleNamespace(pending_bytes=1)
        self.assertFalse(self.shell.accept_edit(request))

    async def test_eof_retires_the_attempt(self):
        """EOF keeps its existing public behavior and leaves no live editor grant."""
        self.source.send_bytes(b"\x04")
        with self.assertRaises(EOFError):
            await self.shell._read_edit()
        self.assertIsNone(self.shell.edit_request)

    async def test_closed_pty_waits_for_the_native_result(self):
        """A delayed child watcher keeps editing disabled after PTY EOF."""
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            await release.wait()
            self.shell.proc.returncode = 7
            return 7

        self.shell.proc = SimpleNamespace(returncode=None, wait=wait)
        self.shell._closing_output = True
        task = asyncio.create_task(self.shell._read_edit())
        try:
            await asyncio.wait_for(entered.wait(), INPUT_TIMEOUT)
            self.assertFalse(task.done())
            self.assertIsNone(self.shell.edit_request)
            release.set()
            self.assertIsNone(await asyncio.wait_for(task, INPUT_TIMEOUT))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class EditCLITests(unittest.TestCase):
    """Observe requests through public key bindings in a controlling PTY."""

    @contextlib.contextmanager
    def session(self, shell):
        """Start ish with isolated startup files and explicit editor witnesses."""
        executable = shutil.which(shell)
        if not executable:
            self.skipTest(f"{shell} is not installed")
        with tempfile.TemporaryDirectory(prefix="ish-edit-") as directory:
            root = Path(directory)
            for name in (".bashrc", ".zshrc", ".shrc"):
                (root / name).write_text("PS1='READY> '\n")
            for name in (".tcshrc", ".cshrc"):
                (root / name).write_text("set prompt='READY> '\n")
            (root / "ish").mkdir()
            (root / "ish/.ishrc.py").write_text(
                "import json\nfrom pathlib import Path\n"
                "saved = []\n"
                "def snapshot(event):\n"
                "    request = prompt.interactive_shell.edit_request\n"
                "    saved.append(request)\n"
                "    with open('requests', 'a') as output:\n"
                "        print(json.dumps([request.id, request.boundary, request.commands]), file=output)\n"
                "def stale(event):\n"
                "    accepted = prompt.interactive_shell.accept_edit(saved[0])\n"
                "    Path('stale').write_text(str(accepted))\n"
                "prompt.set_key('f8', handler=snapshot)\n"
                "prompt.set_key('f9', handler=stale)\n"
            )
            env = {
                "HOME": directory,
                "ZDOTDIR": directory,
                "ENV": str(root / ".shrc"),
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(directory)
                os.execve(
                    sys.executable,
                    [sys.executable, "-B", "-m", "ish.main", executable],
                    env,
                )
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            self.pid, self.fd, self.output, self.cpr = pid, fd, bytearray(), b""
            try:
                self.require(self.ready, STARTUP_TIMEOUT)
                yield root
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + INPUT_TIMEOUT
                while True:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                    if reaped:
                        break
                    if time.monotonic() >= deadline:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                        break
                    self.receive()
                os.close(fd)

    def receive(self):
        """Collect output and answer terminal cursor queries without estimating readiness."""
        if select.select([self.fd], [], [], 0.02)[0]:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            self.output.extend(chunk)
            combined = self.cpr + chunk
            self.cpr = combined[-3:]
            for _ in range(combined.count(b"\x1b[6n")):
                os.write(self.fd, b"\x1b[1;1R")

    def ready(self):
        """Observe the test prompt and PTK enable sequence; production uses wire ACKs."""
        return PRIMARY in self.output and EDIT_ON in self.output

    def require(self, predicate, timeout=INPUT_TIMEOUT):
        """Use time only as a test failure deadline."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.receive()
        self.assertTrue(predicate(), repr(bytes(self.output[-2500:])))

    def snapshot(self, root, count):
        """Ask the running PTK application to record its granted request."""
        os.write(self.fd, SNAPSHOT)
        path = root / "requests"
        self.require(
            lambda: path.exists() and len(path.read_text().splitlines()) == count
        )
        return [json.loads(line) for line in path.read_text().splitlines()][-1]

    def test_cancel_and_stale_reply_keep_the_next_command_intact(self):
        """A late editor reply after Ctrl+C cannot consume or execute the next command."""
        for shell in ("bash", "zsh", "tcsh", "dash"):
            with self.subTest(shell=shell), self.session(shell) as root:
                first = self.snapshot(root, 1)
                self.assertTrue(first[2])
                self.output.clear()
                os.write(self.fd, b"\x03")
                self.require(self.ready)
                second = self.snapshot(root, 2)
                self.assertGreater(second[0], first[0])
                self.assertEqual(second[1], first[1])
                os.write(self.fd, STALE_REPLY)
                self.require(lambda: (root / "stale").exists())
                self.assertEqual((root / "stale").read_text(), "False")
                self.output.clear()
                os.write(self.fd, b"echo intact > result\r")
                self.require(lambda: (root / "result").exists() and self.ready())
                self.assertEqual((root / "result").read_text(), "intact\n")
                third = self.snapshot(root, 3)
                self.assertGreater(third[0], second[0])
                self.assertGreater(third[1], second[1])

    def test_native_exit_keeps_its_result_without_another_editor(self):
        """Shell EOF can precede the child watcher; it must not trigger a failed grant."""
        for shell in ("tcsh", "csh", "bash", "zsh", "dash"):
            with self.subTest(shell=shell), self.session(shell):
                self.output.clear()
                os.write(self.fd, b"printf 'FINAL_TEXT\\n'; exit 7\r")
                observed = None

                def exited():
                    nonlocal observed
                    observed = os.waitid(
                        os.P_PID, self.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                    )
                    return observed is not None

                self.require(exited)
                self.receive()
                self.assertEqual(
                    (observed.si_code, observed.si_status),
                    (os.CLD_EXITED, 7),
                    repr(bytes(self.output[-3000:])),
                )
                self.assertIn(b"FINAL_TEXT\r\n", self.output)
                self.assertNotIn(EDIT_ON, self.output)


if __name__ == "__main__":
    unittest.main()
