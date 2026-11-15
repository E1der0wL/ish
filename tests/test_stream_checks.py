"""Verify the public regression command rejects incomplete or skipped coverage."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 20
CONTRACT_MODULES = (
    "test_stream_bash",
    "test_stream_checks",
    "test_stream_cli",
    "test_stream_distribution",
    "test_stream_extensions",
    "test_stream_extensions_shells",
    "test_stream_helper",
    "test_stream_host",
    "test_stream_logging",
    "test_stream_options",
    "test_stream_panel",
    "test_stream_posix",
    "test_stream_protocol",
    "test_stream_requests",
    "test_stream_scripts",
    "test_stream_scroll",
    "test_stream_shells",
    "test_stream_startup",
    "test_stream_state",
    "test_stream_zsh",
    "test_logging",
    "test_diagnostics",
    "test_i18n",
    "test_plugin_loading",
    "test_plugin_dependencies",
    "test_terminal_session",
    "test_tcsh_completion",
)
PASSING_TEST = """import unittest
from pathlib import Path

class Contract(unittest.TestCase):
    def test_observed_result(self):
        Path(__file__).with_suffix(".ran").write_text("executed")
"""
SHELL_CASES = (
    (
        "bash",
        "ISH_TEST_BASH",
        "test_stream_bash.BashTests.test_continuation_and_heredoc_reserve_f2_without_popup",
        "test_stream_posix.BashCliTests.test_default_output_and_input_have_no_ui",
    ),
    (
        "zsh",
        "ISH_TEST_ZSH",
        "test_stream_zsh.ZshTests.test_primary_hides_before_execution_and_returns_after_empty_error",
        "test_stream_posix.ZshCliTests.test_default_output_and_input_have_no_ui",
    ),
    (
        "tcsh",
        "ISH_TEST_TCSH",
        "test_stream_shells.ShellTests.test_heredoc_preserves_unicode_spaces_and_tool_names",
        "test_stream_cli.StreamCliTests.test_default_output_is_exact_and_no_ui_is_added",
    ),
)


@unittest.skipUnless(
    sys.platform.startswith("linux"), "requires the Linux check runner"
)
class CheckCommandTests(unittest.TestCase):
    """Observe exit codes and executed test files in an isolated fixture project."""

    def setUp(self):
        """Copy the real command without changing its paths, discovery or result logic."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-check-command-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        shutil.copy2(
            ROOT / "tools/check_stream.py", self.root / "tools/check_stream.py"
        )
        shutil.copy2(
            ROOT / "tests/stream_fixture.py", self.root / "tests/stream_fixture.py"
        )
        (self.root / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.12.14,<3.13"\ndependencies = []\n'
            "[dependency-groups]\ndev = []\n"
        )
        for name in CONTRACT_MODULES:
            (self.root / "tests" / f"{name}.py").write_text(PASSING_TEST)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for name in ("ISH_TEST_BASH", "ISH_TEST_TCSH", "ISH_TEST_ZSH"):
            self.env.pop(name, None)

    def run_check(self, *, cwd=None):
        """Invoke the user-facing command as a fresh process."""
        return subprocess.run(
            [sys.executable, str(self.root / "tools/check_stream.py"), "--tests-only"],
            cwd=self.root if cwd is None else cwd,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )

    def test_new_stream_tests_are_discovered_and_executed(self):
        """New modules enter the check without an extra CI allowlist edit."""
        (self.root / "tests/test_stream_added.py").write_text(PASSING_TEST)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 28 tests", result.stderr)
        self.assertEqual(len(list((self.root / "tests").glob("*.ran"))), 28)

    def test_skip_is_a_failed_check(self):
        """A missing test capability cannot yield a green process exit."""
        (self.root / "tests/test_stream_added.py").write_text(
            PASSING_TEST.replace(
                "    def test_observed_result",
                '    @unittest.skip("fixture unavailable")\n    def test_observed_result',
            )
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED (skipped=1)", result.stderr)
        self.assertFalse((self.root / "tests/test_stream_added.ran").exists())

    def test_missing_required_module_fails_before_tests_run(self):
        """Every maintained module, including this checker test, must be present."""
        for name in CONTRACT_MODULES:
            with self.subTest(module=name):
                path = self.root / "tests" / f"{name}.py"
                path.unlink()
                try:
                    result = self.run_check()
                finally:
                    path.write_text(PASSING_TEST)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(
                    f"Required test modules are missing: {name}", result.stderr
                )
                self.assertEqual(list((self.root / "tests").glob("*.ran")), [])

    def test_empty_module_is_rejected(self):
        """An accidentally empty new test file is reported instead of counted as covered."""
        (self.root / "tests/test_stream_added.py").write_text("# No tests.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "Test module collected no tests: test_stream_added", result.stderr
        )

    def test_import_failure_is_a_failed_check(self):
        """A broken test module is an error rather than an omitted suite."""
        (self.root / "tests/test_stream_added.py").write_text(
            'raise ImportError("fixture import failed")\n'
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture import failed", result.stderr)
        self.assertIn("FAILED (errors=1)", result.stderr)

    def test_missing_native_programs_fail_before_tests_run(self):
        """Missing shells stop collection before unittest skip decorators can hide them."""
        self.env["PATH"] = str(self.root / "empty-path")
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "Required native programs are missing: bash, zsh, tcsh, vim", result.stderr
        )
        self.assertEqual(list((self.root / "tests").glob("*.ran")), [])

    def test_selected_shell_path_and_version_are_observed(self):
        """Reports execute the selected binary instead of labeling system Bash as it."""
        executable = self.root / "selected-bash"
        executable.write_text(
            "#!/bin/sh\n"
            'test "$1" = --version || exit 3\n'
            "printf selected-version\n"
            'printf checked >> "$(dirname "$0")/version-probe"\n'
        )
        executable.chmod(0o755)
        self.env["ISH_TEST_BASH"] = "./selected-bash"
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ISH_TEST_BASH: {executable}\nselected-version", result.stdout)
        self.assertEqual((self.root / "version-probe").read_text(), "checked")

    def test_selected_shells_do_not_require_system_copies(self):
        """Explicit shell selections replace missing PATH defaults in the checker."""
        native = self.root / "native"
        native.mkdir()
        vim = native / "vim"
        vim.write_text("#!/bin/sh\nprintf '%s\\n' fixture-vim\n")
        vim.chmod(0o755)
        self.env["PATH"] = str(native)
        for shell, variable, _, _ in SHELL_CASES:
            executable = self.root / ("selected-" + shell)
            executable.write_text(
                '#!/bin/sh\ntest "$1" = --version || exit 3\n'
                + "printf "
                + shlex.quote("selected-" + shell)
                + "\n"
            )
            executable.chmod(0o755)
            self.env[variable] = str(executable)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for shell, variable, _, _ in SHELL_CASES:
            executable = self.root / ("selected-" + shell)
            self.assertIn(f"{variable}: {executable}\nselected-{shell}", result.stdout)

    def test_relative_shell_override_uses_the_callers_directory(self):
        """Running an absolute checker path preserves an outside caller's ./binary."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-check-caller-")
        self.addCleanup(temporary.cleanup)
        caller = Path(temporary.name)
        executable = caller / "selected-bash"
        executable.write_text(
            "#!/bin/sh\n"
            'test "$1" = --version || exit 3\n'
            "printf caller-version\n"
            'printf checked >> "$(dirname "$0")/version-probe"\n'
        )
        executable.chmod(0o755)
        wrong = self.root / "selected-bash"
        wrong.write_text(
            "#!/bin/sh\n"
            'printf wrong > "$(dirname "$0")/wrong-binary"\n'
            "printf project-version\n"
        )
        wrong.chmod(0o755)
        self.env["ISH_TEST_BASH"] = "./selected-bash"
        result = self.run_check(cwd=caller)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ISH_TEST_BASH: {executable}\ncaller-version", result.stdout)
        self.assertEqual((caller / "version-probe").read_text(), "checked")
        self.assertFalse((self.root / "wrong-binary").exists())


@unittest.skipUnless(
    sys.platform.startswith("linux"), "requires native Linux shell fixtures"
)
class ShellSelectionTests(unittest.TestCase):
    """Observe selected executables running actual adapter and public CLI tests."""

    def setUp(self):
        """Keep wrapper invocation records separate from the shell's test homes."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-selected-shell-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "selected"
        self.bin.mkdir()

    def selected_test(self, shell, variable, test, *, relative=False):
        """Run a maintained native test through a recording executable wrapper."""
        binary = shutil.which(shell)
        if binary is None:
            self.skipTest(f"{shell} is required")
        record = self.root / (shell + ".calls")
        wrapper = self.bin / shell
        wrapper.write_text(
            "#!/bin/sh\n"
            + "printf '%s\\n' \"$*\" >> "
            + shlex.quote(str(record))
            + "\nexec "
            + shlex.quote(binary)
            + ' "$@"\n'
        )
        wrapper.chmod(0o755)
        record.unlink(missing_ok=True)
        environment = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))),
        )
        for _, name, _, _ in SHELL_CASES:
            environment.pop(name, None)
        environment[variable] = "./selected/" + shell if relative else str(wrapper)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-v", test],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("skipped=", result.stderr)
        self.assertTrue(record.is_file(), f"{test} bypassed the selected shell")
        self.assertTrue(
            any("-i" in line.split() for line in record.read_text().splitlines()),
            record.read_text(),
        )

    def test_adapter_and_cli_execute_each_selected_shell(self):
        """Both test layers execute each chosen binary, not a system substitute."""
        for shell, variable, adapter, cli in SHELL_CASES:
            for test in (adapter, cli):
                with self.subTest(shell=shell, test=test):
                    self.selected_test(shell, variable, test)

    def test_relative_selections_survive_native_fixture_directory_changes(self):
        """The caller's wrapper still runs after native fixtures change directory."""
        for shell, variable, adapter, _ in SHELL_CASES:
            with self.subTest(shell=shell):
                self.selected_test(shell, variable, adapter, relative=True)
