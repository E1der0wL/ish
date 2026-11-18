"""Verify six native shell/UI combinations through real controlling PTYs and PTK."""

import shlex
import shutil
import sys
import tempfile
import termios
import unittest
from pathlib import Path

from terminal_scenarios import SHELLS, HooklessTerminal, Scenario, prepare
from test_terminal_basics import assert_restored
from test_terminal_cli import STARTUP_DEADLINE, Terminal

EXIT_PLUGIN = '''"""Drive a public PTK exceptional-exit boundary after startup."""
PLUGIN_META = {"version": "1.0"}
TERMINAL_API = 1

def setup(api):
    @api.bind("f3")
    def fail(event):
        event.app.exit(exception=RuntimeError("explicit PTK exit failure"))
'''


class HooklessTests(unittest.TestCase):
    """A plugin never requires the native editor to surrender its command buffer."""

    def run_case(self, shell, plugins):
        """Check each shell's final files, rendered UI and actual PTY restoration."""
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ish-hookless-")
        )
        terminal = HooklessTerminal(Path(directory), shell=shell, plugins=plugins)
        self.addCleanup(terminal.close)
        Scenario(terminal, shell, plugins).run()


class AbnormalExitTests(unittest.TestCase):
    """Real CLI exits must restore its actual outer PTY, not just renderer mocks."""

    def run_case(self, shell):
        """Check status and termios after the public PTK exceptional-exit API."""
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="ish-exit-"))
        root = Path(directory)
        arguments = prepare(root, shell, False)
        plugin = root / "plugins" / "exit_failure"
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text(EXIT_PLUGIN, encoding="utf-8")
        arguments += [
            "--plugin-dir",
            str(plugin.parent),
            "--plugin",
            "exit_failure",
        ]
        terminal = Terminal(
            root, shell=shutil.which(shell), extra_args=arguments, configure=False
        )
        self.addCleanup(terminal.close)
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        shell_pid = terminal.state()["child_pid"]
        # Exercise cancellation while an actual foreground program owns raw stdin.
        terminal.submit(f"{shlex.quote(sys.executable)} raw-reader.py")
        terminal.result("raw-ready", b"ready")
        self.assertNotEqual(termios.tcgetattr(terminal.fd), terminal.attrs)
        terminal.send(b"\x1bOR")
        assert_restored(self, terminal, 1, shell_pid)
        self.assertIn(b"explicit PTK exit failure", terminal.output)


def case(shell, plugins):
    """Expose every required combination independently to unittest and CI reports."""

    def test(self):
        self.run_case(shell, plugins)

    test.__doc__ = f"Native {shell} with {'selected UI' if plugins else 'no plugins'}."
    return test


for _shell in SHELLS:
    for _plugins in (False, True):
        _name = "plugins" if _plugins else "plain"
        setattr(HooklessTests, f"test_{_shell}_{_name}", case(_shell, _plugins))


def exit_case(shell):
    """Expose every shell and exceptional-exit boundary independently to CI."""

    def test(self):
        self.run_case(shell)

    test.__doc__ = f"Restore the actual outer PTY after {shell} exceptional exit."
    return test


for _shell in SHELLS:
    setattr(AbnormalExitTests, f"test_{_shell}_exception", exit_case(_shell))


if __name__ == "__main__":
    unittest.main()
