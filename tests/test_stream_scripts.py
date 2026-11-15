"""Exercise optional external scripts through the public terminal entry point."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ish.stream.scripts import render
from stream_fixture import RAW_OUTPUT, shell_executable
from test_stream_cli import BAR, F2, PROMPT, ROOT, StreamProcess

SCRIPTS = ROOT / "src/ish/stream/integrations"
ENTRY = {"bash": "bash.sh", "zsh": "zsh.env", "tcsh": "tcsh.csh"}


class ScriptTests(unittest.TestCase):
    """Keep missing or incompatible optional UI separate from native execution."""

    def setUp(self):
        """Isolate script edits, user startup and final command files."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-script-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def start(self, shell, directory, *, demo):
        """Launch native shell startup once with the explicit integration path."""
        if not shutil.which(shell_executable(shell)):
            self.skipTest(f"{shell} is required")
        home = self.root / shell
        home.mkdir()
        process = StreamProcess(
            home,
            shell=shell,
            startup="echo started >> startup-count\n",
            demo=demo,
            integration_dir=directory,
        )
        self.addCleanup(process.stop)
        process.expect(PROMPT)
        # Fallback is expected; do not wait for a panel to finish a command.
        process.demo = False
        return process, home

    def native_result(self, process, home):
        """Verify bytes and consumers independently of internal capability flags."""
        result = home / "native-result"
        process.fixture("input", result)
        payload = b"preserved" + F2 + b"\x03\x04END"
        process.send(payload)
        process.expect_file(result, payload)
        offset = process.fixture("output")
        process.expect(b"STREAM_RAW_DONE", offset)
        self.assertIn(RAW_OUTPUT, bytes(process.output[offset:]))
        self.assertNotIn(BAR, process.output)
        self.assertNotIn(b"Plugin popup", process.output)
        self.assertEqual((home / "startup-count").read_text(), "started\n")

    def test_plain_does_not_require_integration_files(self):
        """A missing integration path changes neither startup nor native input."""
        for shell in ENTRY:
            with self.subTest(shell=shell):
                process, home = self.start(shell, self.root / "missing", demo=False)
                self.native_result(process, home)
                self.assertNotIn(b"starting without UI", process.output)

    def test_missing_demo_scripts_fall_back_before_user_startup(self):
        """The native shell starts once and receives its own function keys."""
        for shell in ENTRY:
            with self.subTest(shell=shell):
                process, home = self.start(shell, self.root / "missing", demo=True)
                self.native_result(process, home)
                self.assertIn(b"starting without UI", process.output)

    def test_installation_without_scripts_runs_plain(self):
        """The default entry point works when script resources are physically absent."""
        installation = self.root / "installation"
        shutil.copytree(
            ROOT / "src/ish",
            installation / "src/ish",
            ignore=shutil.ignore_patterns("integrations", "__pycache__"),
        )
        with patch("test_stream_cli.ROOT", installation):
            for shell in ENTRY:
                with self.subTest(shell=shell):
                    process, home = self.start(shell, None, demo=False)
                    self.native_result(process, home)
                    self.assertNotIn(b"starting without UI", process.output)

    def test_incompatible_scripts_do_not_execute(self):
        """Reject a new contract before any script can change native shell state."""
        for shell, filename in ENTRY.items():
            with self.subTest(shell=shell):
                scripts = self.root / (shell + "-scripts")
                shutil.copytree(SCRIPTS, scripts)
                (scripts / filename).write_text(
                    "# ish-stream-api: 999\necho executed > unexpected-script\n"
                )
                process, home = self.start(shell, scripts, demo=True)
                self.native_result(process, home)
                self.assertFalse((home / "unexpected-script").exists())
                self.assertIn(b"Unsupported shell integration contract", process.output)

    def test_shell_without_an_adapter_runs_natively(self):
        """An unregistered executable name does not prevent starting the shell."""
        binary = shutil.which(shell_executable("bash"))
        if binary is None:
            self.skipTest("Bash is required")
        alias = self.root / "shell-without-adapter"
        alias.symlink_to(binary)
        with patch.dict(os.environ, {"ISH_TEST_BASH": str(alias)}):
            process, home = self.start("bash", None, demo=True)
            self.native_result(process, home)
            self.assertIn(b"No prompt integration is installed", process.output)

    def test_unknown_placeholders_fall_back_before_execution(self):
        """A mismatched script cannot launch with a partially substituted command."""
        for shell, filename in ENTRY.items():
            with self.subTest(shell=shell):
                scripts = self.root / (shell + "-scripts")
                shutil.copytree(SCRIPTS, scripts)
                (scripts / filename).write_text(
                    "# ish-stream-api: 1\necho @UNKNOWN@ > unexpected-script\n"
                )
                process, home = self.start(shell, scripts, demo=True)
                self.native_result(process, home)
                self.assertFalse((home / "unexpected-script").exists())
                self.assertIn(b"Unknown integration placeholder", process.output)

    def test_inserted_paths_are_not_interpreted_as_more_placeholders(self):
        """A literal user path remains literal even when it resembles another token."""
        (self.root / "probe.sh").write_text("# ish-stream-api: 1\nsource @PATH@\n")
        self.assertEqual(
            render("probe.sh", {"@PATH@": "'/home/@HOME@/test'"}, self.root),
            "source '/home/@HOME@/test'\n",
        )


if __name__ == "__main__":
    unittest.main()
