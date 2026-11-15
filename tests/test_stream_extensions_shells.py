"""Observe editable Python shell plugins through the public CLI and real PTYs."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stream_fixture import shell_executable
from stream_plugins import copy_plugins
from test_stream_cli import BAR, F2, PROMPT, StreamProcess

SHELLS = ("bash", "zsh", "tcsh")
USER_KEYS = {
    "bash": 'bind \'"\\eOQ":"echo native > f2-result\\C-m"\'\n',
    "zsh": "bindkey -s '\\eOQ' 'echo native > f2-result\\n'\n",
    "tcsh": "bindkey -s '\\eOQ' 'echo native > f2-result\\n'\n",
}
ENVIRONMENT_WRAPPER = """
_original_build = build

def build(argv, env, directory, nonce, popup_socket, *, script_dir=None):
    argv, env = _original_build(
        argv, env, directory, nonce, popup_socket, script_dir=script_dir
    )
    return argv, dict(env, ISH_PLUGIN_PROBE={value!r})
"""
FAILING_BUILDER = """
def build(argv, env, directory, nonce, popup_socket, *, script_dir=None):
    Path(env['HOME'], 'builder-ran').write_text(str(directory))
    argv[0] = '/missing-ish-plugin-shell'
    env['HOME'] = '/missing-ish-plugin-home'
    env['ISH_PLUGIN_LEAK'] = 'mutated'
    raise RuntimeError('builder deliberately failed')
"""


class ShellPluginTests(unittest.TestCase):
    """Verify source changes affect native children without changing the stream host."""

    def setUp(self):
        """Keep Python plugins, rc files and shell results inside an isolated directory."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-shell-plugin-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def copy_plugins(self, shell):
        """Copy normal Python plugin sources without rewriting packaged implementations."""
        directory = self.root / (shell + "-plugins")
        copy_plugins(directory)
        return directory, directory / f"stream_{shell}" / f"stream_{shell}.py"

    def start(
        self, shell, directory, name, *, decorated=True, selected=None, startup=""
    ):
        """Run the real entry point with the copied Python plugin directory."""
        if not shutil.which(shell_executable(shell)):
            self.skipTest(f"{shell} is required")
        home = self.root / (shell + "-" + name)
        home.mkdir()
        process = StreamProcess(
            home,
            shell=shell,
            startup="echo started >> startup-count\n" + startup,
            demo=selected is None,
            plugins=selected or (),
            plugin_dir=directory,
        )
        self.addCleanup(process.stop)
        process.expect(PROMPT)
        if decorated:
            process.expect(BAR)
        else:
            process.demo = False
        return process, home

    def test_python_builder_edits_apply_to_the_next_native_session(self):
        """An external Python edit changes the actual child's environment on all shells."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                plugins, path = self.copy_plugins(shell)
                original = path.read_text()
                for value in ("first", "later"):
                    with self.subTest(value=value):
                        path.write_text(
                            original + ENVIRONMENT_WRAPPER.format(value=value),
                            encoding="utf-8",
                        )
                        process, home = self.start(shell, plugins, value)
                        process.command(
                            "printf '%s\\n' \"$ISH_PLUGIN_PROBE\" > plugin-result"
                        )
                        process.expect_file(home / "plugin-result", value + "\n")
                        self.assertEqual(
                            (home / "startup-count").read_text(), "started\n"
                        )

    def test_unregistered_shell_falls_back_without_ui_or_reserved_input(self):
        """Removing a copied Python registration preserves native startup and input."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                plugins, path = self.copy_plugins(shell)
                path.write_text(
                    path.read_text().replace("api.add_shell(matches, build)", "pass"),
                    encoding="utf-8",
                )
                process, home = self.start(
                    shell, plugins, "unregistered", decorated=False
                )
                result = home / "native-input"
                process.fixture("input", result)
                payload = b"native" + F2 + b"\x03\x04END"
                process.send(payload)
                process.expect_file(result, payload)
                self.assertEqual((home / "startup-count").read_text(), "started\n")
                self.assertNotIn(BAR, process.output)
                self.assertNotIn(b"Plugin popup", process.output)
                self.assertIn(b"starting without UI", process.output)

    def test_mutating_failed_builder_preserves_native_startup_arguments(self):
        """A rejected provider cannot poison fallback argv, HOME or environment."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                plugins, path = self.copy_plugins(shell)
                path.write_text(path.read_text() + FAILING_BUILDER, encoding="utf-8")
                process, home = self.start(shell, plugins, "failed", decorated=False)
                process.command("env > child-env")
                environment = (home / "child-env").read_text().splitlines()
                self.assertIn("HOME=" + str(home), environment)
                self.assertNotIn("ISH_PLUGIN_LEAK=mutated", environment)
                self.assertEqual((home / "startup-count").read_text(), "started\n")
                self.assertIn(b"builder deliberately failed", process.output)
                self.assertNotIn(BAR, process.output)
                private = Path((home / "builder-ran").read_text())
                self.assertFalse(private.exists())

    def test_shell_plugin_without_ui_never_builds_or_installs_hooks(self):
        """A selected provider alone does not change native startup or function keys."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                plugins, path = self.copy_plugins(shell)
                path.write_text(path.read_text() + FAILING_BUILDER, encoding="utf-8")
                process, home = self.start(
                    shell,
                    plugins,
                    "shell-only",
                    decorated=False,
                    selected=(f"stream_{shell}",),
                )
                process.command("echo preserved > result")
                process.expect_file(home / "result", "preserved\n")
                self.assertFalse((home / "builder-ran").exists())
                self.assertEqual((home / "startup-count").read_text(), "started\n")
                self.assertNotIn(BAR, process.output)
                self.assertNotIn(b"\x1b]777;ish;", process.output)
                self.assertNotIn(b"starting without UI", process.output)

    def test_bar_without_popup_preserves_the_users_native_f2_binding(self):
        """A displayed bar does not reserve the native editor's F2 key."""
        for shell in SHELLS:
            with self.subTest(shell=shell):
                plugins, _ = self.copy_plugins(shell)
                process, home = self.start(
                    shell,
                    plugins,
                    "bar-only",
                    selected=("stream_bar", f"stream_{shell}"),
                    startup=USER_KEYS[shell],
                )
                process.send(F2)
                process.expect_file(home / "f2-result", "native\n")
                process.command("echo preserved > result")
                process.expect_file(home / "result", "preserved\n")
                self.assertNotIn(b"Plugin popup", process.output)


if __name__ == "__main__":
    unittest.main()
