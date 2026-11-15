"""Observe colors, overline, cursor and theme changes in private real Kitty/Xvfb."""

import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_terminal_gui as terminal_gui

ROOT, KITTY, EVIDENCE = terminal_gui.ROOT, terminal_gui.KITTY, terminal_gui.EVIDENCE
FOREGROUND = (197, 214, 231)
BACKGROUND = (16, 32, 48)
CURSOR = (18, 52, 86)
ANSI_RED = (209, 35, 69)
OVERRIDE_CURSOR = (70, 180, 210)


@unittest.skipUnless(os.environ.get("ISH_TEST_GUI") == "1", "Set ISH_TEST_GUI=1")
class AppearanceGuiTests(unittest.TestCase):
    """Reuse only fixture helpers, keeping the basic UI free of test plugins."""

    evidence_name = "appearance-result.json"
    stop = staticmethod(terminal_gui.TerminalGuiTests.stop)
    remote = terminal_gui.TerminalGuiTests.remote
    wait = terminal_gui.TerminalGuiTests.wait
    send = terminal_gui.TerminalGuiTests.send
    state = terminal_gui.TerminalGuiTests.state
    screenshot = terminal_gui.TerminalGuiTests.screenshot
    count = staticmethod(terminal_gui.TerminalGuiTests.count)

    def setUp(self):
        """Set nonstandard host colors in an isolated display with no user config."""
        original = subprocess.Popen

        def launch(command, *args, **kwargs):
            if command[0] == str(KITTY):
                command = list(command)
                index = command.index(sys.executable)
                command[index:index] = [
                    "-o",
                    "foreground=#c5d6e7",
                    "-o",
                    "background=#102030",
                    "-o",
                    "cursor=#123456",
                    "-o",
                    "color1=#d12345",
                    "-o",
                    "cursor_blink_interval=0",
                ]
            return original(command, *args, **kwargs)

        name = self._testMethodName
        try:
            self._testMethodName = "test_base_has_no_plugin_ui_or_reserved_keys"
            with patch("test_terminal_gui.subprocess.Popen", launch):
                terminal_gui.TerminalGuiTests.setUp(self)
        finally:
            self._testMethodName = name

    def result(self, name):
        """Read a successful client result or surface its literal protocol failure."""
        error = self.directory / "appearance-error.json"
        if error.exists():
            self.fail(error.read_text())
        path = self.directory / name
        return json.loads(path.read_text()) if path.exists() else None

    def host_colors(self):
        """Read real outer colors through Kitty's independent remote API."""
        return dict(line.split() for line in self.remote("get-colors").splitlines())

    def test_real_colors_pixels_cursor_reset_and_theme_notification(self):
        """Check user-visible pixels plus independent child replies and host state."""
        command = shlex.join(
            [
                sys.executable,
                str(ROOT / "tests/terminal_appearance_client.py"),
                str(self.directory),
            ]
        )
        self.send(command + "\n")
        query = self.wait(
            lambda: self.result("appearance-query.json"), "virtual color replies"
        )
        self.assertEqual(
            query["colors"],
            {
                "10": list(FOREGROUND),
                "11": list(BACKGROUND),
                "12": list(CURSOR),
                "4;1": list(ANSI_RED),
            },
        )
        self.wait(lambda: self.result("appearance-ready.json"), "appearance scene")
        self.wait(
            lambda: self.host_colors().get("cursor") == "#46b4d2",
            "actual cursor override",
        )
        state = self.state()
        self.assertEqual(state["popups"], [])
        width, height = state["host"]["cell_width"], state["host"]["cell_height"]
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        image = self.screenshot("appearance-initial.png")
        self.assertEqual(self.count(image, ANSI_RED), width * height * 4)
        self.assertGreaterEqual(self.count(image, OVERRIDE_CURSOR), width * height)
        plain = image.crop((0, height, width * 4, height * 2))
        overline = image.crop((0, height * 2, width * 4, height * 3))
        self.assertGreater(
            self.count(overline, FOREGROUND), self.count(plain, FOREGROUND)
        )
        self.send("r")
        self.wait(lambda: self.result("appearance-reset.json"), "child cursor reset")
        self.wait(
            lambda: self.host_colors().get("cursor") == "#123456",
            "restored host cursor",
        )
        self.send("o")
        self.wait(
            lambda: self.result("appearance-override.json"), "explicit child foreground"
        )
        self.remote("set-colors", "background=#f0e0d0", "foreground=#121314")
        themed = self.wait(
            lambda: self.result("appearance-theme.json"), "real host theme notification"
        )
        self.assertIn("1b5b3f3939373b326e", themed["notification"])
        self.assertEqual(themed["colors"], {"10": [171, 205, 239]})
        self.wait(
            lambda: self.state()["host"]["appearance"]["scheme"] == "light",
            "published host theme snapshot",
        )
        self.assertIsNone(self.state()["host"]["appearance"]["foreground"])
        self.assertEqual(
            self.state()["host"]["initial_appearance"]["foreground"], list(FOREGROUND)
        )
        self.screenshot("appearance-theme.png")
        self.send("x")
        (EVIDENCE / self.evidence_name).write_text(
            json.dumps(
                {
                    "status": "passed",
                    "query": query,
                    "theme": themed,
                    "cell": [width, height],
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
