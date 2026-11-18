"""Verify management keyboard paths, public CLI files and actual PTY restoration."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from ish.config import config
from ish.lang import i18n
from ish.manage.app import Screen, SettingsWindow
from ish.manage.store import SettingsStore
from ish.terminal.settings import Settings, load
from management_host import ManagementTerminal
from test_terminal_cli import ROOT


class ManagementFixture(unittest.TestCase):
    """Keep settings and plugin source isolated from the actual user's home."""

    def setUp(self):
        """Use English assertions without modifying global configuration after a test."""
        self.directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ish-management-")
        )
        self.root = Path(self.directory)
        self.home = self.root / "ish"
        self.home.mkdir()
        self.rc = self.home / ".ishrc.py"
        self.enterContext(patch.object(config, "_ish_home", self.home))
        previous = i18n.current_lang, i18n.base_path, i18n.message
        self.addCleanup(setattr, i18n, "current_lang", previous[0])
        self.addCleanup(setattr, i18n, "base_path", previous[1])
        self.addCleanup(setattr, i18n, "message", previous[2])
        i18n.base_path = self.home / "lang"
        i18n.load("en")

    def store(self):
        """Read settings using the actual next-session configuration evaluator."""
        return SettingsStore(self.rc, settings_type=Settings, loader=load)

    def plugin(self):
        """Provide metadata with an observable import trap."""
        directory = self.home / "plugin/script/chosen"
        directory.mkdir(parents=True)
        marker = self.root / "plugin-executed"
        (directory / "__init__.py").write_text(
            "PLUGIN_META = {'version': '1.0', 'description': 'Selectable example'}\n"
            f"open({str(marker)!r}, 'w').write('executed')\n"
        )
        return marker


class PipeInputTests(ManagementFixture):
    """Exercise PTK's actual input parser and focus bindings without native emulation."""

    def run_keys(self, screen, pipe, keys):
        """Send keys only after the Application reaches its first actual render."""

        async def drive():
            ready = asyncio.Event()
            screen.app.before_render += lambda _: ready.set()
            task = asyncio.create_task(screen.app.run_async())
            try:
                await asyncio.wait_for(ready.wait(), 3)
                pipe.send_bytes(keys)
                return await asyncio.wait_for(task, 5)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        return asyncio.run(drive())

    def test_help_category_keys_move_focus_then_close_without_files(self):
        """Arrow and Tab keys navigate categories through PTK, not direct callbacks."""
        with create_pipe_input() as pipe:
            screen = Screen(
                "Help",
                [
                    ("First", TextArea(text="One", read_only=True)),
                    ("Second", TextArea(text="Two", read_only=True)),
                ],
                "Esc closes",
                input=pipe,
                output=DummyOutput(),
            )
            self.assertFalse(self.run_keys(screen, pipe, b"\x1b[B\r\t\x1b"))
            self.assertEqual(screen.index, 1)
        self.assertFalse(self.rc.exists())

    def test_language_keys_save_the_next_session_language(self):
        """Native PTK RadioList keys change the file the next session consumes."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertTrue(self.run_keys(window.screen, pipe, b"\r\x1b[B\x1b[B \x13"))
        self.assertEqual(load(self.rc).lang, "ko")
        self.assertIn("config.lang = 'ko'", self.rc.read_text())

    def test_feature_choices_do_not_offer_auto_without_a_query(self):
        """The displayed policy options follow the public support-query contract."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertEqual(
                [value for value, _ in window.feature_choices["hyperlinks"].values],
                ["on", "off"],
            )
            self.assertEqual(
                [
                    value
                    for value, _ in window.feature_choices["enhanced_keyboard"].values
                ],
                ["on", "off", "auto"],
            )
            # Features category -> first policy -> off -> save.
            self.assertTrue(
                self.run_keys(window.screen, pipe, b"\x1b[B\x1b[B\r\x1b[B \x13")
            )
        self.assertEqual(load(self.rc).features["hyperlinks"], "off")

    def test_plugin_selection_uses_one_catalog_and_never_imports_the_plugin(self):
        """Tab reaches the shared selection list and persists only its chosen name."""
        marker = self.plugin()
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertTrue(self.run_keys(window.screen, pipe, b"\x1b[B\r\t\t\t \x13"))
        self.assertEqual(load(self.rc).plugins, ["chosen"])
        self.assertFalse(marker.exists())

    def test_cancel_discards_a_changed_draft(self):
        """Selecting a different language is not persistent until explicit save."""
        self.rc.write_text("config.lang = 'en'\n")
        original = self.rc.read_bytes()
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertFalse(self.run_keys(window.screen, pipe, b"\r\x1b[B \x1b"))
        self.assertEqual(self.rc.read_bytes(), original)

    def test_close_keys_also_work_while_a_text_field_has_focus(self):
        """Global cancellation remains available after entering a form text field."""
        for key in (b"\x03", b"\x04", b"\x1b"):
            with self.subTest(key=key), create_pipe_input() as pipe:
                window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
                self.assertFalse(self.run_keys(window.screen, pipe, b"\r\t" + key))
        self.assertFalse(self.rc.exists())

    def test_alt_editing_keeps_the_field_open_and_standalone_escape_closes_promptly(
        self,
    ):
        """Alt editing retains its PTK consumer without adding a one-second ESC wait."""

        async def check(key, position, expected, *, split=False):
            with create_pipe_input() as pipe:
                field = TextArea(text="alpha beta gamma", multiline=False)
                screen = Screen(
                    "Editing",
                    [("Field", field)],
                    "Esc closes",
                    input=pipe,
                    output=DummyOutput(),
                )
                ready = asyncio.Event()
                screen.app.after_render += lambda _: ready.set()
                task = asyncio.create_task(screen.app.run_async())
                try:
                    await asyncio.wait_for(ready.wait(), 3)
                    pipe.send_bytes(b"\r" + position)
                    if split:
                        pipe.send_bytes(key[:1])
                        await asyncio.sleep(0.01)
                        pipe.send_bytes(key[1:])
                    else:
                        pipe.send_bytes(key)
                    for _ in range(100):
                        if (
                            task.done()
                            or (field.text, field.buffer.cursor_position) == expected
                        ):
                            break
                        await asyncio.sleep(0.01)
                    self.assertFalse(task.done(), "Alt was consumed as cancellation")
                    self.assertEqual(
                        (field.text, field.buffer.cursor_position), expected
                    )
                    self.assertEqual(screen.app.ttimeoutlen, 0.02)
                    pipe.send_bytes(b"\x1b")
                    self.assertFalse(await asyncio.wait_for(task, 0.25))
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        cases = (
            (b"\x1bb", b"\x05", ("alpha beta gamma", 11)),
            (b"\x1bf", b"\x01", ("alpha beta gamma", 5)),
            (b"\x1b\x7f", b"\x05", ("alpha beta ", 11)),
        )
        for key, position, expected in cases:
            for split in (False, True):
                with self.subTest(key=key, split=split):
                    asyncio.run(check(key, position, expected, split=split))

    def test_invalid_rc_and_unreadable_plugin_catalog_keep_the_window_usable(self):
        """Invalid source remains editable and catalog errors do not destroy forms."""
        self.rc.write_text("raise RuntimeError('repair this')\n")
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertIsNotNone(window.store.error)
            self.assertEqual(window.feature_choices["hyperlinks"].current_value, "on")
            self.assertFalse(self.run_keys(window.screen, pipe, b"\x1b"))
        plugin_file = self.root / "not-a-directory"
        plugin_file.write_text("original")
        self.rc.write_text(f"config.plugin_dir = {str(plugin_file)!r}\n")
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.assertIsInstance(window.plugin_error, NotADirectoryError)
            window.refresh()
            self.assertFalse(self.run_keys(window.screen, pipe, b"\x1b"))
        self.assertEqual(plugin_file.read_text(), "original")

    def test_empty_catalog_can_be_refreshed_and_reloaded(self):
        """An empty selection is a supported state during repeated catalog changes."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            window.refresh()
            window.refresh()
            window.reload()
            self.assertIsNone(window.plugin_choices)
            self.assertFalse(self.run_keys(window.screen, pipe, b"\x1b"))


class PublicManagementTests(ManagementFixture):
    """Use the public python -m ish command, actual controlling PTYs and final files."""

    def terminal(self, option, **options):
        """Start one public management command and always clean its child processes."""
        terminal = ManagementTerminal(self.root, option, **options)
        self.addCleanup(terminal.close)
        return terminal

    def test_help_is_categorized_readonly_and_does_not_execute_rc_or_plugins(self):
        """Help renders with an invalid engine path and executable user-code traps."""
        plugin_marker = self.plugin()
        rc_marker = self.root / "rc-executed"
        self.rc.write_text(
            f"open({str(rc_marker)!r}, 'w').write('executed')\nraise RuntimeError('must not run')\n"
        )
        original = self.rc.read_bytes()
        terminal = self.terminal("-h")
        terminal.require(lambda: terminal.frame_has("ish — Help"), timeout=45)
        terminal.require(lambda: terminal.frame_has("Getting started"))
        terminal.send(b"\x1b[B\x1b[B")
        terminal.require(lambda: terminal.frame_has(".ishrc.py"))
        terminal.finish()
        self.assertEqual(self.rc.read_bytes(), original)
        self.assertFalse(rc_marker.exists())
        self.assertFalse(plugin_marker.exists())
        self.assertFalse((self.home / ".ish.log").exists())

    def test_settings_save_from_keyboard_changes_the_actual_file(self):
        """The command's saved language is read by the normal next-session loader."""
        terminal = self.terminal("--config")
        terminal.require(lambda: terminal.frame_has("ish — Settings"), timeout=45)
        terminal.send(b"\r\x1b[B\x1b[B \x13")
        terminal.finish(key=b"")
        self.assertEqual(load(self.rc).lang, "ko")

    def test_failed_rc_preserves_language_precedence_without_using_invalid_fields(self):
        """A broken setting keeps valid rc language; invalid language falls back to LANG."""
        cases = (
            ("ko", "C.UTF-8", None, "ish — 설정"),
            ("../ko", "ko_KR.UTF-8", None, "ish — 설정"),
            ("ko", "ko_KR.UTF-8", "en", "ish — Settings"),
            ("../ko", "C.UTF-8", None, "ish — Settings"),
        )
        for language, locale, override, title in cases:
            with self.subTest(language=language, locale=locale, override=override):
                original = f"config.lang = {language!r}\nconfig.mouse = 'invalid'\n"
                self.rc.write_text(original)
                terminal = self.terminal(
                    "-c", language=override, environment={"LANG": locale}
                )
                terminal.require(
                    lambda terminal=terminal, title=title: terminal.frame_has(title),
                    timeout=45,
                )
                terminal.finish()
                self.assertEqual(self.rc.read_text(), original)

    def test_ctrl_c_ctrl_d_and_escape_restore_outer_termios_after_resize(self):
        """All close paths preserve files and restore the controlling PTY after resize."""
        self.rc.write_text("config.lang = 'en'\n")
        original = self.rc.read_bytes()
        for key in (b"\x03", b"\x04", b"\x1b"):
            with self.subTest(key=key):
                terminal = self.terminal("-c")
                terminal.require(
                    lambda terminal=terminal: terminal.frame_has("ish — Settings"),
                    timeout=45,
                )
                terminal.resize(20, 86)
                terminal.require(
                    lambda terminal=terminal: terminal.frame_has("ish — Settings")
                )
                terminal.finish(key=key)
                self.assertEqual(self.rc.read_bytes(), original)

    def test_external_change_blocks_save_until_reload(self):
        """A changed file is not overwritten and the same running UI can recover."""
        self.rc.write_text("config.lang = 'en'\n")
        terminal = self.terminal("-c")
        terminal.require(lambda: terminal.frame_has("ish — Settings"), timeout=45)
        external = "config.lang = 'en'\n# changed elsewhere\n"
        self.rc.write_text(external)
        # Initially selected English; down selects Korean.
        terminal.send(b"\r\x1b[B \x13")
        terminal.require(lambda: terminal.frame_has("changed outside"))
        self.assertFalse(terminal.exited())
        self.assertEqual(self.rc.read_text(), external)
        terminal.send(b"\x12")
        terminal.require(lambda: terminal.frame_has("reloaded from disk"))
        terminal.finish()
        self.assertEqual(self.rc.read_text(), external)

    def test_external_editor_uses_literal_argv_and_reload_recovers_invalid_rc(self):
        """Editor arguments retain spaces and shell metacharacters without execution."""
        editor = self.root / "editor with spaces.py"
        args_file = self.root / "editor-args.json"
        editor.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            f"Path({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "Path(sys.argv[-1]).write_text(\"config.lang = 'en'\\nconfig.mouse = 'off'\\n\")\n"
        )
        token = "literal;$(touch INJECTED)"
        self.rc.write_text("raise RuntimeError('repair this')\n")
        terminal = self.terminal(
            "-c",
            environment={"EDITOR": shlex.join([sys.executable, str(editor), token])},
        )
        terminal.require(lambda: terminal.frame_has("ish — Settings"), timeout=45)
        terminal.send(b"\x05")
        terminal.require(lambda: terminal.frame_has("reloaded from disk"))
        terminal.finish()
        self.assertEqual(json.loads(args_file.read_text()), [token, str(self.rc)])
        self.assertEqual(load(self.rc).mouse, "off")
        self.assertFalse((self.root / "INJECTED").exists())

    def test_editor_interrupt_leaves_no_child_and_restores_the_terminal(self):
        """An interrupt during the external editor cleans up both PTK and its child."""
        editor = self.root / "waiting-editor.py"
        pid_file = self.root / "editor-pid"
        editor.write_text(
            "import os, signal, time\nfrom pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "signal.signal(signal.SIGINT, lambda *_: exit(0))\n"
            "while True: time.sleep(0.1)\n"
        )
        self.rc.write_text("config.lang = 'en'\n")
        terminal = self.terminal(
            "-c", environment={"EDITOR": shlex.join([sys.executable, str(editor)])}
        )
        terminal.require(lambda: terminal.frame_has("ish — Settings"), timeout=45)
        terminal.send(b"\x05")
        terminal.require(pid_file.exists)
        editor_pid = int(pid_file.read_text())
        terminal.send(b"\x03")
        terminal.require(
            lambda: terminal.exited() or terminal.frame_has("reloaded from disk")
        )
        terminal.finish(key=b"" if terminal.exited() else b"\x1b")
        with self.assertRaises(ProcessLookupError):
            os.kill(editor_pid, 0)

    def test_redirected_help_is_plain_and_config_rejects_missing_tty_before_rc(self):
        """Piped documentation is readable while an interactive edit needs a terminal."""
        marker = self.root / "executed"
        self.rc.write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
        environment = {
            **os.environ,
            "HOME": str(self.root),
            "PYTHONPATH": str(ROOT / "src"),
            "LANG": "C.UTF-8",
        }
        help_result = subprocess.run(
            [sys.executable, "-m", "ish", "--help", "--home", str(self.home)],
            env=environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(b"Getting started", help_result.stdout)
        self.assertIn(b".ishrc.py", help_result.stdout)
        self.assertNotIn(b"\x1b[", help_result.stdout)
        settings = subprocess.run(
            [sys.executable, "-m", "ish", "-c", "--home", str(self.home)],
            env=environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(settings.returncode, 2)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
