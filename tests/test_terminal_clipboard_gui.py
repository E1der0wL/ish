"""Verify OS clipboard bytes in a private Kitty/Xvfb display, never the user's."""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_terminal_gui as terminal_gui

EVIDENCE, KITTEN, KITTY, ROOT = (
    terminal_gui.EVIDENCE,
    terminal_gui.KITTEN,
    terminal_gui.KITTY,
    terminal_gui.ROOT,
)


@unittest.skipUnless(
    os.environ.get("ISH_TEST_GUI") == "1",
    "Set ISH_TEST_GUI=1 for isolated OS clipboard verification",
)
class ClipboardGuiTests(unittest.TestCase):
    """Reuse the established private display fixture without inheriting its tests."""

    evidence_name = "clipboard-result.json"
    stop = staticmethod(terminal_gui.TerminalGuiTests.stop)
    remote = terminal_gui.TerminalGuiTests.remote
    wait = terminal_gui.TerminalGuiTests.wait
    send = terminal_gui.TerminalGuiTests.send
    state = terminal_gui.TerminalGuiTests.state
    screenshot = terminal_gui.TerminalGuiTests.screenshot

    def setUp(self):
        """Grant clipboard access only within a newly created authenticated Xvfb."""
        original = subprocess.Popen

        def launch(command, *args, **kwargs):
            if command[0] == str(KITTY):
                command = list(command)
                index = command.index(sys.executable)
                command[index:index] = [
                    "-o",
                    "clipboard_control=write-clipboard write-primary read-clipboard read-primary",
                ]
                command += ["--clipboard", "read-write"]
                if name == "test_exit_drains_fire_and_forget_write_before_parent_input":
                    index = command.index(sys.executable)
                    command[index:index] = [
                        sys.executable,
                        str(ROOT / "tests/terminal_clipboard_parent.py"),
                        str(self.directory / "parent.json"),
                    ]
            return original(command, *args, **kwargs)

        name = self._testMethodName
        self.evidence_name = (
            "clipboard-exit.json"
            if name == "test_exit_drains_fire_and_forget_write_before_parent_input"
            else "clipboard-result.json"
        )

        def start_fixture():
            self._testMethodName = "test_base_has_no_plugin_ui_or_reserved_keys"
            terminal_gui.TerminalGuiTests.setUp(self)

        try:
            with patch("test_terminal_gui.subprocess.Popen", launch):
                start_fixture()
        finally:
            self._testMethodName = name

    def client(self, action):
        """Run a literal protocol client through the shell's actual controlling PTY."""
        helper = ROOT / "tests/terminal_clipboard_client.py"
        command = " ".join(
            map(shlex.quote, (sys.executable, str(helper), str(self.directory), action))
        )
        self.send(command + "\n")
        result = self.directory / (action + ".json")
        self.wait(result.exists, "clipboard client " + action)
        state = json.loads(result.read_text())
        self.assertTrue(state["ok"], state)
        return state

    def clipboard(self, mime):
        """Ask the isolated X11 server for the final clipboard representation."""
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
            env=self.env,
            capture_output=True,
            timeout=8,
        )
        if result.returncode:
            return None
        return result.stdout

    def test_text_binary_mime_and_actual_kitten_cli_roundtrip(self):
        """Check actual OS data, correlated replies, and the real kitten client."""
        from PIL import Image

        png = self.directory / "source.png"
        Image.frombytes(
            "RGB",
            (200, 200),
            bytes((index * 73 + index // 7) % 256 for index in range(120000)),
        ).save(png, compress_level=0)
        text = self.directory / "source.txt"
        text.write_text("한글 클립보드\n" * 4000)
        self.assertGreater(png.stat().st_size, 65536)
        self.client("legacy")
        self.wait(
            lambda: self.clipboard("UTF8_STRING") == "OSC52 한글".encode(),
            "OSC52 system clipboard",
        )
        self.client("iterm")
        self.wait(
            lambda: self.clipboard("UTF8_STRING") == "iTerm 복사".encode(),
            "iTerm system clipboard",
        )
        self.client("mime")
        self.assertEqual(self.clipboard("image/png"), png.read_bytes())
        self.assertEqual(self.clipboard("text/plain"), text.read_bytes())
        copied_png, copied_text = (
            self.directory / "kitten.png",
            self.directory / "kitten.txt",
        )
        command = " ".join(
            map(
                shlex.quote,
                (
                    str(KITTEN),
                    "clipboard",
                    "--get-clipboard",
                    str(copied_png),
                    str(copied_text),
                ),
            )
        )
        self.send(command + " >& kitten-read.log; echo $status > kitten-read.status\n")
        read_status = self.directory / "kitten-read.status"
        self.wait(read_status.exists, "kitten clipboard read completion")
        read_evidence = {
            "exit": read_status.read_text().strip(),
            "log": (self.directory / "kitten-read.log").read_text(),
        }
        self.assertEqual(read_evidence["exit"], "0", read_evidence)
        self.assertEqual(copied_png.read_bytes(), png.read_bytes())
        self.assertEqual(copied_text.read_bytes(), text.read_bytes())
        self.client("read")
        self.assertEqual((self.directory / "read.png").read_bytes(), png.read_bytes())
        self.assertEqual((self.directory / "read.txt").read_bytes(), text.read_bytes())
        self.client("query")
        command = " ".join(
            map(shlex.quote, (str(KITTEN), "clipboard", str(png), str(text)))
        )
        self.send(
            command + " >& kitten-write.log; echo $status > kitten-write.status\n"
        )
        status = self.directory / "kitten-write.status"
        self.wait(status.exists, "kitten clipboard write completion")
        evidence = {
            "exit": status.read_text().strip(),
            "log": (self.directory / "kitten-write.log").read_text(),
        }
        (self.directory / "kitten-result.json").write_text(json.dumps(evidence))
        self.assertEqual(evidence["exit"], "0", evidence)
        self.assertEqual(self.clipboard("image/png"), png.read_bytes())
        self.assertEqual(self.clipboard("text/plain"), text.read_bytes())
        (EVIDENCE / "clipboard-result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "png_bytes": png.stat().st_size,
                    "text_bytes": text.stat().st_size,
                    "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                    "text_sha256": hashlib.sha256(text.read_bytes()).hexdigest(),
                    "kitten_read": read_evidence,
                    "kitten_write": evidence,
                }
            )
        )

    def test_exit_drains_fire_and_forget_write_before_parent_input(self):
        """An exiting shell leaves its actual clipboard ACK inside ish's reader."""
        helper = ROOT / "tests/terminal_clipboard_client.py"
        command = " ".join(
            map(shlex.quote, (sys.executable, str(helper), str(self.directory), "fire"))
        )
        self.send(command + "; exit\n")
        result = self.directory / "parent.json"
        self.wait(result.exists, "outer parent after clipboard completion")
        state = json.loads(result.read_text())
        self.assertEqual(state["returncode"], 0, state)
        self.assertEqual(bytes.fromhex(state["wire"]), b"\x1b[0n")
        self.assertEqual(self.clipboard("UTF8_STRING"), "fire and forget 한글".encode())
        (EVIDENCE / "clipboard-exit.json").write_text(
            json.dumps({"status": "passed", **state})
        )
