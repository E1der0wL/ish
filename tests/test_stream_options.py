"""Check native startup options through the public CLI and a controlling PTY."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from stream_fixture import shell_executable
from test_stream_cli import BAR, PROMPT, StreamProcess

SHELLS = ("tcsh", "bash", "zsh")
POPUP = b"Plugin popup"
LOGIN_FILES = {
    "tcsh": (
        ".login",
        "if ( $?loginsh ) echo login > login-mode\n",
    ),
    "bash": (
        ".bash_profile",
        "if shopt -q login_shell; then printf 'login\\n' > login-mode; fi\n"
        '. "$HOME/.bashrc"\n',
    ),
    "zsh": (
        ".zprofile",
        "if [[ -o login ]]; then printf 'login\\n' > login-mode; fi\n",
    ),
}
STARTUP = "printf 'start\\n' >> startup-count\n"


@unittest.skipUnless(os.name == "posix", "requires native POSIX PTYs")
class OptionsTests(unittest.TestCase):
    """Assert shell-visible files and exit status rather than parser internals."""

    def start(self, shell, *, ui=False, extra_args=(), login=False):
        """Keep native rc, login files and plugin copies inside one temporary home."""
        if not shutil.which(shell_executable(shell)):
            self.skipTest(f"requires native {shell}")
        temporary = tempfile.TemporaryDirectory(prefix=f"ish-options-{shell}-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        if login:
            filename, source = LOGIN_FILES[shell]
            (directory / filename).write_text(source, encoding="utf-8")
        process = StreamProcess(
            directory,
            shell=shell,
            demo=ui,
            startup=STARTUP,
            extra_args=("--lang", "en", *extra_args),
        )
        self.addCleanup(process.stop)
        return directory, process

    def assert_native(self, process):
        """Native startup must not reserve screen rows or show sample UI."""
        self.assertNotIn(BAR, process.output)
        self.assertNotIn(POPUP, process.output)
        self.assertNotIn(b"\x1b[1;19r", process.output)

    def finish(self, process, status=0):
        """Wait for the actual CLI exit, then drain its restored-terminal output."""
        process.wait(lambda: process.poll() is not None, "native shell exit")
        while process.read(0):
            pass
        self.assertEqual(process.status, status, process.diagnostic("exit status"))

    def test_login_keeps_native_rc_and_home_with_or_without_ui(self):
        """All three login shells keep their startup semantics when UI is unavailable."""
        for shell in SHELLS:
            for ui in (False, True):
                with self.subTest(shell=shell, ui=ui):
                    directory, process = self.start(
                        shell, ui=ui, login=True, extra_args=("--login",)
                    )
                    process.expect(PROMPT)
                    process.expect_file(directory / "login-mode", "login\n")
                    process.expect_file(directory / "startup-count", "start\n")
                    process.send(b"printf '%s\\n' \"$HOME\" > native-home\r")
                    process.expect_file(directory / "native-home", f"{directory}\n")
                    process.send(b"exit 17\r")
                    self.finish(process, 17)
                    self.assert_native(process)
                    if ui:
                        self.assertIn(b"non-login interactive", process.output)
                    self.assertEqual(
                        (directory / "startup-count").read_text(), "start\n"
                    )

    def test_custom_arguments_fall_back_without_rewriting_execution(self):
        """Selected UI providers must preserve -c execution and its native exit code."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                directory, process = self.start(
                    shell,
                    ui=True,
                    extra_args=(
                        "--shell-arg=-c",
                        "--shell-arg=printf 'custom\\n' > custom-result; exit 23",
                    ),
                )
                process.expect_file(directory / "custom-result", "custom\n")
                self.finish(process, 23)
                self.assert_native(process)
                self.assertIn(b"non-login interactive", process.output)
                # tcsh reads its rc even for -c; Bash/zsh do not read an interactive rc.
                count = directory / "startup-count"
                if shell == "tcsh":
                    self.assertEqual(count.read_text(), "start\n")
                else:
                    self.assertFalse(count.exists())

    def test_settings_home_keeps_native_home_and_plain_terminal(self):
        """An ish settings override does not redirect the shell's home or add any UI."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                settings = tempfile.TemporaryDirectory(prefix="ish-settings-parent-")
                self.addCleanup(settings.cleanup)
                home = Path(settings.name) / "unused-settings"
                directory, process = self.start(shell, extra_args=("--home", str(home)))
                process.expect(PROMPT)
                process.send(b"printf '%s\\n' \"$HOME\" > native-home\r")
                process.expect_file(directory / "native-home", f"{directory}\n")
                process.send(b"exit\r")
                self.finish(process)
                self.assert_native(process)
                self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
