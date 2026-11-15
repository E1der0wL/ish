"""Exercise startup's public CLI and its read-only boundaries in subprocesses."""

from __future__ import annotations

import errno
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 30


class StartupTests(unittest.TestCase):
    """Verify visible output, exit codes and files without importing startup internals."""

    def setUp(self):
        """Isolate settings, executable traps and all prospective output paths."""
        self.directory = tempfile.TemporaryDirectory(prefix="ish-startup-")
        self.root = Path(self.directory.name)
        self.home = self.root / "absent-settings"
        self.native_home = self.root / "native-home"
        self.native_home.mkdir()
        self.env = dict(os.environ)
        for key in ("ZDOTDIR", "BASH_ENV", "ENV", "ISH_BUNDLE_ROOT"):
            self.env.pop(key, None)
        self.env.update(
            HOME=str(self.native_home),
            LANG="C.UTF-8",
            LC_ALL="C.UTF-8",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(ROOT / "src"),
        )

    def tearDown(self):
        """Remove only this test's isolated files."""
        self.directory.cleanup()

    def cli(self, *args, module="ish.stream"):
        """Run the public module without a TTY, giving timeout no production meaning."""
        return subprocess.run(
            [sys.executable, "-m", module, "--home", str(self.home), *map(str, args)],
            env=self.env,
            cwd=self.root,
            input="",
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )

    def files(self):
        """Record independent file contents and directory names for write detection."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            if path.is_file()
            else None
            for path in self.root.rglob("*")
        }

    def field(self, output, name):
        """Read a displayed diagnostic value by its documented human-readable label."""
        values = [
            line.partition(": ")[2]
            for line in output.splitlines()
            if line.startswith(name + ": ")
        ]
        self.assertEqual(len(values), 1, output)
        return values[0]

    def write_language(self, values):
        """Create a user override using the shared settings layout."""
        directory = self.home / "lang"
        directory.mkdir(parents=True)
        (directory / "ko.json").write_text(json.dumps(values), encoding="utf-8")

    def trap_plugin(self):
        """Make plugin import observable even when no registration function is called."""
        directory = self.root / "plugins" / "selected"
        directory.mkdir(parents=True)
        marker = self.root / "plugin-executed"
        (directory / "selected.py").write_text(
            "PLUGIN_META = {'name': 'selected', 'version': '1.0'}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "raise RuntimeError('plugin executed during information query')\n",
            encoding="utf-8",
        )
        return directory.parent

    def trap_shell(self):
        """Create an executable whose invocation leaves a file rather than a shell UI."""
        shell = self.root / "shell-trap"
        shell.write_text(
            "#!/bin/sh\n"
            f"printf executed > {shlex.quote(str(self.root / 'shell-executed'))}\n",
            encoding="utf-8",
        )
        shell.chmod(0o700)
        return shell

    def test_information_flags_need_no_terminal_or_new_files(self):
        """Help, version and diagnosis cannot import plugins, run shells or create logs."""
        plugins = self.trap_plugin()
        shell = self.trap_shell()
        before = self.files()
        for flag in ("--help", "--version", "--diagnose"):
            with self.subTest(flag=flag):
                result = self.cli(
                    flag,
                    "--lang",
                    "en",
                    "--shell",
                    shell,
                    "--plugin",
                    "selected",
                    "--plugin-dir",
                    plugins,
                    "--plugin-lib-dir",
                    self.root / "absent-libraries",
                    "--log-file",
                    self.root / "absent-logs" / "stream.log",
                    "--log-level",
                    "DEBUG",
                    "--cwd",
                    self.root / "absent-cwd",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ish-stream", result.stdout)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.files(), before)

    def test_implicit_log_path_remains_read_only(self):
        """Selecting the configured log file during a query does not create settings."""
        before = self.files()
        result = self.cli("--lang", "en", "--diagnose", "--log-file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.field(result.stdout, "Log file"), str(self.home / ".ish.log")
        )
        self.assertEqual(self.files(), before)

    def test_korean_help_and_diagnostics_need_no_user_language_file(self):
        """Shipped Korean is usable on a fresh installation and translates headings."""
        for flag, expected in (
            ("--help", "사용법:"),
            ("--diagnose", "ish-stream 환경 진단"),
        ):
            with self.subTest(flag=flag):
                result = self.cli(flag, "--lang", "ko")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.home.exists())

    def test_user_language_override_works_in_any_argument_order(self):
        """The selected settings directory supplies translations before help is printed."""
        self.write_language(
            {
                "stream_description": "LOCAL DESCRIPTION",
                "cli_options_title": "LOCAL OPTIONS",
            }
        )
        before = self.files()
        result = self.cli("--help", "--lang", "ko")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOCAL DESCRIPTION", result.stdout)
        self.assertIn("LOCAL OPTIONS:", result.stdout)
        self.assertIn("기본 셸 인수 -i를 대체합니다", result.stdout)
        self.assertEqual(self.files(), before)

    def test_translation_warnings_obey_level_without_creating_query_logs(self):
        """Broken user JSON is reported once or filtered without enabling file logs."""
        self.write_language({})
        (self.home / "lang" / "ko.json").write_text("{", encoding="utf-8")
        before = self.files()
        for flag in ("--help", "--version", "--diagnose"):
            for level, warnings in (("CRITICAL", 0), ("WARNING", 1)):
                with self.subTest(flag=flag, level=level):
                    result = self.cli(
                        flag,
                        "--lang",
                        "ko",
                        "--log-level",
                        level,
                        "--log-file",
                        self.root / "new-log" / "query.log",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("ish-stream", result.stdout)
                    self.assertEqual(
                        result.stderr.count("Cannot load language file"), warnings
                    )
                    self.assertEqual(result.stderr.count("[WARNING]"), warnings)
                    if not warnings:
                        self.assertEqual(result.stderr, "")
                    self.assertEqual(self.files(), before)

    def test_version_uses_stream_name(self):
        """The stream entry point keeps an independently recognizable version label."""
        result = self.cli("--lang", "en", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\Aish-stream \d+\.\d+\.\d+\n\Z")

    def test_removed_demo_flag_is_rejected(self):
        """The obsolete implicit demo selector is absent from the public CLI."""
        result = self.cli("--lang", "en", "--demo")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unrecognized arguments: --demo", result.stderr)
        self.assertFalse(self.home.exists())

    def test_execution_without_tty_fails_before_files_or_plugins(self):
        """A noninteractive launch fails before plugin imports or log setup."""
        plugins = self.trap_plugin()
        before = self.files()
        result = self.cli(
            "--lang",
            "en",
            "--plugin",
            "selected",
            "--plugin-dir",
            plugins,
            "--log-file",
            self.root / "logs" / "stream.log",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Standard input and output must be terminals", result.stderr)
        self.assertEqual(self.files(), before)

    def test_login_and_custom_arguments_are_mutually_exclusive(self):
        """Ambiguous shell startup modes produce an argument error even for diagnosis."""
        result = self.cli("--lang", "en", "--diagnose", "--login", "--shell-arg=-i")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("not allowed with argument --login", result.stderr)
        self.assertFalse(self.home.exists())

    def test_diagnostics_report_exact_selected_shell_arguments(self):
        """Custom arguments replace -i, and login mode selects only its own arguments."""
        for options, expected in (
            ((), "-i"),
            (("--login",), "-l"),
            (("--shell-arg=--norc", "--shell-arg=-i"), "--norc -i"),
            (("--shell-arg=-c", "--shell-arg=printf x"), "-c 'printf x'"),
        ):
            with self.subTest(options=options):
                result = self.cli("--lang", "en", "--diagnose", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.field(result.stdout, "Shell arguments"), expected)

    def test_diagnostics_report_configuration_without_certifying_support(self):
        """A missing shell or selected plugin remains configuration, never readiness."""
        result = self.cli(
            "--lang",
            "en",
            "--diagnose",
            "--shell",
            "not-an-installed-shell",
            "--plugin",
            "unverified",
            "--log-level",
            "debug",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.field(result.stdout, "Selected plugins"), "unverified")
        self.assertEqual(self.field(result.stdout, "Log threshold"), "DEBUG")
        self.assertEqual(self.field(result.stdout, "Shell path"), "Not found")
        self.assertIn("No shell or plugin was executed", result.stdout)
        self.assertIn("do not certify runtime support or readiness", result.stdout)
        self.assertEqual(self.field(result.stdout, "Standard input is a TTY"), "No")

    def test_diagnostics_use_resource_paths_without_importing_ui_or_plugins(self):
        """Reading owned default paths does not load the optional execution layers."""
        source = (
            "import sys\n"
            "from ish.stream.__main__ import main\n"
            f"status = main(['--home', {str(self.home)!r}, '--lang', 'en', '--diagnose'])\n"
            "for module in ('ish.plugin.manager', 'ish.stream.app', 'prompt_toolkit'):\n"
            "    assert module not in sys.modules, module\n"
            "raise SystemExit(status)\n"
        )
        before = self.files()
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.field(result.stdout, "Plugin directory"),
            str(ROOT / "src/ish/stream/bundled"),
        )
        self.assertEqual(
            self.field(result.stdout, "Shell integration directory"),
            str(ROOT / "src/ish/stream/integrations"),
        )
        self.assertEqual(self.files(), before)

    def test_invalid_language_is_an_argument_error_without_traceback(self):
        """Invalid language paths are rejected through the CLI instead of crashing."""
        for language in ("../outside", ""):
            with self.subTest(language=language):
                result = self.cli("--help", "--lang", language)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_relative_shell_survives_cwd_change_and_preserves_symlink_name(self):
        """Resolve launch location without changing the selected executable identity."""
        import pty
        import termios

        work = self.root / "shell-work"
        work.mkdir()
        result = work / "launched-as"
        shell = self.root / "local-shell"
        shell.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0" > ' + shlex.quote(str(result)) + "\n",
            encoding="utf-8",
        )
        shell.chmod(0o700)
        (self.root / "named-shell").symlink_to("local-shell")
        for name in ("local-shell", "named-shell"):
            with self.subTest(shell=name):
                result.unlink(missing_ok=True)
                argv = [
                    sys.executable,
                    "-m",
                    "ish.stream",
                    "--lang",
                    "en",
                    "--home",
                    str(self.home),
                    "--shell",
                    "./" + name,
                    "--cwd",
                    str(work),
                ]
                pid, fd = pty.fork()
                if pid == 0:
                    os.chdir(self.root)
                    termios.tcsetwinsize(0, (24, 80))
                    os.execvpe(argv[0], argv, self.env)
                    os._exit(127)
                status = None
                output = bytearray()
                deadline = time.monotonic() + DEADLINE
                try:
                    while time.monotonic() < deadline:
                        if select.select([fd], [], [], 0.1)[0]:
                            try:
                                output.extend(os.read(fd, 65536))
                            except OSError as error:
                                if error.errno != errno.EIO:
                                    raise
                        found, waited = os.waitpid(pid, os.WNOHANG)
                        if found:
                            status = os.waitstatus_to_exitcode(waited)
                            break
                finally:
                    if status is None:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    os.close(fd)
                self.assertEqual(status, 0, bytes(output))
                self.assertEqual(result.read_text(), str(self.root / name) + "\n")

    def test_original_entry_information_queries_are_preserved(self):
        """Shared translation and diagnostic changes retain old ish's information CLI."""
        before = self.files()
        for flag in ("--help", "--version", "--diagnose"):
            with self.subTest(flag=flag):
                result = self.cli(flag, "--lang", "en", module="ish.main")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ish", result.stdout)
                self.assertNotIn("ish-stream", result.stdout)
                self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
