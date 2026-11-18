"""Verify public VT startup choices, native results and read-only query boundaries."""

from __future__ import annotations

import json
import sys
import unittest

from startup_fixture import StartupFixture
from test_terminal_cli import Terminal


class StartupTests(StartupFixture, unittest.TestCase):
    """Reuse isolated rc/PTY fixtures; expectations are public output and file bytes."""

    def run_terminal(self, *args, shell="/bin/sh"):
        """Start actual PTK/PTY and require process cleanup before reading outputs."""
        terminal = Terminal(
            self.root,
            shell=shell,
            configure=False,
            extra_args=("--home", str(self.home), *map(str, args)),
            environment=self.env,
        )
        self.addCleanup(terminal.close)
        terminal.require(terminal.exited, timeout=30)
        terminal.receive()
        return terminal.status, bytes(terminal.output).decode(errors="replace")

    def test_queries_do_not_execute_rc_plugins_shell_or_native_library(self):
        """Queries work with active rc, import traps and an unusable native library."""
        marker = self.root / "rc-executed"
        self.write_rc(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('rc trap')\n"
        )
        plugin = self.trap_plugin()
        shell = self.trap_shell()
        self.env["ISH_GHOSTTY_LIB"] = str(self.root / "missing-native.so")
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
                    plugin,
                    "--log-file",
                    self.root / "query.log",
                    "--dump-state",
                    self.root / "query-state.json",
                    "--feature",
                    "enhanced_keyboard=on",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.files(), before)
        self.assertIn("Native library file exists (not loaded): No", result.stdout)

    def test_missing_home_queries_create_no_files(self):
        """Read-only queries never create config, library, cache, language or log paths."""
        before = self.files()
        for flag in ("--help", "--version", "--diagnose"):
            result = self.cli(flag, "--log-file")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.files(), before)

    def test_help_and_diagnostics_describe_the_single_terminal_runtime(self):
        """The public runtime advertises terminal policy and no retired options."""
        help_result = self.cli("--help", "--lang", "en")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for text in (
            "--mouse",
            "--scrollback-lines",
            "--no-rc",
            "--no-plugins",
            "--escape-delay",
            "--feature",
            "--login",
            "--config",
        ):
            self.assertIn(text, help_result.stdout)
        for text in (
            "--integration-dir",
            "--library-dir",
            "--demo",
            "--dump-state",
            "fall back to native operation",
            "ish-vt",
        ):
            self.assertNotIn(text, help_result.stdout)
        result = self.cli("--diagnose", "--lang", "en")
        self.assertEqual(self.field(result.stdout, "Settings object"), "config")
        self.assertEqual(self.field(result.stdout, "Mouse capture"), "auto")
        self.assertEqual(
            self.field(result.stdout, "Escape sequence delay (seconds)"), "0.02"
        )
        self.assertIn("0.02", help_result.stdout)
        self.assertEqual(self.field(result.stdout, "History line limit"), "10000")
        self.assertEqual(self.field(result.stdout, "History byte limit"), "33554432")
        self.assertEqual(self.field(result.stdout, "Selected plugins"), "Disabled")
        self.assertEqual(self.field(result.stdout, "Color depth policy"), "inherit")

    def test_language_is_cli_then_lang_then_builtin_english(self):
        """Locale variables other than LANG cannot override the explicit policy."""
        self.env.update(LANG="ko_KR.UTF-8", LC_ALL="C", LC_MESSAGES="C", LANGUAGE="en")
        result = self.cli("--help")
        self.assertIn("셸 hook 없이", result.stdout)
        result = self.cli("--help", "--lang", "en")
        self.assertIn("hookless libghostty-vt", result.stdout)
        self.env["LANG"] = "C.UTF-8"
        result = self.cli("--version")
        self.assertEqual(result.stdout.strip(), "ish 2.0.0")

    def test_feature_diagnostics_keep_forced_policy_separate_from_unprobed_support(
        self,
    ):
        """A read-only forced request does not become a positive support observation."""
        before = self.files()
        for language, fragment in (
            (
                "en",
                "enhanced_keyboard: policy=on, support=not-probed, active=not-evaluated",
            ),
            ("ko", "enhanced_keyboard: 요청=on, 관찰=not-probed, 사용=not-evaluated"),
        ):
            with self.subTest(language=language):
                result = self.cli(
                    "--diagnose",
                    "--lang",
                    language,
                    "--feature",
                    "enhanced_keyboard=off",
                    "--feature",
                    "enhanced_keyboard=on",
                    "--clipboard",
                    "off",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(fragment, result.stdout)
                self.assertIn("clipboard_protocol", result.stdout)
                self.assertEqual(self.files(), before)

    def test_removed_options_are_rejected_without_loading_rc(self):
        """Removed execution paths and aliases cannot silently select a fallback."""
        marker = self.root / "rc-executed"
        self.write_rc(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        for option in ("--integration-dir=unused", "--library-dir=unused", "--demo"):
            with self.subTest(option=option):
                result = self.cli("--diagnose", option)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(option.split("=", 1)[0], result.stderr)
                self.assertFalse(marker.exists())

    def test_invalid_cli_is_translated_and_never_runs_rc(self):
        """Policy and parsing failures stay before arbitrary initialization."""
        marker = self.root / "rc-executed"
        self.write_rc(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        for options in (
            ("--mouse", "bad"),
            ("--color-depth", "16"),
            ("--color-depth", "auto"),
            ("--feature", "hyperlinks=auto"),
            ("--feature", "unknown-feature=on"),
            ("--feature", "enhanced_keyboard=maybe"),
            ("--feature", "enhanced_keyboard"),
            ("--escape-delay", "0"),
            ("--scrollback-bytes", "-1"),
            ("--scrollback-lines", str(1 << 63)),
            ("--login", "--shell-arg=-i"),
        ):
            with self.subTest(options=options):
                result = self.cli("--diagnose", "--lang", "ko", *options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("오류", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(marker.exists())

    def test_native_shell_arguments_and_cwd_are_preserved(self):
        """The native consumer sees exact argv and cwd instead of injected -i."""
        shell = self.trap_shell()
        shell.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > argv-result\npwd > cwd-result\n"
        )
        work = self.root / "work"
        work.mkdir()
        status, output = self.run_terminal(
            "--cwd", work, "--shell-arg=--first", "--shell-arg=two words", shell=shell
        )
        self.assertEqual(status, 0, output)
        self.assertEqual((work / "argv-result").read_bytes(), b"--first\ntwo words\n")
        self.assertEqual((work / "cwd-result").read_text().strip(), str(work))
        status, output = self.run_terminal("--cwd", work, "--login", shell=shell)
        self.assertEqual(status, 0, output)
        self.assertEqual((work / "argv-result").read_bytes(), b"-l\n")

    def test_saved_plugins_persist_once_per_session(self):
        """One selection list loads each plugin once and applies flat policies."""
        self.recording_plugin("saved")
        self.write_rc(
            "config.plugins = ['saved', 'saved']\n"
            "config.plugin_dir = 'plugins'\nconfig.lang = 'ko'\n"
            "config.mouse = 'off'\nconfig.clipboard = 'off'\n"
            "config.escape_delay = 0.08\nconfig.scrollback_lines = 42\n"
            "config.scrollback_bytes = 65536\n"
        )
        original = (self.home / ".ishrc.py").read_bytes()
        for count in (1, 2):
            status, output = self.run_terminal("--shell-arg=-c", "--shell-arg=exit 0")
            self.assertEqual(status, 0, output)
            self.assertEqual(
                (self.root / "saved-runs").read_text(),
                "import\nsetup\ncleanup\n" * count,
            )
            state = json.loads((self.root / "terminal-state.json").read_text())
            self.assertEqual(state["mouse_policy"], "off")
            self.assertEqual(state["clipboard_policy"], {"read": False, "write": False})
            self.assertEqual(state["popups"], [])
        self.assertEqual((self.home / ".ishrc.py").read_bytes(), original)

    def test_cli_plugin_selection_replaces_saved_and_no_plugins_disables(self):
        """Overrides affect the new session without rewriting persisted choices."""
        self.recording_plugin("saved")
        self.recording_plugin("once")
        self.write_rc("config.plugin_dir = 'plugins'\nconfig.plugins = ['saved']\n")
        original = (self.home / ".ishrc.py").read_bytes()
        status, output = self.run_terminal(
            "--plugin", "once", "--shell-arg=-c", "--shell-arg=exit 0"
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(
            (self.root / "once-runs").read_text(), "import\nsetup\ncleanup\n"
        )
        self.assertFalse((self.root / "saved-runs").exists())
        status, output = self.run_terminal(
            "--no-plugins", "--shell-arg=-c", "--shell-arg=exit 0"
        )
        self.assertEqual(status, 0, output)
        self.assertFalse((self.root / "saved-runs").exists())
        self.assertEqual((self.home / ".ishrc.py").read_bytes(), original)

    def test_feature_overrides_merge_saved_keys_and_last_cli_choice(self):
        """Per-key CLI changes preserve other saved policies and the native consumer."""
        script = self.root / "feature-reader.py"
        script.write_text(
            "from pathlib import Path\nprint('FEATURE_READY',flush=True)\nPath('feature-input').write_text(input())\n"
        )
        self.write_rc(
            "config.features = {'hyperlinks': 'off', 'enhanced_keyboard': 'off', 'pixel_mouse': 'off'}\n"
        )
        original = (self.home / ".ishrc.py").read_bytes()
        cases = (
            (
                ("--feature=enhanced_keyboard=on", "--feature=enhanced_keyboard=auto"),
                "off",
            ),
            (("--no-rc", "--feature=hyperlinks=off"), "auto"),
        )
        for options, pixel_policy in cases:
            with self.subTest(options=options):
                terminal = Terminal(
                    self.root,
                    shell=sys.executable,
                    configure=False,
                    extra_args=(
                        "--home",
                        str(self.home),
                        "--no-plugins",
                        *options,
                        "--shell-arg=" + str(script),
                    ),
                    environment=self.env,
                )
                try:
                    terminal.require(
                        lambda terminal=terminal: terminal.frame_has("FEATURE_READY")
                    )
                    features = terminal.state()["features"]
                    self.assertEqual(features["hyperlinks"]["policy"], "off")
                    self.assertEqual(features["enhanced_keyboard"]["policy"], "auto")
                    self.assertEqual(features["pixel_mouse"]["policy"], pixel_policy)
                    self.assertFalse(features["hyperlinks"]["active"])
                    terminal.send(b"native input\r")
                    terminal.require(terminal.exited)
                    self.assertEqual(terminal.status, 0)
                    self.assertEqual(
                        (self.root / "feature-input").read_text(), "native input"
                    )
                finally:
                    terminal.close()
        self.assertEqual((self.home / ".ishrc.py").read_bytes(), original)

    def test_no_rc_skips_broken_config(self):
        """An explicit bypass remains available without editing either shell's rc."""
        self.write_rc("raise RuntimeError('broken config')\n")
        status, output = self.run_terminal(
            "--no-rc", "--shell-arg=-c", "--shell-arg=exit 0"
        )
        self.assertEqual(status, 0, output)
        self.assertNotIn("broken config", output)

    def test_explicit_language_and_cli_precedence_apply_after_rc(self):
        """The saved language beats LANG and an explicit CLI language beats both."""
        self.env["LANG"] = "en_US.UTF-8"
        self.write_rc("config.lang = 'ko'\n")
        status, output = self.run_terminal(shell="missing-test-shell-executable")
        self.assertEqual(status, 2, output)
        self.assertIn("셸 실행 파일", output)
        status, output = self.run_terminal(
            "--lang", "en", shell="missing-test-shell-executable"
        )
        self.assertEqual(status, 2, output)
        self.assertIn("Shell executable not found", output)

    def test_obsolete_namespaces_fail_before_plugins_or_shell(self):
        """Old namespaces fail visibly instead of being migrated or ignored."""
        plugin = self.trap_plugin()
        shell = self.trap_shell()
        for assignment in (
            "config.vt = {'mouse': 'off'}",
            "config.integration_dir = 'old'",
        ):
            with self.subTest(assignment=assignment):
                self.write_rc(
                    f"config.shell = {str(shell)!r}\n"
                    f"config.plugin_dir = {str(plugin)!r}\n"
                    "config.plugins = ['selected']\n" + assignment + "\n"
                )
                status, output = self.run_terminal(shell=str(shell))
                self.assertEqual(status, 2, output)
                self.assertIn("AttributeError", output)
                self.assertIn(assignment.split("=", 1)[0].strip().split(".")[1], output)
                self.assertFalse((self.root / "shell-executed").exists())
                self.assertFalse((self.root / "plugin-executed").exists())

    def test_invalid_settings_fail_before_native_shell(self):
        """Misspelled fields and invalid limits cannot silently alter policy."""
        for assignment in (
            "config.mose = 'on'",
            "config.scrollback_bytes = True",
            "config.clipboard = 'invalid'",
            "config.features = []",
            "config.features = None",
            "config.features = {'hyperlinks': 'auto'}",
            "config.features = {'enhanced_keyboard': True}",
            "config.features = {'unknown': 'on'}",
            "config.color_depth = 'auto'",
            "config.plugins = [None]",
        ):
            with self.subTest(assignment=assignment):
                self.write_rc("config.lang = 'ko'\n" + assignment + "\n")
                status, output = self.run_terminal(shell=self.trap_shell())
                self.assertEqual(status, 2, output)
                self.assertIn("설정", output)
                self.assertFalse((self.root / "shell-executed").exists())

    def test_selected_file_logs_exclude_native_payload_and_obey_level(self):
        """Only session diagnostics reach optional logs; child output remains private."""
        log = self.root / "session.log"
        status, output = self.run_terminal(
            "--log-file",
            log,
            "--log-level",
            "INFO",
            "--shell-arg=-c",
            "--shell-arg=printf SECRET_NATIVE_PAYLOAD",
        )
        self.assertEqual(status, 0, output)
        records = log.read_text()
        self.assertIn("VT session start", records)
        self.assertIn("VT session exit status=0", records)
        self.assertNotIn("SECRET_NATIVE_PAYLOAD", records)
        log.unlink()
        status, output = self.run_terminal(
            "--log-file",
            log,
            "--log-level",
            "ERROR",
            "--shell-arg=-c",
            "--shell-arg=exit 0",
        )
        self.assertEqual(status, 0, output)
        self.assertNotIn("VT session start", log.read_text())

    def test_failed_protocol_adapter_is_reported_without_its_payload(self):
        """A failed supplement cannot disappear silently or leak child data to logs."""
        directory = self.home / "plugins" / "codec"
        directory.mkdir(parents=True)
        (directory / "codec.py").write_text(
            "PLUGIN_META = {'version': '1.0'}\nTERMINAL_API = 1\n"
            "class Codec:\n"
            "    def handle(self, control): raise ValueError('PRIVATE_CODEC_PAYLOAD')\n"
            "    def cancel(self): pass\n"
            "    def close(self): pass\n"
            "def setup(api):\n"
            "    api.register_protocol('test-codec', kind='osc', prefixes=(b'7400;',), factory=Codec)\n"
        )
        self.write_rc("config.plugin_dir = 'plugins'\nconfig.plugins = ['codec']\n")
        log = self.root / "codec.log"
        status, output = self.run_terminal(
            "--log-file",
            log,
            "--shell-arg=-c",
            "--shell-arg=printf '\\033]7400;input\\007'",
        )
        self.assertEqual(status, 0, output)
        expected = "Protocol adapter failed: test-codec (1 errors; payload omitted)"
        self.assertIn(expected, log.read_text())
        self.assertIn(expected, output)
        self.assertNotIn("PRIVATE_CODEC_PAYLOAD", log.read_text())
        self.assertNotIn("PRIVATE_CODEC_PAYLOAD", output)

    def test_image_rejections_are_counted_without_retaining_payload_in_logs(self):
        """Real PTY input produces bounded diagnostics after normal PTK cleanup."""
        script = self.root / "rejected-images.py"
        script.write_text(
            "import os\n"
            "os.write(1, b'\\x1b]1337;File=name=PRIVATE_IMAGE_INPUT\\x07' * 19)\n"
            "os.write(1, b'IMAGE_REJECTION_SURVIVED\\r\\n')\n"
            "input()\n"
        )
        log = self.root / "images.log"
        terminal = Terminal(
            self.root,
            shell=sys.executable,
            configure=False,
            extra_args=(
                "--home",
                str(self.home),
                "--no-rc",
                "--lang",
                "ko",
                "--log-file",
                str(log),
                "--log-level",
                "INFO",
                "--shell-arg=" + str(script),
            ),
            environment=self.env,
        )
        self.addCleanup(terminal.close)
        # Cell metadata can separate adjacent glyphs in raw output. Observe the
        # independent rendered cells before releasing the real native consumer.
        terminal.require(lambda: terminal.frame_has("IMAGE_REJECTION_SURVIVED"))
        terminal.send(b"\r")
        terminal.require(terminal.exited, timeout=30)
        terminal.receive()
        output = bytes(terminal.output).decode(errors="replace")
        self.assertEqual(terminal.status, 0, output)
        records = log.read_text()
        self.assertIn("이미지 입력 거부: 오류 19개(본문 제외)", records)
        self.assertIn(
            "지원하는 호스트 이미지 출력 프로토콜을 확인하지 못했습니다", records
        )
        self.assertNotIn("PRIVATE_IMAGE_INPUT", records)
        self.assertNotIn("IMAGE_REJECTION_SURVIVED", records)
        self.assertNotIn("Missing inline image payload", records)
        self.assertNotIn("PRIVATE_IMAGE_INPUT", output)

    def test_diagnose_explains_unsupported_controls_without_claiming_host_support(self):
        """Read-only policy reports cannot advertise unprobed host features."""
        for language, control, host in (
            (
                "en",
                "No general pass-through; native parsing and registered adapters only.",
                "--clipboard does not enable host support",
            ),
            (
                "ko",
                "일괄 직접 전달 없이 native 해석과 등록한 adapter만 사용합니다",
                "--clipboard가 호스트 기능을 활성화하지는 않습니다",
            ),
        ):
            with self.subTest(language=language):
                result = self.cli("--diagnose", "--lang", language)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(control, result.stdout)
                self.assertIn(host, result.stdout)

    def test_color_depth_preserves_explicit_rgb_and_respects_ptk_environment(self):
        """Feature gates remain independent of inherited or explicitly selected RGB."""
        script = self.root / "color.py"
        script.write_text(
            "import os\n"
            "os.write(1, b'\\x1b[38;2;17;34;51mCOLOR_RGB_CHECK\\x1b[0m\\r\\n')\n"
            "input()\n"
        )
        self.write_rc("config.color_depth = '24'\n")
        cases = (
            (("--no-rc",), "", False),
            (("--no-rc", "--color-depth", "24"), "", True),
            (("--no-rc",), "DEPTH_24_BIT", True),
            (("--no-rc", "--color-depth", "8"), "DEPTH_24_BIT", False),
            ((), "", True),
        )
        for options, environment_depth, expected_rgb in cases:
            with self.subTest(options=options, environment_depth=environment_depth):
                terminal = Terminal(
                    self.root,
                    shell=sys.executable,
                    configure=False,
                    extra_args=(
                        "--home",
                        str(self.home),
                        "--no-plugins",
                        "--feature=hyperlinks=off",
                        *options,
                        "--shell-arg=" + str(script),
                    ),
                    environment={
                        "NO_COLOR": "",
                        "PROMPT_TOOLKIT_COLOR_DEPTH": environment_depth,
                    },
                )
                try:
                    terminal.require(
                        lambda terminal=terminal: terminal.frame_has("COLOR_RGB_CHECK")
                    )
                    self.assertEqual(b"38;2;17;34;51" in terminal.output, expected_rgb)
                    self.assertEqual(terminal.state()["host_adapter"], "unknown")
                    self.assertFalse(terminal.state()["host"]["hyperlinks"])
                    self.assertFalse(terminal.state()["host"]["enhanced_keyboard"])
                    terminal.send(b"\r")
                    terminal.require(terminal.exited)
                    self.assertEqual(terminal.status, 0)
                finally:
                    terminal.close()

    def test_missing_native_library_translates_guidance_and_retains_oserror(self):
        """An absent shared object has Korean recovery steps and unchanged loader detail."""
        missing = self.root / "missing-native.so"
        self.env["ISH_GHOSTTY_LIB"] = str(missing)
        status, output = self.run_terminal(
            "--no-rc", "--lang", "ko", "--shell-arg=-c", "--shell-arg=exit 0"
        )
        self.assertEqual(status, 1, output)
        self.assertIn("파일을 불러올 수 없습니다", output)
        self.assertIn("tools/build_ghostty_vt.py를 실행", output)
        self.assertIn("ISH_GHOSTTY_LIB", output)
        self.assertIn(str(missing), output)
        self.assertIn("cannot open shared object file", output)
        self.assertNotIn("Cannot load", output)
        self.assertNotIn("Run tools/build_ghostty_vt.py", output)

    def test_native_processing_failures_are_translated(self):
        """Injected bridge failure reaches public feed without English-only text."""
        from unittest.mock import patch

        from ish.lang.i18n import I18N
        from ish.terminal.vt import Terminal as NativeTerminal
        from ish.terminal.vt import TerminalError

        language = I18N()
        language.base_path = self.root / "languages"
        language.load("ko")
        with patch("ish.terminal.vt.i18n", language):
            with NativeTerminal(10, 4, lambda _: None) as terminal:
                with patch.object(terminal._library, "ish_vt_feed", return_value=-7):
                    with self.assertRaisesRegex(
                        TerminalError, "처리에 실패했습니다\\(상태 -7\\)"
                    ):
                        terminal.feed(b"native failure probe")
            with self.assertRaisesRegex(TerminalError, "터미널이 닫혔습니다"):
                terminal.frame()

    def test_buffer_limit_failure_is_reported_after_application_cleanup(self):
        """The public CLI treats a bounded transport failure as a session error."""
        import io
        from contextlib import redirect_stderr
        from types import SimpleNamespace
        from unittest.mock import patch

        from ish.lang.i18n import I18N
        from ish.terminal.__main__ import main

        language = I18N()
        language.base_path = self.root / "languages"
        language.load("ko")
        state = []

        async def fail():
            try:
                raise BufferError("QUEUE_LIMIT")
            finally:
                state.append("restored")

        application = SimpleNamespace(
            run=fail, stop=lambda _: None, suspend=lambda: None
        )
        options = SimpleNamespace(
            log_level="WARNING",
            log_file=None,
            log_records=(),
            plugin=(),
            cwd=self.root,
            dump_state=None,
            plugin_dir=None,
            plugin_lib_dir=None,
            clipboard="write",
            mouse="auto",
            color_depth="inherit",
            features={},
            escape_delay=0.05,
            scrollback_lines=10000,
            scrollback_bytes=32 * 1024 * 1024,
        )
        output = io.StringIO()

        def report(application, logger):
            self.assertEqual(state, ["restored"])
            self.assertEqual(output.getvalue(), "")
            state.append("reported")

        with (
            patch("ish.terminal.__main__.parse", return_value=(None, options, False)),
            patch("ish.terminal.__main__.command", return_value=("/bin/sh",)),
            patch("ish.terminal.__main__.i18n", language),
            patch("ish.terminal.app.TerminalApp", return_value=application),
            patch("ish.terminal.diagnostics.report_session", side_effect=report),
            redirect_stderr(output),
        ):
            self.assertEqual(main([]), 1)
        self.assertEqual(state, ["restored", "reported"])
        self.assertIn("ish 세션 오류: QUEUE_LIMIT", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
