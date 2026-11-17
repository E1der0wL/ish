"""Opt-in text/xterm and iTerm/WezTerm verification using actual X11 pixels."""

import ctypes
import json
import os
import secrets
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_plugin import plugin_options

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/terminal-test"
EVIDENCE = CACHE / "evidence"
XTERM = CACHE / "deps/usr/bin/xterm"
WEZTERM = CACHE / "wezterm/wezterm/usr/bin/wezterm"
XDOTOOL = CACHE / "deps/usr/bin/xdotool"
DEADLINE = 30


@unittest.skipUnless(
    os.environ.get("ISH_TEST_GUI") == "1",
    "Set ISH_TEST_GUI=1 for actual X11 renderer tests",
)
class LegacyGuiTests(unittest.TestCase):
    """Check image consumers, opaque PTK popups and restoration on verified hosts."""

    def setUp(self):
        """Start a private Xvfb and cached terminal without changing user settings."""
        self.host = "xterm" if self._testMethodName.endswith("xterm") else "wezterm"
        self.protocol = None if self.host == "xterm" else "iterm"
        executable = XTERM if self.host == "xterm" else WEZTERM
        self.assertTrue(executable.is_file(), str(executable))
        self.assertTrue(XDOTOOL.is_file(), str(XDOTOOL))
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / f"{self.host}-result.json").write_text(
            json.dumps({"status": "running"})
        )
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-legacy-gui-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dump = self.directory / "state.json"
        self.log_path = EVIDENCE / f"{self.host}-ish.log"
        self.log = self.log_path.open("w")
        self.addCleanup(self.log.close)
        authority = self.directory / "xauthority"
        cookie = secrets.token_hex(16)
        self.auth(authority, "0", cookie)
        display = subprocess.Popen(
            [
                "Xvfb",
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1200x900x24",
                "-nolisten",
                "unix",
                "-listen",
                "tcp",
                "-auth",
                str(authority),
            ],
            stdout=subprocess.PIPE,
            stderr=self.log,
            start_new_session=True,
        )
        self.addCleanup(self.stop, display)
        self.assertTrue(select.select([display.stdout], [], [], DEADLINE)[0])
        display_id = display.stdout.readline().decode().strip()
        self.assertTrue(display_id.isdigit())
        self.auth(authority, display_id, cookie)
        self.env = {
            **os.environ,
            "DISPLAY": "localhost:" + display_id,
            "XAUTHORITY": str(authority),
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": str(CACHE / "deps/usr/lib/x86_64-linux-gnu"),
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
            "ISH_GUI_DIR": str(self.directory),
            "XDG_RUNTIME_DIR": str(self.directory),
            "XDG_DATA_HOME": str(self.directory / "data"),
            "XDG_CACHE_HOME": str(self.directory / "cache"),
        }
        environment = patch.dict(
            os.environ, {"DISPLAY": self.env["DISPLAY"], "XAUTHORITY": str(authority)}
        )
        environment.start()
        self.addCleanup(environment.stop)
        child = [
            sys.executable,
            "-m",
            "ish.terminal",
            "--shell",
            "tcsh",
            "--cwd",
            str(self.directory),
            "--dump-state",
            str(self.dump),
            *plugin_options(self.directory),
        ]
        if self.host == "xterm":
            command = [
                str(XTERM),
                "-fa",
                "DejaVu Sans Mono",
                "-fs",
                "12",
                "-geometry",
                "80x24",
                "-bg",
                "#102030",
                "-fg",
                "#eeeeee",
                "-xrm",
                "*allowWindowOps: true",
                "-xrm",
                "*color1: #2468ac",
                "-e",
                *child,
            ]
        else:
            config = self.directory / "wezterm.lua"
            self.wez_config = config
            config.write_text(
                "return {enable_tab_bar=false,enable_wayland=false,front_end='OpenGL',font_size=12,initial_rows=24,initial_cols=80,colors={background='#102030',foreground='#eeeeee'},window_padding={left=0,right=0,top=0,bottom=0},check_for_updates=false}"
            )
            command = [
                str(WEZTERM),
                "--config-file",
                str(config),
                "start",
                "--always-new-process",
                *child,
            ]
        self.process = subprocess.Popen(
            command,
            env=self.env,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )
        self.addCleanup(self.stop, self.process)
        self.window = self.wait(
            lambda: (
                self.xdo(
                    "search",
                    "--onlyvisible",
                    "--class",
                    "XTerm" if self.host == "xterm" else "org.wezfurlong.wezterm",
                )
                .strip()
                .splitlines()[0]
            ),
            "mapped terminal",
        )
        self.xdo("windowfocus", "--sync", self.window)
        self.wait(lambda: self.dump.exists(), "ish startup")
        self.wait(
            lambda: (
                self.state()["host"]["image_protocol"] == self.protocol
                and self.state()["host"]["appearance"]["background"] == [16, 32, 48]
            ),
            "verified image output profile",
        )
        if self.host == "wezterm":
            sockets = list(self.directory.rglob("gui-sock-*"))
            self.assertEqual(
                len(sockets), 1, f"Expected one private WezTerm GUI socket: {sockets}"
            )
            self.env["WEZTERM_UNIX_SOCKET"] = str(sockets[0])
            panes = json.loads(self.wez("list", "--format", "json"))
            self.assertEqual(len(panes), 1)
            self.pane = str(panes[0]["pane_id"])

    @staticmethod
    def auth(authority, display_id, cookie):
        """Register an isolated X11 cookie, with no global xhost changes."""
        subprocess.run(
            [
                "xauth",
                "-f",
                str(authority),
                "add",
                "localhost:" + display_id,
                "MIT-MAGIC-COOKIE-1",
                cookie,
            ],
            check=True,
            capture_output=True,
        )

    @staticmethod
    def stop(process):
        """Reap only process groups created by the test."""
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def xdo(self, *arguments):
        """Inject real X11 events rather than bypassing the outer terminal."""
        result = subprocess.run(
            [str(XDOTOOL), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def send(self, text):
        """Type ordinary text and Enter through the terminal's keyboard path."""
        if self.host == "wezterm":
            self.wez("send-text", "--pane-id", self.pane, "--no-paste", text)
            return
        for index, line in enumerate(text.split("\n")):
            if index:
                self.key("Return")
            if line:
                self.xdo("type", "--clearmodifiers", "--delay", "0", "--", line)

    def key(self, name):
        """Send one real keypress to the currently focused terminal window."""
        if self.host == "wezterm":
            self.send(
                {"F2": "\x1bOQ", "F3": "\x1bOR", "Escape": "\x1b", "Return": "\r"}[name]
            )
            return
        # xdotool 3.20160805 chooses an Alt-bearing symbol level for F2/F3 in
        # this Xvfb map. XTEST with the unmodified physical keycode produces
        # the independently observed SS3 key in a raw xterm child.
        x11 = ctypes.CDLL("libX11.so.6")
        xtest = ctypes.CDLL("libXtst.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        xtest.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtest.XTestFakeKeyEvent.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(self.env["DISPLAY"].encode())
        self.assertTrue(display)
        try:
            keycode = x11.XKeysymToKeycode(display, x11.XStringToKeysym(name.encode()))
            self.assertTrue(keycode)
            self.assertTrue(xtest.XTestFakeKeyEvent(display, keycode, 1, 0))
            self.assertTrue(xtest.XTestFakeKeyEvent(display, keycode, 0, 0))
            x11.XFlush(display)
        finally:
            x11.XCloseDisplay(display)

    def wez(self, *arguments):
        """Use the official CLI against this test's isolated GUI socket."""
        result = subprocess.run(
            [
                str(WEZTERM),
                "--config-file",
                str(self.wez_config),
                "cli",
                "--no-auto-start",
                *arguments,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def state(self):
        """Use the explicit test diagnostic as synchronization, not shell readiness."""
        return json.loads(self.dump.read_text())

    def screenshot(self, name):
        """Capture actual X11-rendered pixels, including native images and PTK UI."""
        from PIL import ImageGrab

        image = ImageGrab.grab(xdisplay=self.env["DISPLAY"]).convert("RGB")
        image.save(EVIDENCE / f"{self.host}-{name}.png")
        return image

    @staticmethod
    def count(image, color):
        """Count independent final pixel colors."""
        return sum(pixel == color for pixel in image.get_flattened_data())

    @staticmethod
    def glyph_pixels(image):
        """Include host white palettes and antialiasing, excluding image/background."""
        return sum(min(pixel) >= 220 for pixel in image.get_flattened_data())

    def wait(self, predicate, description):
        """Timeouts fail tests and never infer a successful terminal state."""
        deadline = time.monotonic() + DEADLINE
        last = ""
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, RuntimeError, IndexError, json.JSONDecodeError) as error:
                last = str(error)
            if hasattr(self, "process") and self.process.poll() is not None:
                break
            time.sleep(0.05)
        self.screenshot("failure")
        if self.dump.exists():
            (EVIDENCE / f"{self.host}-failure-state.json").write_text(
                self.dump.read_text()
            )
        if (self.directory / "native-input").exists():
            (EVIDENCE / f"{self.host}-failure-input.bin").write_bytes(
                (self.directory / "native-input").read_bytes()
            )
        self.log.flush()
        self.fail(f"{description}: {last}\n{self.log_path.read_text()[-3000:]}")

    def exercise(self):
        """Show each native image protocol and check partial popup masking and recovery."""
        self.send("foreach x (alpha beta)\n echo $x >> gui-loop\nend\n")
        loop = self.directory / "gui-loop"
        self.wait(
            lambda: loop.exists() and loop.read_text() == "alpha\nbeta\n",
            "tcsh continuation",
        )
        command = shlex.join(
            [
                sys.executable,
                str(ROOT / "tests/terminal_fixture.py"),
                str(self.directory),
                self.protocol,
            ]
        )
        self.send(command + "\n")
        self.wait(
            lambda: (self.directory / "fixture-ready").exists(), "native image fixture"
        )
        pixels = 96 * 64
        self.wait(
            lambda: self.count(self.screenshot("01-image"), (255, 0, 0)) == pixels,
            "original native image pixels",
        )
        original = self.screenshot("01-image")
        red_positions = [
            (index % original.width, index // original.width)
            for index, color in enumerate(original.get_flattened_data())
            if color == (255, 0, 0)
        ]
        image_x = min(x for x, _ in red_positions)
        image_y = min(y for _, y in red_positions)
        self.key("F2")
        self.wait(lambda: len(self.state()["popups"]) == 1, "PTK popup")
        cw, ch = self.state()["host"]["cell_width"], self.state()["host"]["cell_height"]
        overlap = max(0, min(4 * cw + 96, 20 * cw) - 8 * cw) * max(
            0, min(4 * ch + 64, 9 * ch) - 5 * ch
        )
        self.wait(
            lambda: (
                self.count(self.screenshot("02-popup"), (255, 0, 0)) == pixels - overlap
            ),
            "partial popup masking",
        )
        glyph_crop = (
            image_x + 4 * cw,
            image_y + ch,
            min(image_x + 96, image_x + 16 * cw),
            min(image_y + 64, image_y + 5 * ch),
        )
        self.assertGreater(
            self.glyph_pixels(self.screenshot("02-popup").crop(glyph_crop)),
            20,
            "Popup glyphs in the image overlap must remain visible",
        )
        self.send("POPUP_ONLY")
        self.wait(
            lambda: (
                (self.directory / "popup-input").exists()
                and "POPUP_ONLY" in (self.directory / "popup-input").read_text()
            ),
            "popup input",
        )
        self.assertFalse((self.directory / "native-input").exists())
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 1, "color": "blue"})
        )
        self.wait(
            lambda: (self.directory / "fixture-ack").exists(),
            "native image replacement",
        )
        self.wait(
            lambda: (
                self.count(self.screenshot("03-live-change"), (0, 0, 255))
                == pixels - overlap
            ),
            "latest image behind popup",
        )
        glyphs = self.screenshot("03-live-change").crop(glyph_crop)
        glyphs.save(EVIDENCE / f"{self.host}-popup-glyphs.png")
        self.assertGreater(
            self.glyph_pixels(glyphs),
            20,
            "Live image changes must not erase popup glyphs",
        )
        self.assertEqual(self.count(self.screenshot("03-live-change"), (255, 0, 0)), 0)
        self.key("F3")
        self.wait(
            lambda: (
                self.count(self.screenshot("04-popup-moved"), (0, 0, 255)) == pixels
            ),
            "moved popup restoration",
        )
        self.key("Escape")
        self.wait(lambda: not self.state()["popups"], "popup close")
        self.wait(
            lambda: (
                self.count(self.screenshot("05-popup-closed"), (0, 0, 255)) == pixels
            ),
            "closed popup restoration",
        )
        self.send("NATIVE")
        self.wait(
            lambda: (
                (self.directory / "native-input").exists()
                and (self.directory / "native-input").read_bytes() == b"NATIVE"
            ),
            "native input restored",
        )
        if self.host == "xterm":
            self.xdo("windowsize", "--usehints", self.window, "92", "34")
        else:
            self.xdo("windowsize", self.window, "920", "680")
        self.wait(lambda: self.state()["cols"] > 80, "actual terminal resize")
        self.wait(
            lambda: self.count(self.screenshot("06-resized"), (0, 0, 255)) == pixels,
            "image after resize",
        )
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 2, "color": "red", "wide": True})
        )
        self.wait(
            lambda: (self.directory / "fixture-ack").read_text() == "2",
            "wide native image",
        )
        self.wait(
            lambda: self.count(self.screenshot("07-wide"), (255, 0, 0)) == 300 * 64,
            "wide image preserves every pixel across encoder tiles",
        )
        self.key("F2")
        self.wait(lambda: len(self.state()["popups"]) == 1, "wide image popup")
        self.wait(
            lambda: (
                self.count(self.screenshot("08-wide-popup"), (255, 0, 0))
                == 300 * 64 - 12 * cw * (64 - ch)
            ),
            "wide image popup masking",
        )
        self.key("Escape")
        self.wait(lambda: not self.state()["popups"], "wide popup close")
        self.wait(
            lambda: (
                self.count(self.screenshot("09-wide-restored"), (255, 0, 0)) == 300 * 64
            ),
            "wide image restoration",
        )
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 3, "erase": True})
        )
        self.wait(
            lambda: (self.directory / "fixture-ack").read_text() == "3",
            "native image erase",
        )
        self.wait(
            lambda: self.count(self.screenshot("07-erased"), (0, 0, 255)) == 0,
            "no stale outer image",
        )
        self.wait(
            lambda: self.count(self.screenshot("10-erased"), (255, 0, 0)) == 0,
            "no stale wide image",
        )
        (EVIDENCE / f"{self.host}-result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "protocol": self.protocol,
                    "image_pixels": pixels,
                    "popup_covered_pixels": overlap,
                }
            )
        )

    def test_native_text_without_sixel_on_xterm(self):
        """Preserve the native text UI and measured palette without advertising images."""
        profile = self.state()["host"]
        self.assertIsNone(profile["image_protocol"])
        self.assertEqual(profile["appearance"]["palette"][1], [36, 104, 172])
        self.send("foreach x (alpha beta)\n echo $x >> gui-loop\nend\n")
        result = self.directory / "gui-loop"
        self.wait(
            lambda: result.exists() and result.read_text() == "alpha\nbeta\n",
            "tcsh continuation",
        )
        self.xdo("windowsize", "--usehints", self.window, "92", "34")
        self.wait(lambda: self.state()["cols"] == 92, "native text resize")
        self.assertEqual(self.state()["rows"], 34)
        self.assertIsNone(self.state()["host"]["image_protocol"])
        self.screenshot("text-resized")
        (EVIDENCE / "xterm-result.json").write_text(
            json.dumps({"status": "passed", "protocol": None, "cols": 92, "rows": 34})
        )

    def test_wezterm_iterm(self):
        """Exercise genuine OSC 1337 input and PNG output in verified WezTerm."""
        self.exercise()


if __name__ == "__main__":
    unittest.main()
