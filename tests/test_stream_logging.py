"""Observe startup and logging through the public CLI on a controlling PTY."""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

from stream_fixture import shell_executable
from stream_plugins import copy_plugins
from test_stream_cli import BAR, F2, PROMPT, StreamProcess

METADATA = "PLUGIN_META = {'version': '1.0.0'}\nSTREAM_API = 1\n"


@unittest.skipUnless(
    os.name == "posix" and shutil.which("tcsh"), "requires native tcsh"
)
class StreamLoggingTests(unittest.TestCase):
    """Check actual files, terminal output, and native command exit status."""

    def setUp(self):
        """Give each case a native startup directory and optional plugin sources."""
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-stream-logging-")
        self.root = Path(self.temporary.name)
        self.plugins = self.root / "plugins"
        self.processes = []

    def tearDown(self):
        """Reap only processes created by this fixture before deleting their files."""
        for process in self.processes:
            process.stop()
        self.temporary.cleanup()

    def start(self, *, prompt=True, **options):
        """Invoke CLI options through the shared actual-terminal fixture."""
        process = StreamProcess(self.root, **options)
        self.processes.append(process)
        if prompt:
            process.expect(PROMPT)
        return process

    def finish(self, process, *, status=0, submit=True):
        """Read through actual PTY EOF, including diagnostics after restoration."""
        if submit:
            process.send(b"exit\r")
        process.wait(lambda: process.poll() is not None, "CLI exit")
        process.wait(lambda: process.pty_end is not None, "final terminal output")
        self.assertEqual(process.status, status, process.diagnostic("unexpected exit"))

    def plugin(self, name, source):
        """Write a normal external plugin consumed by the existing PluginManager."""
        directory = self.plugins / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.py").write_text(METADATA + source, encoding="utf-8")

    def logged(self, process, path, text):
        """Observe an immediately flushed log record without ending the live shell."""
        marker = text.encode("utf-8")
        process.wait(
            lambda: path.is_file() and marker in path.read_bytes(),
            f"file diagnostic: {text}",
        )

    def test_debug_records_are_immediate_on_disk_and_deferred_in_terminal(self):
        """Shell events and popup focus never become bytes inside the live shell view."""
        path = self.root / "logs/session.log"
        process = self.start(
            demo=True,
            extra_args=("--log-level", "DEBUG", "--log-file", str(path)),
        )
        process.expect(BAR)
        self.logged(process, path, "Session start shell=")
        self.logged(process, path, "Shell event kind=")
        offset = len(process.output)
        process.send(F2)
        process.expect(b"Plugin popup", offset)
        self.logged(process, path, "Popup focus=True")
        offset = len(process.output)
        process.send(b"\x1b")
        process.expect(BAR, offset)
        self.logged(process, path, "Popup focus=False")
        process.command("echo private-native-value > private-result")
        process.expect_file(self.root / "private-result", "private-native-value\n")
        self.assertIsNone(process.poll())
        self.assertNotIn(b"[DEBUG]", process.output)
        self.assertNotIn(b"Session exit status=", process.output)
        self.assertNotIn(b"private-native-value", path.read_bytes())
        self.finish(process)
        self.assertIn("Session exit status=0", path.read_text(encoding="utf-8"))
        self.assertIn(b"[DEBUG] Shell event kind=", process.output)
        self.assertIn(b"[DEBUG] Popup focus=True", process.output)
        self.assertIn(b"[INFO] Session exit status=0", process.output)

    def test_warning_default_filters_and_appends_utf8_plugin_records(self):
        """Plugins share filtering, UTF-8 append, and cleanup through a regular-file link."""
        path = self.root / "session.log"
        destination = self.root / "actual.log"
        destination.write_text("existing records\n", encoding="utf-8")
        path.symlink_to(destination)
        self.plugin(
            "messages",
            "from ish.log import get_logger\n"
            "def cleanup():\n"
            "    get_logger().info('hidden cleanup info')\n"
            "    get_logger().warning('종료 경고')\n"
            "def setup(api):\n"
            "    log = get_logger()\n"
            "    log.debug('hidden plugin debug')\n"
            "    log.info('hidden plugin info')\n"
            "    log.warning('시작 경고')\n"
            "    return cleanup\n",
        )
        for _ in range(2):
            process = self.start(
                plugins=("messages",),
                plugin_dir=self.plugins,
                extra_args=("--log-file", str(path)),
            )
            self.assertNotIn(BAR, process.output)
            self.assertNotIn(b"[INFO]", process.output)
            self.assertNotIn(b"[DEBUG]", process.output)
            self.assertIn("시작 경고".encode(), process.output)
            self.assertNotIn("종료 경고".encode(), process.output)
            self.finish(process)
            self.assertIn("종료 경고".encode(), process.output)
        contents = path.read_text(encoding="utf-8")
        self.assertTrue(path.is_symlink())
        self.assertEqual(contents, destination.read_text(encoding="utf-8"))
        self.assertTrue(contents.startswith("existing records\n"))
        self.assertEqual(contents.count("시작 경고"), 2)
        self.assertEqual(contents.count("종료 경고"), 2)
        self.assertNotIn("hidden", contents)
        self.assertNotIn("Session start", contents)
        self.assertNotIn("Session exit", contents)

    def test_runtime_plugin_errors_wait_for_restore_and_cleanup_is_logged(self):
        """A failed popup preserves native commands while its errors stay off live stderr."""
        path = self.root / "errors.log"
        copy_plugins(self.plugins)
        self.plugin(
            "broken",
            "def popup(accept): raise RuntimeError('팝업 실패')\n"
            "def cleanup(): raise RuntimeError('정리 실패')\n"
            "def setup(api):\n"
            "    api.set_popup(popup)\n"
            "    return cleanup\n",
        )
        process = self.start(
            plugins=("stream_bar", "broken", "stream_tcsh"),
            plugin_dir=self.plugins,
            extra_args=("--log-file", str(path)),
        )
        process.expect(BAR)
        offset = len(process.output)
        process.send(F2 + b"touch failed-popup-leak\r")
        self.logged(process, path, "팝업 실패")
        process.expect(PROMPT, offset)
        process.command("echo recovered > recovery")
        process.expect_file(self.root / "recovery", "recovered\n")
        self.assertFalse((self.root / "failed-popup-leak").exists())
        self.assertNotIn("팝업 실패".encode(), process.output)
        self.assertNotIn(b"[ERROR]", process.output)
        self.finish(process)
        self.assertIn("팝업 실패".encode(), process.output)
        self.assertIn("정리 실패".encode(), process.output)
        contents = path.read_text(encoding="utf-8")
        self.assertIn("팝업 실패", contents)
        self.assertIn("정리 실패", contents)
        self.assertNotIn("failed-popup-leak", contents)

    def test_bad_log_destination_fails_before_shell_startup(self):
        """Logging cannot silently fail and proceed to execute native startup files."""
        destination = self.root / "directory-instead-of-log"
        destination.mkdir()
        process = self.start(
            prompt=False,
            startup="echo unwanted > shell-started\n",
            extra_args=("--log-file", str(destination)),
        )
        self.finish(process, status=1, submit=False)
        self.assertFalse((self.root / "shell-started").exists())
        self.assertNotIn(PROMPT, process.output)
        self.assertIn(b"directory-instead-of-log", process.output)

    def test_terminal_devices_and_fifo_are_rejected_before_shell_startup(self):
        """A file option cannot create a second terminal writer or wait for a FIFO reader."""
        fifo = self.root / "log-fifo"
        os.mkfifo(fifo)
        for destination in (
            "/dev/stderr",
            "/dev/tty",
            "/proc/self/fd/1",
            "/dev/null",
            fifo,
        ):
            with self.subTest(destination=destination):
                process = self.start(
                    prompt=False,
                    startup="echo unwanted > shell-started\n",
                    extra_args=("--log-file", str(destination)),
                )
                self.finish(process, status=1, submit=False)
                self.assertFalse((self.root / "shell-started").exists())
                self.assertNotIn(PROMPT, process.output)

    def test_startup_language_warning_obeys_level_and_reaches_selected_file(self):
        """Pre-session translation diagnostics are filtered and delivered exactly once."""
        home = self.root / "settings"
        language = home / "lang"
        language.mkdir(parents=True)
        (language / "ko.json").write_text("{invalid", encoding="utf-8")
        marker = "을 불러올 수 없습니다:".encode()
        for level, count in (("WARNING", 1), ("CRITICAL", 0)):
            with self.subTest(level=level):
                path = self.root / (level.lower() + ".log")
                process = self.start(
                    extra_args=(
                        "--home",
                        str(home),
                        "--lang",
                        "ko",
                        "--log-level",
                        level,
                        "--log-file",
                        str(path),
                    ),
                )
                self.assertEqual(process.output.count(marker), count)
                self.assertEqual(path.read_bytes().count(marker), count)
                self.assertNotIn(b"Cannot load language file", process.output)
                self.finish(process)
                self.assertEqual(process.output.count(marker), count)
                self.assertEqual(path.read_bytes().count(marker), count)

    def test_home_selects_default_log_path_without_changing_shell_home(self):
        """Bare --log-file uses ish's selected home and leaves native HOME alone."""
        home = self.root / "ish-settings"
        path = home / ".ish.log"
        process = self.start(
            startup="echo $HOME > native-home\n",
            extra_args=("--home", str(home), "--log-file", "--log-level", "INFO"),
        )
        self.logged(process, path, "Session start shell=")
        process.expect_file(self.root / "native-home", str(self.root) + "\n")
        self.assertFalse((home / "lang").exists())
        self.assertFalse((home / "plugin").exists())
        self.assertNotIn(BAR, process.output)
        self.finish(process)
        self.assertIn("Session exit status=0", path.read_text(encoding="utf-8"))

    def test_plain_default_creates_no_log_or_settings_files(self):
        """A session with no logging options remains a plain native terminal."""
        process = self.start()
        process.command("echo plain > plain-result")
        process.expect_file(self.root / "plain-result", "plain\n")
        self.finish(process)
        self.assertFalse((self.root / "ish").exists())
        self.assertNotIn(b"[INFO]", process.output)
        self.assertNotIn(b"[DEBUG]", process.output)
        self.assertNotIn(BAR, process.output)

    def test_shell_arguments_preserve_files_and_native_exit_codes(self):
        """Explicit native -c options run unchanged in Bash, zsh, and tcsh."""
        for shell in ("bash", "zsh", "tcsh"):
            with self.subTest(shell=shell):
                if shutil.which(shell_executable(shell)) is None:
                    self.skipTest(f"{shell} is required")
                path = self.root / (shell + "-result")
                command = f"printf 'native 한글\\n' > {shlex.quote(str(path))}; exit 7"
                process = self.start(
                    shell=shell,
                    prompt=False,
                    extra_args=("--shell-arg=-c", "--shell-arg=" + command),
                )
                self.finish(process, status=7, submit=False)
                self.assertEqual(path.read_text(encoding="utf-8"), "native 한글\n")
                self.assertNotIn(BAR, process.output)
                self.assertFalse((self.root / "ish").exists())


if __name__ == "__main__":
    unittest.main()
