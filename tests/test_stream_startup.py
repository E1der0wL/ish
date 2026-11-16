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

    def terminal(self, *options, default_home=False):
        """Launch the public CLI in a real controlling PTY and capture its exit."""
        import pty
        import termios

        argv = [sys.executable, "-m", "ish.stream"]
        if not default_home:
            argv += ["--home", str(self.home)]
        argv += list(map(str, options))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            termios.tcsetwinsize(0, (24, 80))
            os.execvpe(argv[0], argv, self.env)
            os._exit(127)
        status = None
        eof = False
        output = bytearray()
        deadline = time.monotonic() + DEADLINE
        try:
            while time.monotonic() < deadline:
                if not eof and select.select([fd], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b""
                    output.extend(chunk)
                    if not chunk:
                        eof = True
                if status is None:
                    found, waited = os.waitpid(pid, os.WNOHANG)
                    if found:
                        status = os.waitstatus_to_exitcode(waited)
                if eof and status is not None:
                    break
                if eof:
                    time.sleep(0.01)
        finally:
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(fd)
        return status, output.decode(errors="replace")

    def write_rc(self, text):
        """Persist user choices in the public rc file."""
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".ishrc.py").write_text(text, encoding="utf-8")

    def recording_plugin(self, name):
        """Make actual PluginManager import, setup and cleanup independently visible."""
        directory = self.home / "plugins" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.py").write_text(
            "from pathlib import Path\n"
            "PLUGIN_META = {'version':'1', 'requirements':[], 'dependencies':[]}\n"
            "STREAM_API = 1\n"
            f"target = Path({str(self.root / (name + '-runs'))!r})\n"
            "def record(text):\n"
            "    with target.open('a') as file: file.write(text + '\\n')\n"
            "record('import')\n"
            "def setup(api):\n"
            "    record('setup')\n"
            "    return lambda: record('cleanup')\n",
            encoding="utf-8",
        )

    def test_information_queries_do_not_execute_existing_rc(self):
        """Queries remain useful even when Python configuration is broken or active."""
        marker = self.root / "rc-ran"
        self.write_rc(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "raise RuntimeError('must not execute')\n"
        )
        before = self.files()
        for flag in ("--help", "--version", "--diagnose"):
            result = self.cli(flag, "--lang", "en")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.files(), before)
        result = self.cli("--diagnose", "--lang", "en")
        self.assertIn(".ishrc.py was not executed", result.stdout)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_plugin_selection_persists_across_real_sessions(self):
        """The same saved selection loads through PluginManager on each new launch."""
        shell = self.trap_shell()
        self.recording_plugin("saved")
        work = self.home / "work"
        work.mkdir()
        shell.write_text(
            "#!/bin/sh\npwd > "
            + shlex.quote(str(self.root / "cwd-result"))
            + "\nprintf '%s\\n' \"$@\" > "
            + shlex.quote(str(self.root / "argv-result"))
            + "\n",
            encoding="utf-8",
        )
        self.write_rc(
            f"config.shell = {str(shell)!r}\n"
            "config.shell_args = ['--from-rc', 'two words']\n"
            "config.cwd = 'work'\nconfig.plugin_dir = 'plugins'\n"
            "config.plugins = ['saved', 'saved']\nconfig.lang = 'ko'\n"
            "config.escape_delay = 0.15\nconfig.log_file = 'session.log'\n"
            "config.log_level = 'debug'\n"
        )
        for run in (1, 2):
            status, output = self.terminal()
            self.assertEqual(status, 0, output)
            self.assertEqual(
                (self.root / "saved-runs").read_text(), "import\nsetup\ncleanup\n" * run
            )
            self.assertEqual((self.root / "cwd-result").read_text().strip(), str(work))
            self.assertEqual(
                (self.root / "argv-result").read_text(), "--from-rc\ntwo words\n"
            )
        self.assertTrue((self.home / "session.log").is_file())
        self.assertNotIn("\x1b[", output)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_cli_replaces_saved_plugins_and_can_disable_or_skip_rc(self):
        """One-shot choices neither append unwanted plugins nor rewrite saved settings."""
        shell = self.trap_shell()
        for name in ("saved", "once"):
            self.recording_plugin(name)
        self.write_rc(
            f"config.shell = {str(shell)!r}\nconfig.plugin_dir = 'plugins'\n"
            "config.plugins = ['saved']\nconfig.lang = 'ko'\n"
            "config.cwd = 'missing-work'\n"
        )
        original = (self.home / ".ishrc.py").read_bytes()
        status, output = self.terminal("--plugin", "once", "--cwd", ".", "--lang", "en")
        self.assertEqual(status, 0, output)
        self.assertFalse((self.root / "saved-runs").exists())
        self.assertEqual(
            (self.root / "once-runs").read_text(), "import\nsetup\ncleanup\n"
        )
        status, output = self.terminal("--no-plugins", "--cwd", ".")
        self.assertEqual(status, 0, output)
        self.assertFalse((self.root / "saved-runs").exists())
        self.assertEqual((self.home / ".ishrc.py").read_bytes(), original)
        self.write_rc("raise RuntimeError('BROKEN CONFIG')\n")
        status, output = self.terminal("--no-rc", "--shell", shell)
        self.assertEqual(status, 0, output)
        self.assertNotIn("BROKEN CONFIG", output)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_default_home_loads_saved_ish_settings(self):
        """Only ~/ish/.ishrc.py is executed when no --home override is present."""
        shell = self.trap_shell()
        directory = self.native_home / "ish"
        directory.mkdir()
        (directory / ".ishrc.py").write_text(f"config.shell = {str(shell)!r}\n")
        before = self.files()
        status, output = self.terminal(default_home=True)
        self.assertEqual(status, 0, output)
        self.assertEqual((self.root / "shell-executed").read_text(), "executed")
        after = self.files()
        del after["shell-executed"]
        self.assertEqual(after, before)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_invalid_rc_stops_before_starting_a_shell(self):
        """Typos and invalid choices fail explicitly without partially applying config."""
        shell = self.trap_shell()
        for choice in (
            "config.typo = True",
            "config.plugins = 'saved'",
            "config.shell_args = '-i'",
            "config.escape_delay = float('nan')",
            "config.escape_delay = True",
            "config.escape_delay = 0",
            "config.escape_delay = 3",
            "config.cwd = None",
            "config.log_level = 'NOPE'",
            "raise SystemExit(0)",
            "syntax error !",
        ):
            with self.subTest(choice=choice):
                self.write_rc(choice + "\n")
                status, output = self.terminal("--shell", shell, "--lang", "en")
                self.assertEqual(status, 2, output)
                self.assertIn(".ishrc.py", output)
                self.assertNotIn("Traceback", output)
                self.assertFalse((self.root / "shell-executed").exists())

    def test_cli_escape_delay_and_plugin_exclusion_are_validated(self):
        """CLI timing and mutually exclusive plugin policies reject bad values."""
        for args in (
            ("--escape-delay=nan",),
            ("--escape-delay=0",),
            ("--no-plugins", "--plugin", "one"),
        ):
            with self.subTest(args=args):
                result = self.cli("--diagnose", *args)
                self.assertEqual(result.returncode, 2, result.stderr)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_relative_shell_and_cli_override_keep_distinct_bases(self):
        """RC executable paths use its home, while explicit CLI paths use launch cwd."""
        shell = self.trap_shell()
        self.write_rc("config.shell = './named-shell'\n")
        (self.home / "named-shell").symlink_to(shell)
        status, output = self.terminal()
        self.assertEqual(status, 0, output)
        status, output = self.terminal("--shell", "./shell-trap")
        self.assertEqual(status, 0, output)
        (self.root / "shell-executed").unlink()
        self.write_rc("config.shell = '~/absent-shell'\nconfig.lang = 'ko'\n")
        status, output = self.terminal()
        self.assertEqual(status, 2, output)
        self.assertIn("셸 실행 파일을 찾을 수 없습니다", output)
        self.assertIn(str(self.native_home / "absent-shell"), output)
        self.assertFalse((self.root / "shell-executed").exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_directory_is_rejected_without_running_main(self):
        """A mistaken directory cannot turn run_path into package execution."""
        shell = self.trap_shell()
        directory = self.home / ".ishrc.py"
        directory.mkdir(parents=True)
        (directory / "__main__.py").write_text(
            "raise RuntimeError('DIRECTORY EXECUTED')"
        )
        status, output = self.terminal("--shell", shell)
        self.assertEqual(status, 2, output)
        self.assertIn("configuration must be a regular Python file", output)
        self.assertNotIn("DIRECTORY EXECUTED", output)
        self.assertFalse((self.root / "shell-executed").exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_exit_cleanup_can_finish_after_terminal_eof(self):
        """The harness must observe process completion after its last terminal write."""
        shell = self.trap_shell()
        marker = self.root / "exit-cleanup"
        self.write_rc(
            "import atexit, os, time\nfrom pathlib import Path\n"
            "def finish():\n"
            "    for fd in (0, 1, 2): os.close(fd)\n"
            "    time.sleep(0.1)\n"
            f"    Path({str(marker)!r}).write_text('complete')\n"
            "atexit.register(finish)\n"
        )
        status, output = self.terminal("--shell", shell)
        self.assertEqual(status, 0, output)
        self.assertEqual(marker.read_text(), "complete")

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
                self.assertIn("ish", result.stdout)
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
            ("--diagnose", "ish 환경 진단"),
        ):
            with self.subTest(flag=flag):
                result = self.cli(flag, "--lang", "ko")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.home.exists())

    def test_queries_choose_cli_then_lang_then_english_without_reading_rc(self):
        """LANG works without installed locales and cannot be superseded by LC_ALL."""
        marker = self.root / "rc-executed"
        self.write_rc(
            'config.lang = "ko"\nfrom pathlib import Path\n'
            f"Path({str(marker)!r}).write_text('executed')\n"
        )
        for language, cli, expected in (
            ("ko_KR.UTF-8", (), "사용법:"),
            ("ko_KR@custom", (), "사용법:"),
            ("en_US.UTF-8", (), "usage:"),
            ("ko_KR.UTF-8", ("--lang", "en"), "usage:"),
            ("en_US.UTF-8", ("--lang", "ko"), "사용법:"),
            (None, (), "usage:"),
            ("", (), "usage:"),
            ("C", (), "usage:"),
        ):
            with self.subTest(language=language, cli=cli):
                if language is None:
                    self.env.pop("LANG", None)
                else:
                    self.env["LANG"] = language
                # This variable deliberately disagrees with LANG's Korean case.
                self.env["LC_ALL"] = "C.UTF-8"
                result = self.cli("--help", *cli)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith(expected), result.stdout)
                self.assertFalse(marker.exists())

    def test_argparse_details_and_usage_are_translated_independently(self):
        """Pinned argparse errors use translated content, not only a Korean wrapper."""
        for options, expected in (
            (("--unknown",), "알 수 없는 인수: --unknown"),
            (("--shell",), "인수 --shell: 값 하나가 필요합니다"),
            (("--home",), "인수 --home: 값 하나가 필요합니다"),
            (
                ("--escape-delay=oops",),
                "인수 --escape-delay: 올바르지 않은 float 형식의 값: 'oops'",
            ),
            (
                ("--log-level=wrong",),
                "인수 --log-level: 선택할 수 없는 값: 'WRONG' (가능한 값:",
            ),
            (
                ("--login", "--shell-arg=-i"),
                "인수 --shell-arg: 인수 --login와 함께 사용할 수 없습니다",
            ),
            (
                ("--no-plugins", "--plugin", "one"),
                "인수 --plugin: 인수 --no-plugins와 함께 사용할 수 없습니다",
            ),
            (
                ("--help=value",),
                "인수 -h/--help: 이 옵션은 값을 받지 않습니다: 'value'",
            ),
            (
                ("--no-rc=value",),
                "인수 --no-rc: 이 옵션은 값을 받지 않습니다: 'value'",
            ),
        ):
            with self.subTest(options=options):
                result = self.cli(*options, "--lang", "ko")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("사용법:", result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("usage:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.home.exists())

    def test_invalid_language_uses_lang_for_the_complete_error(self):
        """An unusable explicit language falls back without leaking English context."""
        self.env["LANG"] = "ko_KR.UTF-8"
        for options, expected in (
            (("--lang",), "인수 --lang/-l: 값 하나가 필요합니다"),
            (("--lang", "../outside"), "언어"),
        ):
            with self.subTest(options=options):
                result = self.cli(*options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("사용법:", result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("Language must", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_cli_never_executes_rc_to_discover_its_language(self):
        """Invalid arguments use CLI/LANG; reporting cannot execute arbitrary rc code."""
        marker = self.root / "rc-executed"
        self.write_rc(
            'config.lang = "ko"\nfrom pathlib import Path\n'
            f"Path({str(marker)!r}).write_text('executed')\n"
        )
        self.env["LANG"] = "en_US.UTF-8"
        for options, expected in (
            ((), "unrecognized arguments: --unknown"),
            (("--lang", "ko"), "알 수 없는 인수: --unknown"),
        ):
            with self.subTest(options=options):
                result = self.cli("--unknown", *options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_execution_language_precedence_is_cli_then_rc_then_lang_then_english(self):
        """A real startup error observes independent expected messages for each source."""
        shell = self.trap_shell()
        for language, rc_language, options, expected in (
            ("en_US.UTF-8", "ko", (), "작업 디렉터리를 찾을 수 없습니다"),
            ("ko_KR.UTF-8", "en", (), "Working directory"),
            ("ko_KR.UTF-8", "ko", ("--lang", "en"), "Working directory"),
            ("en_US.UTF-8", "en", ("--lang", "ko"), "작업 디렉터리를 찾을 수 없습니다"),
            ("ko_KR.UTF-8", None, (), "작업 디렉터리를 찾을 수 없습니다"),
            (None, None, (), "Working directory"),
        ):
            with self.subTest(language=language, rc=rc_language, options=options):
                if language is None:
                    self.env.pop("LANG", None)
                else:
                    self.env["LANG"] = language
                self.write_rc(
                    f"config.lang = {rc_language!r}\nconfig.cwd = 'absent-work'\n"
                )
                status, output = self.terminal("--shell", shell, *options)
                self.assertEqual(status, 2, output)
                self.assertIn(expected, output)
                self.assertFalse((self.root / "shell-executed").exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_language_applies_to_validation_before_the_shell_starts(self):
        """Early validation retains the rc language instead of freezing LANG's English."""
        shell = self.trap_shell()
        for choice, expected in (
            (
                "config.shell = ''",
                "shell은 비어 있지 않은 실행 파일 이름 또는 경로여야 합니다",
            ),
            ("config.plugins = 'saved'", "plugins은 문자열 목록이어야 합니다"),
            ("config.shell_args = '-i'", "shell_args은 문자열 목록이어야 합니다"),
            (
                "config.plugins = ['']",
                "plugins에는 비어 있지 않은 이름을 지정해야 합니다",
            ),
            ("config.cwd = None", "cwd은 경로여야 합니다"),
            ("config.log_level = 'WRONG'", "log_level은"),
            (
                "config.escape_delay = 0",
                "escape_delay는 0.005초 이상 2.0초 이하여야 합니다",
            ),
        ):
            with self.subTest(choice=choice):
                self.write_rc(f"config.lang = 'ko'\n{choice}\n")
                status, output = self.terminal("--shell", shell)
                self.assertEqual(status, 2, output)
                self.assertIn("설정 파일", output)
                self.assertIn(str(self.home / ".ishrc.py"), output)
                self.assertIn(expected, output)
                self.assertNotIn("Traceback", output)
                self.assertFalse((self.root / "shell-executed").exists())
        self.write_rc("config.lang = 'ko'\nconfig.plugins = 'saved'\n")
        status, output = self.terminal("--shell", shell, "--lang", "en")
        self.assertEqual(status, 2, output)
        self.assertIn("plugins must be a list of strings", output)
        self.assertNotIn("설정 파일", output)

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_exception_keeps_foreign_detail_under_the_selected_language(self):
        """The ish error is translated even if user code fails after setting its language."""
        self.write_rc("config.lang = 'ko'\nraise RuntimeError('USER DETAIL')\n")
        status, output = self.terminal("--shell", self.trap_shell())
        self.assertEqual(status, 2, output)
        self.assertIn("설정 파일", output)
        self.assertIn("RuntimeError: USER DETAIL", output)
        self.assertNotIn("Traceback", output)
        self.assertFalse((self.root / "shell-executed").exists())

    @unittest.skipUnless(os.name == "posix", "requires a controlling PTY")
    def test_rc_language_replaces_provisional_translation_warnings_once(self):
        """A broken English fallback reports once in the final rc-selected language."""
        shell = self.trap_shell()
        self.write_rc(
            "config.lang = 'ko'\nconfig.log_level = 'WARNING'\n"
            "config.log_file = 'startup.log'\n"
        )
        languages = self.home / "lang"
        languages.mkdir()
        (languages / "en.json").write_text("{", encoding="utf-8")
        for language in ("en_US.UTF-8", "ko_KR.UTF-8"):
            with self.subTest(language=language):
                self.env["LANG"] = language
                log_file = self.home / "startup.log"
                log_file.unlink(missing_ok=True)
                status, output = self.terminal("--shell", shell)
                self.assertEqual(status, 0, output)
                records = log_file.read_text(encoding="utf-8")
                for messages in (output, records):
                    self.assertEqual(
                        messages.count("을 불러올 수 없습니다"), 1, messages
                    )
                    self.assertNotIn("Cannot load language file", messages)
                self.assertEqual(output.count("[WARNING]"), 1, output)
                self.assertEqual((self.root / "shell-executed").read_text(), "executed")

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
                    self.assertIn("ish", result.stdout)
                    self.assertEqual(
                        result.stderr.count("을 불러올 수 없습니다"), warnings
                    )
                    self.assertEqual(result.stderr.count("[WARNING]"), warnings)
                    if not warnings:
                        self.assertEqual(result.stderr, "")
                    self.assertEqual(self.files(), before)

    def test_version_uses_stream_name(self):
        """The stream entry point keeps an independently recognizable version label."""
        result = self.cli("--lang", "en", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ish 2.0.0\n")

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


if __name__ == "__main__":
    unittest.main()
