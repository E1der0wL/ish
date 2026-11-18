"""Verify read-only shared diagnostics."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n
from ish.runtime.diagnostics import (
    display_diagnostics,
    environment_fields,
)


class DiagnosticsTests(unittest.TestCase):
    """Use empty configuration homes to observe any unintended startup effects."""

    def setUp(self):
        """Keep test configuration separate from the user's installed settings."""
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-diagnostics-test-")
        self.previous_home = config._ish_home
        config.ISH_HOME = Path(self.temporary.name) / "uncreated-home"

    def tearDown(self):
        """Restore the shared configuration and remove only test-owned files."""
        config._ish_home = self.previous_home
        self.temporary.cleanup()

    def test_common_fields_describe_runtime_without_creating_home(self):
        """Shared facts retain selected paths and do not prepare session files."""
        with contextlib.redirect_stdout(io.StringIO()):
            values = environment_fields(sys.executable)
        self.assertEqual(values[Message.CLI_DIAGNOSE_VERSION], __version__)
        self.assertEqual(values[Message.CLI_DIAGNOSE_EXECUTABLE], sys.executable)
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_SHELL_PATH], os.path.abspath(sys.executable)
        )
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_SHELL_REALPATH],
            os.path.realpath(sys.executable),
        )
        self.assertEqual(values[Message.CLI_DIAGNOSE_HOME], config.ISH_HOME)
        self.assertEqual(values[Message.CLI_DIAGNOSE_LANGUAGE], i18n.current_lang)
        self.assertFalse(config.ISH_HOME.exists())
        for field in (
            Message.CLI_DIAGNOSE_RC,
            Message.CLI_DIAGNOSE_PLUGINS,
        ):
            self.assertNotIn(field, values)

    def test_missing_shell_and_non_tty_are_reported_without_execution(self):
        """An unavailable shell remains diagnostic data rather than startup failure."""
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO()),
            contextlib.redirect_stdout(output),
            patch("subprocess.Popen", side_effect=AssertionError("spawned a process")),
        ):
            values = environment_fields("ish-test-shell-that-does-not-exist")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_SHELL_PATH],
            i18n.get(Message.CLI_DIAGNOSE_MISSING),
        )
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_TERMINAL_SIZE],
            i18n.get(Message.CLI_DIAGNOSE_UNAVAILABLE),
        )
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_STDIN_TTY],
            i18n.get(Message.CLI_DIAGNOSE_NO),
        )
        self.assertEqual(
            values[Message.CLI_DIAGNOSE_STDOUT_TTY],
            i18n.get(Message.CLI_DIAGNOSE_NO),
        )
        self.assertFalse(config.ISH_HOME.exists())

    def test_common_import_stays_read_only(self):
        """A fresh diagnostic process neither loads shell integrations nor spawns code."""
        code = """
import sys
from unittest.mock import patch
with patch('subprocess.Popen', side_effect=AssertionError('process started')):
    from ish.config import config
    config.ISH_HOME = sys.argv[1]
    from ish.runtime.diagnostics import environment_fields
    environment_fields('ish-test-missing-shell')
assert not config.ISH_HOME.exists()
print('read-only common diagnostics')
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(config.ISH_HOME)],
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "read-only common diagnostics\n")
        self.assertEqual(result.stderr, "")

    def test_display_uses_requested_translations_and_field_order(self):
        """The common renderer accepts caller-specific scope without changing facts."""
        translations = {
            Message.VT_DIAGNOSE_TITLE: "환경 진단",
            Message.VT_DIAGNOSE_SCOPE: "읽기 전용",
            Message.CLI_DIAGNOSE_SHELL: "요청 셸",
            Message.CLI_DIAGNOSE_VERSION: "버전",
        }
        output = io.StringIO()
        with (
            patch.object(i18n, "get", side_effect=translations.__getitem__),
            contextlib.redirect_stdout(output),
        ):
            display_diagnostics(
                Message.VT_DIAGNOSE_TITLE,
                Message.VT_DIAGNOSE_SCOPE,
                {
                    Message.CLI_DIAGNOSE_SHELL: "tcsh",
                    Message.CLI_DIAGNOSE_VERSION: "test-version",
                },
            )
        self.assertEqual(
            output.getvalue(),
            "환경 진단\n읽기 전용\n요청 셸: tcsh\n버전: test-version\n",
        )


if __name__ == "__main__":
    unittest.main()
