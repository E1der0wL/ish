"""External text providers: literal wire results and isolated X11 interoperability.

The tests never connect to the user's DISPLAY or Windows clipboard. Windows
commands use temporary Python pipe fixtures; X11 uses a dedicated Xvfb server
and independent xclip client. Missing Xvfb/xclip is a failure, not a passed skip.
"""

import asyncio
import base64
import errno
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.terminal_plugins import clipboard_system as sample

from ish.terminal.clipboard import MAX_TEXT, ClipboardBroker
from ish.terminal.host import HostProfile

ST = b"\x1b\\"
ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "examples" / "terminal_plugins"
TEXT_MIME = b"dGV4dC9wbGFpbg=="


class MemoryClipboard:
    """An independent provider fixture with explicit asynchronous checkpoints."""

    def __init__(self):
        """Provide independent text and explicit completion controls."""
        self.text = "external 한글\nline"
        self.reads = 0
        self.writes = []
        self.closed = False
        self.entered = asyncio.Event()
        self.release = None
        self.fail = None
        self.ignore_cancel = False

    async def read(self):
        """Optionally simulate late external completion after cancellation."""
        self.reads += 1
        self.entered.set()
        value = self.text
        if self.release is not None:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.release.wait()
        if self.fail is not None:
            raise self.fail
        return value

    async def write(self, text):
        """Record only successful completed writes."""
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail is not None:
            raise self.fail
        self.text = text
        self.writes.append(text)

    def close(self):
        """Observe provider lifetime completion."""
        self.closed = True

    async def aclose(self):
        """Match the production provider's asynchronous cleanup contract."""
        self.close()


class ClipboardProviderTests(unittest.IsolatedAsyncioTestCase):
    """Check literal child responses and independent provider effects."""

    def setUp(self):
        """Use a host with no clipboard read protocol to require the provider path."""
        self.output, self.replies = [], []
        self.provider = MemoryClipboard()
        self.broker = ClipboardBroker(
            reply=self.replies.append,
            emit=self.output.append,
            profile=lambda: HostProfile(),
        )
        self.broker.register_provider("fixture", factory=lambda: self.provider)

    async def asyncTearDown(self):
        """Release deliberately delayed requests and reap broker tasks."""
        if self.provider.release is not None:
            self.provider.release.set()
        await self.broker.aclose()

    async def done(self):
        """Wait for explicit transaction completion, including a queued user copy."""
        async with asyncio.timeout(5):
            while self.broker.pending is not None:
                await self.broker.idle.wait()
        await asyncio.sleep(0)

    async def test_default_write_policy_and_explicit_read_return_fresh_text(self):
        """Default-denied reads become real fresh reads only under explicit policy."""
        self.broker.child_osc(b"52;c;?")
        self.assertEqual(self.replies, [b"\x1b]52;c;" + ST])
        self.assertEqual(self.provider.reads, 0)
        self.broker.child_osc(b"52;c;Y29weQ==")
        await self.done()
        self.assertEqual(self.provider.writes, ["copy"])
        self.provider.text = "outside 😀"
        self.broker.allow_read = True
        self.broker.child_osc(b"52;c;?")
        await self.done()
        self.assertEqual(
            self.replies[-1],
            b"\x1b]52;c;" + base64.b64encode("outside 😀".encode()) + ST,
        )
        self.provider.text = ""
        self.broker.child_osc(b"52;c;?")
        await self.done()
        self.assertEqual(self.replies[-1], b"\x1b]52;c;" + ST)
        self.assertEqual(self.output, [])

    async def test_failed_provider_never_falls_back_to_host_or_internal_cache(self):
        """Even a capable host cannot redirect a failed explicit provider request."""
        self.broker.profile = lambda: HostProfile(clipboard_protocol=True)
        self.broker.allow_read = True
        self.provider.fail = OSError(errno.ENOSYS, "fixture")
        self.broker.child_osc(b"52;c;?")
        await self.done()
        self.assertEqual(self.replies, [b"\x1b]52;c;" + ST])
        self.broker.child_osc(b"5522;type=read:id=r;dGV4dC9wbGFpbg==")
        await self.done()
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=read:status=ENOSYS:id=r" + ST
        )
        self.assertEqual(self.output, [])
        self.assertEqual(self.broker.failures, 2)

    async def test_registration_is_single_session_fixed_and_closes_once(self):
        """Reject competing or late registration and post-close writes."""
        with self.assertRaises(ValueError):
            self.broker.register_provider("second", factory=MemoryClipboard)
        self.broker.seal()
        self.broker.seal()
        with self.assertRaises(RuntimeError):
            self.broker.register_provider("late", factory=MemoryClipboard)
        self.broker.close()
        self.assertTrue(self.provider.closed)
        self.assertFalse(self.broker.write_text(b"after close"))
        self.assertEqual(self.provider.writes, [])

    async def test_plaintext_mime_commit_and_read_are_correlated_and_bounded(self):
        """Incomplete writes and forged host acknowledgements cannot publish success."""
        self.broker.child_osc(b"5522;type=write:id=w")
        self.broker.child_osc(b"5522;type=wdata:id=other:mime=dGV4dC9wbGFpbg==;YmFk")
        self.broker.child_osc(b"5522;type=wdata:id=w:mime=dGV4dC9wbGFpbg==;aG")
        self.broker.child_osc(b"5522;type=wdata:id=w:mime=dGV4dC9wbGFpbg==;k=")
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.replies, [])
        identity = self.broker.pending.host_id
        self.broker.host_reply(f"\x1b]5522;type=write:id={identity}:status=DONE\x1b\\")
        self.assertEqual(self.replies, [])
        self.broker.child_osc(b"5522;type=wdata:id=w")
        await self.done()
        self.assertEqual(self.provider.writes, ["hi"])
        self.assertEqual(self.replies, [b"\x1b]5522;type=write:status=DONE:id=w" + ST])
        self.broker.allow_read = True
        self.broker.child_osc(b"5522;type=read:id=r;dGV4dC9wbGFpbg==")
        await self.done()
        self.assertEqual(
            self.replies[-3:],
            [
                b"\x1b]5522;type=read:status=OK:id=r" + ST,
                b"\x1b]5522;type=read:status=DATA:mime=dGV4dC9wbGFpbg==:id=r;aGk=" + ST,
                b"\x1b]5522;type=read:status=DONE:id=r" + ST,
            ],
        )
        self.assertEqual(self.output, [])

    async def test_mime_listing_does_not_read_content_or_advertise_other_types(self):
        """A text-only backend cannot invent the system clipboard's actual MIME list."""
        self.broker.child_osc(b"5522;type=read:id=m;Lg==")
        await self.done()
        self.assertEqual(self.provider.reads, 0)
        self.assertEqual(
            self.replies,
            [b"\x1b]5522;type=read:status=ENOSYS:id=m" + ST],
        )

    async def test_primary_compound_selectors_and_nontext_mime_are_not_redirected(self):
        """Unsupported selections and representations never touch CLIPBOARD."""
        self.broker.allow_read = True
        for selector in (b"p", b"s", b"cp"):
            self.broker.child_osc(b"52;" + selector + b";?")
            self.assertEqual(self.replies[-1], b"\x1b]52;" + selector + b";" + ST)
            self.assertFalse(self.broker.write_text(b"data", selector.decode()))
        self.assertIsNone(
            self.broker.read_primary(lambda _: self.fail("unexpected PRIMARY"))
        )
        self.broker.child_osc(b"5522;type=read:loc=primary:id=p;dGV4dC9wbGFpbg==")
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=read:status=ENOSYS:loc=primary:id=p" + ST
        )
        self.broker.child_osc(b"5522;type=write:id=m")
        self.broker.child_osc(b"5522;type=wdata:mime=aW1hZ2UvcG5n;AA==")
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=write:status=ENOSYS:id=m" + ST
        )
        self.assertEqual(
            (self.provider.reads, self.provider.writes, self.output), (0, [], [])
        )
        self.assertTrue(self.broker.copy_selection(b"user copy"))
        await self.done()
        self.assertEqual(self.provider.writes, ["user copy"])

    async def test_reset_rejects_late_completion_and_old_timeout_during_new_read(self):
        """An old read result and timeout cannot complete a replacement request."""
        self.broker.allow_read = True
        self.provider.release = asyncio.Event()
        self.provider.ignore_cancel = True
        self.broker.child_osc(b"52;c;?")
        old = self.broker.pending.host_id
        await self.provider.entered.wait()
        self.broker.reset()
        self.provider.text = "new"
        self.broker.child_osc(b"52;c;?")
        current = self.broker.pending.host_id
        self.broker.expire(old)
        self.assertEqual(self.broker.pending.host_id, current)
        self.provider.release.set()
        await self.done()
        self.assertEqual(self.replies, [b"\x1b]52;c;" + ST, b"\x1b]52;c;bmV3" + ST])

    async def test_close_cancels_read_and_suppresses_late_reply(self):
        """Shutdown reaps work without emitting a reply to a closed child."""
        self.broker.allow_read = True
        self.provider.release = asyncio.Event()
        self.broker.child_osc(b"52;c;?")
        await self.provider.entered.wait()
        await self.broker.aclose()
        self.assertEqual(self.replies, [])
        self.assertEqual(self.broker._tasks, set())
        self.assertTrue(self.provider.closed)

    async def test_text_limit_invalid_utf8_and_failed_commit_do_not_publish_prefix(
        self,
    ):
        """Invalid text and oversized responses have no partial clipboard effect."""
        self.assertFalse(self.broker.write_text(b"x" * (MAX_TEXT + 1)))
        self.assertFalse(self.broker.write_text(b"\xff"))
        self.broker.allow_read = True
        self.provider.text = "x" * (MAX_TEXT + 1)
        self.broker.child_osc(b"52;c;?")
        await self.done()
        self.assertEqual(self.replies[-1], b"\x1b]52;c;" + ST)
        self.broker.child_osc(b"5522;type=write:id=w")
        self.broker.child_osc(b"5522;type=wdata:mime=dGV4dC9wbGFpbg==;/w==")
        self.broker.child_osc(b"5522;type=wdata")
        self.assertEqual(
            self.replies[-1], b"\x1b]5522;type=write:status=EINVAL:id=w" + ST
        )
        self.assertEqual(self.provider.writes, [])

    async def test_explicit_copy_waits_for_active_read_and_preserves_write_order(self):
        """A user copy queues behind the active transaction without racing it."""
        self.broker.allow_read = True
        self.provider.release = asyncio.Event()
        self.broker.child_osc(b"52;c;?")
        await self.provider.entered.wait()
        self.assertTrue(self.broker.copy_selection(b"next"))
        self.assertFalse(self.broker.write_text(b"unrequested concurrent"))
        self.provider.release.set()
        await self.done()
        self.assertEqual(self.provider.writes, ["next"])
        self.assertEqual(
            self.replies,
            [b"\x1b]52;c;" + base64.b64encode("external 한글\nline".encode()) + ST],
        )

    async def test_consecutive_osc52_copy_and_read_preserve_order_without_yielding(
        self,
    ):
        """A single child output batch can copy then read its completed value."""
        self.broker.allow_read = True
        self.provider.release = asyncio.Event()
        self.broker.child_osc(b"52;c;bmV3")
        self.broker.child_osc(b"52;c;?")
        self.broker.child_osc(b"52;c;bGFzdA==")
        self.broker.child_osc(b"52;c;?")
        await self.provider.entered.wait()
        self.assertEqual((self.replies, self.provider.reads), ([], 0))
        self.provider.release.set()
        await self.done()
        self.assertEqual(self.provider.writes, ["new", "last"])
        self.assertEqual(
            self.replies, [b"\x1b]52;c;bmV3" + ST, b"\x1b]52;c;bGFzdA==" + ST]
        )

    async def test_deferred_requests_are_bounded_and_reset_discards_unsent_writes(self):
        """A blocked provider retains bounded work and reset cannot publish that queue."""
        self.provider.release = asyncio.Event()
        self.broker.child_osc(b"52;c;Zmlyc3Q=")
        await self.provider.entered.wait()
        for _ in range(70):
            self.broker.child_osc(b"52;c;bmV4dA==")
        self.assertEqual(len(self.broker._deferred), 64)
        self.assertEqual(self.broker.failures, 6)
        self.broker.reset()
        self.provider.release.set()
        await self.done()
        self.assertEqual((self.provider.writes, list(self.broker._deferred)), ([], []))

    async def test_response_sink_failure_reaches_application_failure_boundary(self):
        """Delivery failure is never hidden as a successful clipboard transaction."""
        error = BufferError("fixture response queue is full")
        failures = []
        self.broker.allow_read = True
        self.broker.failed = failures.append

        def reply(_):
            raise error

        self.broker.reply = reply
        self.broker.child_osc(b"52;c;?")
        await self.done()
        self.assertEqual(failures, [error])
        self.assertEqual(self.broker.failures, 0)

    async def test_idle_waits_for_all_queued_copies_before_child_exit(self):
        """Finishing one write cannot wake session shutdown while the next is active."""
        first, second = asyncio.Event(), asyncio.Event()
        started, next_started = asyncio.Event(), asyncio.Event()

        async def write(text):
            if text == "first":
                started.set()
                await first.wait()
            else:
                next_started.set()
                await second.wait()
            self.provider.writes.append(text)

        self.provider.write = write
        self.broker.child_osc(b"52;c;Zmlyc3Q=")
        self.broker.child_osc(b"52;c;c2Vjb25k")
        await started.wait()
        waiter = asyncio.create_task(self.broker.idle.wait())
        await asyncio.sleep(0)
        first.set()
        try:
            await asyncio.wait_for(next_started.wait(), 5)
            self.assertFalse(self.broker.idle.is_set())
            self.assertFalse(waiter.done())
        finally:
            second.set()
            await asyncio.wait_for(waiter, 5)
        self.assertEqual(self.provider.writes, ["first", "second"])


class SystemProviderTests(unittest.IsolatedAsyncioTestCase):
    """Check target selection and real subprocess pipes without desktop access."""

    async def test_destination_is_fixed_and_ssh_never_selects_remote_windows(self):
        """SSH chooses only the supplied X11 connection and never a remote desktop."""
        for env, target in (
            ({"WSL_INTEROP": "fixture", "DISPLAY": ":fixture"}, "windows"),
            (
                {"SSH_TTY": "fixture", "WSL_INTEROP": "fixture", "DISPLAY": ":fixture"},
                "x11",
            ),
            ({"SSH_CONNECTION": "fixture", "WSL_INTEROP": "fixture"}, None),
            ({"DISPLAY": ":fixture"}, "x11"),
            ({"WAYLAND_DISPLAY": "fixture"}, None),
        ):
            provider = sample.SystemClipboard(env=env)
            self.assertEqual(provider.target, target)
            await provider.aclose()
        provider = sample.SystemClipboard(target="windows", env={"DISPLAY": ":fixture"})
        with self.assertRaises(OSError):
            await provider.read()
        await provider.aclose()

    async def test_sample_loads_through_existing_plugin_manager_without_ui_or_io(self):
        """Resolve package-relative worker imports through the one supported loader."""
        from types import SimpleNamespace

        from ish.plugin.manager import PluginManager

        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(script_dir=SAMPLE_DIR, lib_dir=Path(directory))
            manager.load(names=["clipboard_system"])
            module = manager.get("clipboard_system")
            self.assertIsNotNone(module)
            selected = []
            api = SimpleNamespace(
                register_clipboard=lambda name, **options: selected.append(
                    (name, options["factory"])
                )
            )
            module.setup(api)
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0][0], "system")
            with patch.dict(os.environ, {}, clear=True):
                provider = selected[0][1]()
                self.assertIsNone(provider.target)
                self.assertEqual(provider.processes, set())
                await provider.aclose()

    async def test_windows_pipe_fixtures_roundtrip_unicode_and_empty_text(self):
        """Preserve Windows transport encodings using temporary independent files."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clipboard"
            write = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
            read = "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes().decode('utf-16-le').encode('utf-8'))"
            provider = sample.SystemClipboard(
                env={
                    **os.environ,
                    "WSL_INTEROP": "fixture",
                    "SSH_TTY": "",
                    "SSH_CONNECTION": "",
                }
            )
            try:
                with (
                    patch.object(
                        sample.SystemClipboard,
                        "WINDOWS_WRITE",
                        (sys.executable, "-c", write, str(path)),
                    ),
                    patch.object(
                        sample.SystemClipboard,
                        "WINDOWS_READ",
                        (sys.executable, "-c", read, str(path)),
                    ),
                ):
                    for text in ("한글 😀\n$(literal)", ""):
                        await provider.write(text)
                        self.assertEqual(path.read_bytes(), text.encode("utf-16-le"))
                        self.assertEqual(await provider.read(), text)
                    await provider.write("keep")
                    with self.assertRaises(OSError):
                        await provider.write("embedded\0nul")
                    self.assertEqual(path.read_bytes(), "keep".encode("utf-16-le"))
            finally:
                await provider.aclose()

    async def test_subprocess_output_is_bounded_and_cancellation_reaps_children(self):
        """A large or stalled helper cannot retain unbounded memory or orphan a child."""
        provider = sample.SystemClipboard(
            env={
                **os.environ,
                "WSL_INTEROP": "fixture",
                "SSH_TTY": "",
                "SSH_CONNECTION": "",
            }
        )
        try:
            with patch.object(
                sample.SystemClipboard,
                "WINDOWS_READ",
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x'*1048577)",
                ),
            ):
                with self.assertRaises(OSError) as error:
                    await provider.read()
                self.assertEqual(error.exception.errno, errno.EFBIG)
            with patch.object(
                sample.SystemClipboard,
                "WINDOWS_READ",
                (sys.executable, "-c", "import time; time.sleep(60)"),
            ):
                task = asyncio.create_task(provider.read())
                async with asyncio.timeout(5):
                    while not provider.processes:
                        await asyncio.sleep(0)
                processes = list(provider.processes)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(
                    all(process.returncode is not None for process in processes)
                )
                self.assertEqual(provider.processes, set())
        finally:
            await provider.aclose()


class X11ProviderTests(unittest.IsolatedAsyncioTestCase):
    """Use an independent X11 client on an isolated test server."""

    @classmethod
    def setUpClass(cls):
        """Start a private server without changing WSLg or desktop socket permissions."""
        xvfb, cls.xclip = shutil.which("Xvfb"), shutil.which("xclip")
        if not xvfb or not cls.xclip:
            raise RuntimeError("Clipboard interoperability requires Xvfb and xclip")
        command = [
            xvfb,
            "-displayfd",
            "1",
            "-screen",
            "0",
            "640x480x24",
            "-nolisten",
            "tcp",
            "-noreset",
        ]
        sockets = Path("/tmp/.X11-unix")
        if not (sockets.is_dir() and os.access(sockets, os.W_OK | os.X_OK)):
            # WSLg's shared directory can be unwritable. Only this case needs
            # a private mount; ordinary Linux does not require user namespaces.
            # No existing desktop sockets or permissions are changed. Abstract
            # local sockets remain reachable by the independent xclip client.
            unshare = shutil.which("unshare")
            if not unshare:
                raise RuntimeError(
                    "An unwritable X11 socket directory requires unshare"
                )
            command = [
                unshare,
                "--user",
                "--map-root-user",
                "--mount",
                "--propagation",
                "private",
                "sh",
                "-c",
                'mount -t tmpfs -o mode=1777 tmpfs /tmp && exec "$@"',
                "sh",
                *command,
            ]
        cls.log = tempfile.TemporaryFile()
        cls.server = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_server)
        if not select.select([cls.server.stdout], [], [], 15)[0]:
            cls.log.seek(0)
            raise RuntimeError(
                "Private X server did not start: " + cls.log.read(4096).decode()
            )
        number = cls.server.stdout.readline().decode().strip()
        if not number.isdigit():
            cls.log.seek(0)
            raise RuntimeError(cls.log.read(4096).decode())
        cls.env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "DISPLAY",
                "XAUTHORITY",
                "WAYLAND_DISPLAY",
                "WSL_DISTRO_NAME",
                "WSL_INTEROP",
            )
        }
        cls.env.update(
            DISPLAY=":" + number, PATH="/no-clipboard-helpers", SSH_TTY="fixture"
        )

    @classmethod
    def stop_server(cls):
        """Reap only this test's X server and close its diagnostic streams."""
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.server.stdout.close()
        cls.log.close()

    async def asyncSetUp(self):
        """Create a fresh provider and retain independent owner processes for cleanup."""
        self.provider = sample.SystemClipboard(env=self.env)
        self.external = []

    async def asyncTearDown(self):
        """Close both native and independent selection owners before the next test."""
        await self.provider.aclose()
        for process in self.external:
            if process.returncode is None:
                process.terminate()
            await process.wait()

    async def read_external(self, target="UTF8_STRING"):
        """Read the actual selection through independent xclip, without helper fallback."""
        process = await asyncio.create_subprocess_exec(
            self.xclip,
            "-selection",
            "clipboard",
            "-o",
            "-target",
            target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        try:
            async with asyncio.timeout(5):
                data, error = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        self.assertEqual(process.returncode, 0, error)
        return data

    async def write_external(self, data):
        """Establish independent ownership before asking the provider for fresh text."""
        previous = self.provider.x11.owner
        process = await asyncio.create_subprocess_exec(
            self.xclip,
            "-selection",
            "clipboard",
            "-i",
            "-quiet",
            "-target",
            "UTF8_STRING",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.env,
        )
        self.external.append(process)
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()
        if previous is not None and previous.returncode is None:
            # SelectionClear proves the replacement occurred. Reading an old
            # owner's INCR stream while it exits would race this fixture itself.
            await asyncio.wait_for(previous.wait(), 5)
        async with asyncio.timeout(5):
            while True:
                reader = await asyncio.create_subprocess_exec(
                    self.xclip,
                    "-selection",
                    "clipboard",
                    "-o",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self.env,
                )
                try:
                    output, _ = await reader.communicate()
                finally:
                    if reader.returncode is None:
                        reader.kill()
                        await reader.communicate()
                if output == data and reader.returncode == 0:
                    return
                await asyncio.sleep(0.01)

    async def test_independent_unicode_empty_and_incremental_roundtrip(self):
        """Exchange empty, Unicode and INCR-sized content in both directions."""
        for text in ("한글 😀\nsecond line", "", "한글 😀\n" * 30000):
            await self.provider.write(text)
            self.assertEqual((await self.read_external()).decode(), text)
            await self.write_external((text + "external").encode())
            self.assertEqual(await self.provider.read(), text + "external")

    async def test_ownership_replacement_and_close_reap_native_worker(self):
        """Repeated owner replacement retains the latest value and reaps old workers."""
        previous = None
        for index in range(8):
            await self.provider.write(f"copy {index}")
            self.assertEqual(await self.read_external(), f"copy {index}".encode())
            if previous is not None:
                await asyncio.wait_for(previous.wait(), 5)
            previous = self.provider.x11.owner
        await self.provider.aclose()
        self.assertIsNotNone(previous.returncode)

    async def test_failed_display_cannot_replace_existing_selection(self):
        """A failed connection neither redirects a write nor destroys earlier content."""
        await self.provider.write("keep")
        broken = sample.SystemClipboard(
            target="x11", env={**self.env, "DISPLAY": ":65530"}
        )
        try:
            with self.assertRaises(OSError):
                await broken.write("lost")
            self.assertEqual(await self.read_external(), b"keep")
        finally:
            await broken.aclose()

    async def test_actual_vt_and_controlling_pty_return_provider_data_to_foreground_file(
        self,
    ):
        """A foreground PTY consumer saves the actual provider reply in a result file."""
        from ish.terminal.session import Session
        from ish.terminal.vt import Terminal

        await self.write_external("remote clipboard 한글".encode())
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result"
            script = (
                "import os,tty,pathlib,sys; tty.setraw(0); "
                "os.write(1,b'\\x1b]52;c;?\\x1b\\\\'); data=b''\n"
                "while not data.endswith(b'\\x1b\\\\'):\n data+=os.read(0,4096)\n"
                "pathlib.Path(sys.argv[1]).write_bytes(data)\n"
            )
            done, failures, codes = asyncio.Event(), [], []
            terminal = Terminal(80, 24, reply=lambda data: session.send(data))
            broker = ClipboardBroker(
                reply=lambda data: session.send(data),
                emit=lambda _: self.fail("unexpected host clipboard traffic"),
                profile=HostProfile,
                allow_read=True,
            )
            broker.register_provider("private-x11", factory=lambda: self.provider)
            terminal.clipboard = broker
            session = Session(
                [sys.executable, "-c", script, str(result)],
                directory,
                terminal.feed,
                lambda code: (codes.append(code), done.set()),
                lambda error: (failures.append(error), done.set()),
                env=self.env,
            )
            try:
                session.start(80, 24)
                await asyncio.wait_for(done.wait(), 8)
                self.assertEqual(failures, [])
                self.assertEqual(codes, [0])
                self.assertEqual(
                    result.read_bytes(),
                    b"\x1b]52;c;"
                    + base64.b64encode("remote clipboard 한글".encode())
                    + ST,
                )
            finally:
                await broker.aclose()
                await session.close()
                terminal.close()
