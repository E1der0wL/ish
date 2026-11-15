"""Verify title, cursor, cwd and terminal mode restoration in a real Kitty host."""

import json
import os
import shlex
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_terminal_gui as terminal_gui

ROOT, EVIDENCE, KITTY = terminal_gui.ROOT, terminal_gui.EVIDENCE, terminal_gui.KITTY


@unittest.skipUnless(
    os.environ.get("ISH_TEST_GUI") == "1",
    "Set ISH_TEST_GUI=1 for actual terminal checks",
)
class TerminalBasicsGuiTests(unittest.TestCase):
    """Use the existing isolated host and its official remote inspection API."""

    stop = staticmethod(terminal_gui.TerminalGuiTests.stop)
    remote = terminal_gui.TerminalGuiTests.remote
    wait = terminal_gui.TerminalGuiTests.wait
    send = terminal_gui.TerminalGuiTests.send
    state = terminal_gui.TerminalGuiTests.state
    screenshot = terminal_gui.TerminalGuiTests.screenshot

    def setUp(self):
        """Launch a parent observer around the UI-free ish entry point."""
        original = subprocess.Popen
        name = self._testMethodName
        self.evidence_name = name + ".json"

        def launch(command, *args, **kwargs):
            if command[0] == str(KITTY):
                command = list(command)
                index = command.index(sys.executable)
                command[index:index] = [
                    sys.executable,
                    str(ROOT / "tests/terminal_basics_parent.py"),
                    str(self.directory),
                ]
            return original(command, *args, **kwargs)

        try:
            self._testMethodName = "test_base_has_no_plugin_ui_or_reserved_keys"
            with patch("test_terminal_gui.subprocess.Popen", launch):
                terminal_gui.TerminalGuiTests.setUp(self)
        finally:
            self._testMethodName = name

    def window(self):
        """Read actual GUI title/cwd rather than the ish diagnostic snapshot."""
        return json.loads(self.remote("ls"))[0]["tabs"][0]["windows"][0]

    def exercise(self, signum=None):
        """Change presentation inside a native program, then inspect its parent's host."""
        self.wait(
            lambda: self.state()["host"]["title_stack"], "observed title-stack host"
        )
        command = shlex.join(
            [
                sys.executable,
                str(ROOT / "tests/terminal_basics_client.py"),
                str(self.directory),
            ]
        )
        if signum is None:
            command += "; exit"
        self.send(command + "\n")
        self.wait(
            lambda: (self.directory / "lifecycle-ready").exists(),
            "native presentation program",
        )
        self.wait(
            lambda: self.window()["title"] == "ISH_CHILD_TITLE", "child title applied"
        )
        parent = json.loads((self.directory / "lifecycle-parent.json").read_text())
        if signum is not None:
            os.kill(parent["pid"], signum)
        else:
            self.send("x")
        result_path = self.directory / "lifecycle-result.json"
        self.wait(result_path.exists, "outer state measured after ish exit")
        result = json.loads(result_path.read_text())
        self.assertEqual(
            result["returncode"], 128 + signum if signum is not None else 0
        )
        self.assertTrue(result["termios_restored"])
        self.assertEqual(result["after"], result["before"])
        self.wait(
            lambda: self.window()["title"] == "ISH_PARENT_TITLE",
            "parent title restored",
        )
        self.assertEqual(self.window()["cwd"], parent["cwd"])
        self.screenshot(self._testMethodName + ".png")
        (EVIDENCE / self.evidence_name).write_text(
            json.dumps({"status": "passed", **result})
        )

    def test_normal_exit_restores_presentation(self):
        """Shell exit restores actual host state and returns its status."""
        self.exercise()

    def test_sigterm_restores_presentation(self):
        """SIGTERM follows PTK cleanup while a foreground program waits for stdin."""
        self.exercise(signal.SIGTERM)

    def test_sighup_restores_presentation(self):
        """SIGHUP restores the same state before the native shell is reaped."""
        self.exercise(signal.SIGHUP)
