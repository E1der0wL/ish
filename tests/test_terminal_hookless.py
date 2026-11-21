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

    def test_unbound_keys_follow_foreground_keyboard_mode(self):
        """One host stroke submits in Bash or reaches raw stdin in its requested format."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ish-keys-")))
        terminal = HooklessTerminal(root, shell="bash", plugins=False)
        self.addCleanup(terminal.close)
        terminal.require(lambda: b"\x1b[?u" in terminal.output, STARTUP_DEADLINE)
        terminal.send(b"\x1b[?0u")
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        self.assertEqual(terminal.escape_key, b"\x1b[27u")
        press = b"\x1b[13;2u"
        release = b"\x1b[13;2:3u"
        terminal.send(b"printf ok > submitted" + press + release)
        terminal.result("submitted", b"ok")
        terminal.require(terminal.at_prompt)

        repeat = b"\x1b[13;2:2u"
        pages = b"\x1b[5;2~\x1b[6;2~"
        raw = b"\r\r"
        xterm = b"\x1b[27;2;13~" * 2
        cases = (
            (
                b"",
                pages + b"\x1b[9;2u\x1b[97;4u\x1b[91;5u",
                raw + pages + b"\x1b[Z\x1bA\x1b",
            ),
            (b"\x1b[>4;1m", b"", xterm),
            (b"\x1b[>4;2m", b"", xterm),
            (
                b"\x1b[>11u",
                b"\r\t\x03",
                press + repeat + release + b"\x1b[13u\x1b[9u\x1b[99;5u",
            ),
            (b"\x1b[<u", b"", xterm),
            (b"\x1b[>4;0m", b"", raw),
            (b"\x1b[?1049h", pages, raw + pages),
            (b"\x1b[?1049l", b"", raw),
            (b"\x1b[>4;2m\x1b[!p", b"", raw),
            (b"\x1b[>4;2m\x1b[>4n", b"", raw),
            (b"\x1b[>4;1m\x1bc", b"", raw),
        )
        (root / "capture.py").write_text(
            "import os, tty\nfrom pathlib import Path\ntty.setraw(0)\n"
            f"for i, mode in enumerate({[mode for mode, _, _ in cases]!r}):\n"
            " os.write(1, mode + f'\\r\\nKEY_STAGE_{i}\\r\\n'.encode())\n"
            " data = b''\n"
            " while not data.endswith(b'|done|'): data += os.read(0, 1024)\n"
            " Path(f'key-{i}').write_bytes(data)\n"
        )
        terminal.submit(f"{shlex.quote(sys.executable)} capture.py")
        for i, (mode, extra, expected) in enumerate(cases):
            with self.subTest(mode=mode):
                terminal.require(
                    lambda marker=f"KEY_STAGE_{i}": terminal.frame_has(marker)
                )
                terminal.send(
                    press + repeat + release + extra + b"\x1b[200~|done|\x1b[201~"
                )
                terminal.result(f"key-{i}", expected + b"|done|")
        terminal.require(terminal.at_prompt)
        terminal.finish()

    def test_negotiated_escape_closes_popup_and_preserves_continuation(self):
        """The host's keyboard-mode push changes a scripted Esc press on the wire."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ish-esc-")))
        terminal = HooklessTerminal(root, shell="tcsh", plugins=True)
        self.addCleanup(terminal.close)
        self.assertEqual(terminal.escape_key, b"\x1b")
        terminal.require(lambda: b"\x1b[?u" in terminal.output, STARTUP_DEADLINE)
        terminal.send(b"\x1b[?0u")
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        self.assertEqual(terminal.escape_key, b"\x1b[27u")
        terminal.submit("printf '%s\\n' one \\")
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        scenario = Scenario(terminal, "tcsh", True)
        scenario.open_popup()
        scenario.cancel_popup(tail=b"two > continued\r")
        terminal.result("continued", b"one\ntwo\n")
        terminal.finish()
        terminal.receive()
        self.assertEqual(terminal.escape_key, b"\x1b")

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
