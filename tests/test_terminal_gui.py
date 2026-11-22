"""Opt-in pixel verification in a real Kitty renderer running under Xvfb."""

import json
import os
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from terminal_plugin import plugin_options

ROOT = Path(__file__).resolve().parents[1]
KITTY = ROOT / ".cache/terminal-test/kitty/bin/kitty"
KITTEN = KITTY.with_name("kitten")
EVIDENCE = ROOT / ".cache/terminal-test/evidence"
DEADLINE = 30


@unittest.skipUnless(
    os.environ.get("ISH_TEST_GUI") == "1",
    "Set ISH_TEST_GUI=1 for real Kitty/Xvfb verification",
)
class TerminalGuiTests(unittest.TestCase):
    """Assert final pixels and actual consumers, not only internal VT snapshots."""

    def setUp(self):
        """Start isolated display and terminal processes without user configuration."""
        from PIL import Image

        self.image_type = Image
        self.assertTrue(KITTY.is_file(), f"Missing portable Kitty at {KITTY}")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / getattr(self, "evidence_name", "result.json")).write_text(
            json.dumps({"status": "running"})
        )
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-gui-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dump = self.directory / "state.json"
        self.socket = "unix:" + str(self.directory / "kitty.sock")
        self.log = (EVIDENCE / "kitty.log").open("w")
        self.addCleanup(self.log.close)
        authority = self.directory / "xauthority"
        cookie = secrets.token_hex(16)
        subprocess.run(
            [
                "xauth",
                "-f",
                str(authority),
                "add",
                "localhost:0",
                "MIT-MAGIC-COOKIE-1",
                cookie,
            ],
            check=True,
            capture_output=True,
        )
        self.display = subprocess.Popen(
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
        self.addCleanup(self.stop, self.display)
        readable, _, _ = select.select([self.display.stdout], [], [], DEADLINE)
        self.assertTrue(readable, "Xvfb did not allocate a display")
        display_id = self.display.stdout.readline().decode().strip()
        self.assertTrue(display_id.isdigit(), "Xvfb startup failed")
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
        self.env = {
            **os.environ,
            "DISPLAY": "localhost:" + display_id,
            "XAUTHORITY": str(authority),
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": str(
                ROOT / ".cache/terminal-test/deps/usr/lib/x86_64-linux-gnu"
            ),
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
            "ISH_GUI_DIR": str(self.directory),
        }
        options = (
            "allow_remote_control=socket-only",
            "linux_display_server=x11",
            "remember_window_size=no",
            "initial_window_width=800",
            "initial_window_height=600",
            "window_padding_width=0",
            "font_family=DejaVu Sans Mono",
            "font_size=12",
            "background=#102030",
            "foreground=#eeeeee",
            "enable_audio_bell=no",
            "shell_integration=disabled",
        )
        command = [str(KITTY), "--config", "NONE", "--listen-on", self.socket]
        for option in options:
            command += ["-o", option]
        command += [
            sys.executable,
            "-m",
            "ish.terminal",
            "--shell",
            "tcsh",
            "--cwd",
            str(self.directory),
            "--dump-state",
            str(self.dump),
        ]
        if self._testMethodName != "test_base_has_no_plugin_ui_or_reserved_keys":
            command += plugin_options(self.directory)
        if self._testMethodName == "test_native_shell_and_image_popup_composition":
            shutil.copytree(
                ROOT / "bundles/plugins/terminal_history",
                self.directory / "plugins/terminal_history",
            )
            command += ["--plugin", "terminal_history"]
        self.process = subprocess.Popen(
            command,
            env=self.env,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )
        self.addCleanup(self.stop, self.process)
        self.wait(lambda: self.remote("ls"), "Kitty remote-control socket")
        self.wait(lambda: self.dump.exists(), "ish PTK startup")

    @staticmethod
    def stop(process):
        """Stop only a process group created by this test."""
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

    def remote(self, *arguments, text=None):
        """Exercise the terminal's official remote-control protocol."""
        result = subprocess.run(
            [str(KITTEN), "@", "--to", self.socket, *arguments],
            input=text,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def wait(self, predicate, description):
        """Use timeouts only to fail a test, never to infer successful readiness."""
        deadline = time.monotonic() + DEADLINE
        last = ""
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, RuntimeError, json.JSONDecodeError) as error:
                last = str(error)
            if hasattr(self, "process") and self.process.poll() is not None:
                break
            time.sleep(0.05)
        self.log.flush()
        if self.dump.exists():
            (EVIDENCE / "failure-state.json").write_text(self.dump.read_text())
        if (self.directory / "native-input").exists():
            (EVIDENCE / "failure-native-input.bin").write_bytes(
                (self.directory / "native-input").read_bytes()
            )
        try:
            (EVIDENCE / "failure-text.txt").write_text(self.remote("get-text"))
            self.screenshot("failure.png")
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        self.fail(
            f"Timed out waiting for {description}: {last}\n{(EVIDENCE / 'kitty.log').read_text()[-4000:]}"
        )

    def send(self, text):
        """Send literal bytes through the real outer terminal input path."""
        self.remote("send-text", "--stdin", text=text)

    def state(self):
        """Read opt-in diagnostics only as a test synchronization aid."""
        return json.loads(self.dump.read_text())

    def screenshot(self, name):
        """Capture Kitty's final GPU-composited pixels as evidence."""
        path = EVIDENCE / name
        self.remote("screenshot", str(path))
        with self.image_type.open(path) as image:
            return image.convert("RGB")

    @staticmethod
    def count(image, color):
        """Count exact image colors independently from terminal state."""
        return sum(pixel == color for pixel in image.get_flattened_data())

    def test_native_shell_and_image_popup_composition(self):
        """Verify native shell editing, pixels, moving popup, resize and input isolation."""
        self.send("foreach x (alpha beta)\n echo $x >> gui-loop\nend\n")
        loop = self.directory / "gui-loop"
        self.wait(
            lambda: loop.exists() and loop.read_text() == "alpha\nbeta\n",
            "tcsh continuation file",
        )
        self.send("cat > gui-here << END\n  한글 공백  \npython_tool_name\nEND\n")
        here = self.directory / "gui-here"
        self.wait(
            lambda: (
                here.exists()
                and here.read_text() == "  한글 공백  \npython_tool_name\n"
            ),
            "native heredoc bytes",
        )
        self.send("vim -Nu NONE -n gui-vim\n")
        self.wait(lambda: self.state()["alternate"], "Vim alternate screen")
        self.remote("send-key", "i")
        self.send("GUI_VIM")
        self.remote("send-key", "escape")
        self.send(":wq\n")
        saved = self.directory / "gui-vim"
        self.wait(
            lambda: saved.exists() and saved.read_text() == "GUI_VIM\n",
            "Vim saved bytes",
        )
        self.wait(lambda: not self.state()["alternate"], "native shell after Vim exit")
        self.send(
            shlex.join(
                [
                    sys.executable,
                    str(ROOT / "tests/terminal_fixture.py"),
                    str(self.directory),
                ]
            )
            + "\n"
        )
        self.wait(
            lambda: (self.directory / "fixture-ready").exists(),
            "native graphics fixture",
        )
        image = self.wait(
            lambda: (
                shot
                if self.count(shot := self.screenshot("01-image.png"), (255, 0, 0))
                > 1000
                else None
            ),
            "actual red image pixels",
        )
        red_area = self.count(image, (255, 0, 0))
        self.remote("send-key", "f2")
        popup = self.wait(
            lambda: (
                shot
                if self.count(shot := self.screenshot("02-popup.png"), (48, 160, 80))
                > 100
                else None
            ),
            "actual opaque PTK popup pixels",
        )
        self.assertGreater(self.count(popup, (255, 0, 0)), 0)
        self.assertLess(self.count(popup, (255, 0, 0)), red_area)
        self.send("POPUP_ONLY")
        self.wait(
            lambda: (
                (self.directory / "popup-input").exists()
                and "POPUP_ONLY" in (self.directory / "popup-input").read_text()
            ),
            "popup input consumer",
        )
        native = self.directory / "native-input"
        self.assertFalse(
            native.exists() and native.read_bytes(),
            "Popup input leaked to native stdin",
        )
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 1, "color": "blue"})
        )
        self.wait(
            lambda: (self.directory / "fixture-ack").exists(), "updated native image"
        )
        changed = self.wait(
            lambda: (
                shot
                if self.count(
                    shot := self.screenshot("03-live-change.png"), (0, 0, 255)
                )
                > 100
                else None
            ),
            "blue image behind the still-open popup",
        )
        self.assertEqual(self.count(changed, (255, 0, 0)), 0)
        self.assertGreater(self.count(changed, (48, 160, 80)), 100)
        self.remote("send-key", "f3")
        self.wait(
            lambda: (
                self.count(self.screenshot("04-popup-moved.png"), (0, 0, 255))
                == red_area
            ),
            "restored image after popup movement",
        )
        self.remote("send-key", "escape")
        self.wait(
            lambda: (
                self.count(self.screenshot("05-popup-closed.png"), (48, 160, 80)) == 0
            ),
            "popup closure",
        )
        self.send("NATIVE")
        self.wait(
            lambda: native.exists() and native.read_bytes() == b"NATIVE",
            "native input after focus restoration",
        )
        old_size = (self.state()["cols"], self.state()["rows"])
        self.remote(
            "resize-os-window",
            "--action",
            "resize",
            "--width",
            "920",
            "--height",
            "680",
            "--unit",
            "pixels",
        )
        self.wait(
            lambda: (self.state()["cols"], self.state()["rows"]) != old_size,
            "real OS resize propagation",
        )
        self.wait(
            lambda: (
                self.count(self.screenshot("06-resized.png"), (0, 0, 255)) == red_area
            ),
            "image pixels after resize",
        )
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 2, "scroll": 20})
        )
        self.wait(
            lambda: (self.directory / "fixture-ack").read_text() == "2",
            "native image scroll into history",
        )
        self.wait(
            lambda: (
                self.count(self.screenshot("09-image-in-history.png"), (0, 0, 255)) == 0
            ),
            "offscreen image placement cleanup",
        )
        self.remote("send-key", "shift+page_up")
        self.wait(
            lambda: (
                self.count(
                    self.screenshot("10-history-image-restored.png"), (0, 0, 255)
                )
                == red_area
            ),
            "native image restored from scrollback",
        )
        self.remote("send-key", "shift+page_down")
        self.wait(
            lambda: (
                self.count(self.screenshot("11-returned-live.png"), (0, 0, 255)) == 0
            ),
            "return to live viewport without stale images",
        )
        (self.directory / "fixture-control").write_text(
            json.dumps({"sequence": 3, "exit": True})
        )
        self.wait(
            lambda: (self.directory / "fixture-done").exists(), "fixture completion"
        )
        self.assertEqual(native.read_bytes(), b"NATIVE")
        (EVIDENCE / "result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "terminal": "Kitty 0.50.0",
                    "renderer": "Xvfb/software GL",
                    "native_loop": loop.read_text(),
                    "heredoc": here.read_text(),
                    "vim": saved.read_text(),
                    "native_input": native.read_text(),
                    "image_pixels": red_area,
                    "resize": self.state(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def test_base_has_no_plugin_ui_or_reserved_keys(self):
        """The unchanged full shell owns F2, interrupt and EOF bytes without plugins."""
        self.assertEqual(self.state()["popups"], [])
        self.send(
            shlex.join(
                [
                    sys.executable,
                    str(ROOT / "tests/terminal_fixture.py"),
                    str(self.directory),
                ]
            )
            + "\n"
        )
        self.wait(
            lambda: (self.directory / "fixture-ready").exists(),
            "default native input fixture",
        )
        self.wait(
            lambda: (
                self.count(self.screenshot("07-base-no-plugins.png"), (255, 0, 0))
                > 1000
            ),
            "default native image without plugin UI",
        )
        self.remote("send-key", "f2")
        self.remote("send-key", "ctrl+c")
        self.remote("send-key", "ctrl+d")
        result = self.directory / "native-input"
        self.wait(
            lambda: result.exists() and result.read_bytes().endswith(b"\x03\x04"),
            "unreserved default key bytes",
        )
        self.assertEqual(result.read_bytes(), b"\x1bOQ\x03\x04")
        self.assertEqual(self.state()["popups"], [])
        image = self.screenshot("08-base-native-keys.png")
        self.assertEqual(self.count(image, (48, 160, 80)), 0)
        (EVIDENCE / "base-result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "plugins": [],
                    "native_input_hex": result.read_bytes().hex(),
                    "cols": self.state()["cols"],
                    "rows": self.state()["rows"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    unittest.main()
