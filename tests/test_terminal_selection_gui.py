"""Verify user selection and system PRIMARY through an actual Kitty/X11 session."""

import ctypes
import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_terminal_gui as terminal_gui

EVIDENCE, KITTY, ROOT = terminal_gui.EVIDENCE, terminal_gui.KITTY, terminal_gui.ROOT
FIXTURE = r"""
import os, pathlib, sys, termios, tty
directory = pathlib.Path(sys.argv[1])
saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    os.write(1, b"\x1b[2J\x1b[3;1Halpha beta\r\nsecond line\r\n\x1b[?2004h")
    (directory / "selection-ready").write_text("ready")
    with (directory / "selection-input").open("ab", buffering=0) as output:
        while data := os.read(0, 4096):
            if data == b"\x04":
                break
            output.write(data)
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


@unittest.skipUnless(os.environ.get("ISH_TEST_GUI") == "1", "Set ISH_TEST_GUI=1")
class SelectionGuiTests(unittest.TestCase):
    """Keep child clipboard policy off while verifying explicit user actions."""

    evidence_name = "selection-result.json"
    stop = staticmethod(terminal_gui.TerminalGuiTests.stop)
    remote = terminal_gui.TerminalGuiTests.remote
    wait = terminal_gui.TerminalGuiTests.wait
    send = terminal_gui.TerminalGuiTests.send
    state = terminal_gui.TerminalGuiTests.state
    screenshot = terminal_gui.TerminalGuiTests.screenshot

    def setUp(self):
        """Start a private authorized X11 clipboard without touching the user's."""
        original = subprocess.Popen

        def launch(command, *args, **kwargs):
            if command[0] == str(KITTY):
                command = list(command)
                index = command.index(sys.executable)
                command[index:index] = [
                    "-o",
                    "clipboard_control=write-clipboard write-primary read-clipboard read-primary",
                ]
                command += ["--clipboard", "off", "--mouse", "on"]
            return original(command, *args, **kwargs)

        name = self._testMethodName
        try:
            self._testMethodName = "test_base_has_no_plugin_ui_or_reserved_keys"
            with patch("test_terminal_gui.subprocess.Popen", launch):
                terminal_gui.TerminalGuiTests.setUp(self)
        finally:
            self._testMethodName = name
        self.wait(lambda: self.state()["host"]["clipboard_protocol"], "5522 support")
        fixture = self.directory / "selection_fixture.py"
        fixture.write_text(FIXTURE)
        self.send(
            shlex.join((sys.executable, str(fixture), str(self.directory))) + "\n"
        )
        self.wait((self.directory / "selection-ready").exists, "raw input fixture")
        self.wait(
            lambda: self.state()["lines"][2].startswith("alpha beta"), "native text"
        )

    def clipboard(self, selection):
        """Read the OS selection independently of ish's clipboard implementation."""
        result = subprocess.run(
            ["xclip", "-selection", selection, "-target", "UTF8_STRING", "-out"],
            env=self.env,
            capture_output=True,
            timeout=8,
        )
        return result.stdout if result.returncode == 0 else None

    def set_primary(self, text):
        """Give system PRIMARY to an external owner, distinct from ish's local copy."""
        process = subprocess.Popen(
            [
                "xclip",
                "-selection",
                "primary",
                "-target",
                "UTF8_STRING",
                "-in",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
            start_new_session=True,
        )
        self.addCleanup(self.stop, process)
        process.stdin.write(text.encode())
        process.stdin.close()
        self.wait(
            lambda: self.clipboard("primary") == text.encode(), "external PRIMARY"
        )

    def click(self, button, x, y, count=1):
        """Use real XTEST mouse events in native pixel geometry."""
        x11, xtest = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xtest.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtest.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        with patch.dict(os.environ, {"XAUTHORITY": self.env["XAUTHORITY"]}):
            display = x11.XOpenDisplay(self.env["DISPLAY"].encode())
        self.assertTrue(display)
        try:
            profile = self.state()["host"]
            cw, ch = profile["cell_width"], profile["cell_height"]
            self.assertGreater(cw * ch, 0)
            self.assertTrue(
                xtest.XTestFakeMotionEvent(
                    display, -1, x * cw + cw // 2, y * ch + ch // 2, 0
                )
            )
            for index in range(count):
                self.assertTrue(xtest.XTestFakeButtonEvent(display, button, 1, 0))
                self.assertTrue(xtest.XTestFakeButtonEvent(display, button, 0, 0))
                x11.XFlush(display)
                if index + 1 < count:
                    time.sleep(0.05)  # Explicit gesture timing, never readiness.
        finally:
            x11.XCloseDisplay(display)

    def test_selection_copy_and_external_primary_middle_paste(self):
        """Double-click copy and middle-click use OS data with child access disabled."""
        self.click(1, 2, 2, count=2)
        self.wait(lambda: self.clipboard("primary") == b"alpha", "double-click PRIMARY")
        self.assertEqual(self.clipboard("clipboard"), b"alpha")
        self.screenshot("selection-double-word.png")
        external = "External PRIMARY 한글\nsecond"
        self.set_primary(external)
        self.click(2, 3, 5)
        received = self.directory / "selection-input"
        expected = b"\x1b[200~" + external.encode() + b"\x1b[201~"
        self.wait(lambda: received.read_bytes() == expected, "native PRIMARY paste")
        self.assertEqual(self.clipboard("clipboard"), b"alpha")
        # Native stdin can receive the completed read before PTK publishes its
        # next diagnostic snapshot. Await that render, not a fixed delay.
        self.wait(
            lambda: not self.state()["clipboard_pending"], "completed paste render"
        )
        self.screenshot("selection-primary-paste.png")
        (EVIDENCE / self.evidence_name).write_text(
            json.dumps(
                {
                    "status": "passed",
                    "copied": "alpha",
                    "primary": external,
                    "native_bytes": len(expected),
                    "child_policy": "off",
                }
            )
        )
