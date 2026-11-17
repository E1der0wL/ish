"""Require exact native history through live PTK output, popups and resize."""

import sys
import tempfile
import unittest
from pathlib import Path

from terminal_scenarios import HooklessTerminal
from terminal_supplements import exercise, prepare
from test_terminal_cli import Terminal

PLUGIN_HEADER = 'PLUGIN_META = {"version": "1"}\nTERMINAL_API = 1\n'
OUTPUT_PLUGIN = """from ish.terminal.extensions import ProtocolResult
class Codec:
    def handle(self, control):
        return ProtocolResult(native=b"SHARED_OUTPUT_OK")
    def cancel(self): pass
    def close(self): pass
def setup(api):
    api.register_protocol("shared-output", kind="osc", prefixes=(b"7400;",), factory=Codec)
"""
KEY_PLUGIN = """from ish.terminal.host import StructuredKey
def setup(api):
    api.register_keys("shared_key", {"\\x1b[9001~": StructuredKey("up", 57352)})
"""
CLIPBOARD_PLUGIN = """from pathlib import Path
class Provider:
    async def read(self): return "shared clipboard"
    async def write(self, text): raise AssertionError("Unexpected write")
    def close(self): pass
    async def aclose(self): Path("provider-closed").write_text("closed")
def setup(api):
    api.register_clipboard("shared-clipboard", Provider)
"""
READER = r"""import os
import termios
import tty
from pathlib import Path
saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    os.write(1, b'\x1b]7400;test\x07\x1b]52;c;?\x1b\\')
    reply = bytearray()
    while not reply.endswith(b'\x1b\\'):
        reply.extend(os.read(0, 4096))
    Path('clipboard-reply').write_bytes(reply)
    Path('keys-ready').touch()
    keys = bytearray()
    while not keys.endswith(b'|done|'):
        keys.extend(os.read(0, 4096))
    Path('keys-result').write_bytes(keys)
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


class SharedDirectoryTests(unittest.TestCase):
    """Exercise all extension types through the existing persisted selection."""

    def test_one_directory_loads_output_keys_and_clipboard_without_ui(self):
        """The normal rc and PluginManager load all supplements from one source root."""
        with tempfile.TemporaryDirectory(prefix="ish-single-directory-") as directory:
            root = Path(directory)
            home = root / "ish"
            scripts = home / "plugin" / "script"
            for name, source in (
                ("shared_output", OUTPUT_PLUGIN),
                ("shared_key", KEY_PLUGIN),
                ("shared_clipboard", CLIPBOARD_PLUGIN),
            ):
                package = scripts / name
                package.mkdir(parents=True)
                (package / f"{name}.py").write_text(PLUGIN_HEADER + source)
            (home / ".ishrc.py").write_text(
                "config.vt = {'plugins': ['shared_output', 'shared_key', "
                "'shared_clipboard'], 'clipboard': 'read-write'}\n"
            )
            program = root / "reader.py"
            program.write_text(READER)
            terminal = Terminal(
                root,
                shell=sys.executable,
                configure=False,
                extra_args=("--home", str(home), "--shell-arg=" + str(program)),
            )
            try:
                terminal.require(lambda: (root / "keys-ready").exists())
                terminal.result(
                    "clipboard-reply", b"\x1b]52;c;c2hhcmVkIGNsaXBib2FyZA==\x1b\\"
                )
                terminal.require(lambda: terminal.frame_has("SHARED_OUTPUT_OK"))
                state = terminal.state()
                self.assertEqual(state["rows"], terminal.rows)
                self.assertEqual(state["popups"], [])
                self.assertEqual(state["clipboard_provider"], "shared-clipboard")
                self.assertEqual(state["key_mappings"], {"shared_key": 1})
                terminal.send(b"\x1b[9001~|done|")
                terminal.result("keys-result", b"\x1b[A|done|")
                terminal.require(terminal.exited)
                terminal.result("provider-closed", b"closed")
                self.assertEqual(terminal.status, 0)
                self.assertEqual(terminal.state()["clipboard_failures"], 0)
            finally:
                terminal.close()


class SustainedOutputTests(unittest.TestCase):
    """Check final retained content while focus and host geometry change."""

    def run_shell(self, shell):
        """Run the same observable contract for each native shell."""
        with tempfile.TemporaryDirectory(prefix="ish-supplements-") as directory:
            root = Path(directory)
            options = prepare(root)
            terminal = HooklessTerminal(
                root, shell=shell, plugins=True, extra_args=options
            )
            try:
                result = exercise(terminal, shell)
                self.assertEqual(result, {"rows": 1280, "chunks": 8, "popups": 4})
                self.assertEqual(terminal.status, 0)
            finally:
                terminal.close()

    def test_tcsh_output_survives_popup_and_repeated_resize(self):
        """Retain all tcsh foreground output across focus and size changes."""
        self.run_shell("tcsh")

    def test_bash_output_survives_popup_and_repeated_resize(self):
        """Retain all Bash foreground output across focus and size changes."""
        self.run_shell("bash")

    def test_zsh_output_survives_popup_and_repeated_resize(self):
        """Retain all zsh foreground output across focus and size changes."""
        self.run_shell("zsh")
