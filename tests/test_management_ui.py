"""Verify management keyboard paths, public CLI files and actual PTY restoration."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from functools import partial
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from wcwidth import wcswidth

from ish.config import config
from ish.lang import i18n
from ish.manage.app import Screen, SettingsWindow
from ish.manage.palette import PanelStyle, ui_style
from ish.manage.store import SettingsStore
from ish.plugin.manager import PluginManager
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

    def test_unchanged_hover_and_navigation_keep_the_frame(self):
        """Repaint changed hover, but leave the same rail and boundary keys idle."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            app = window.screen.app

            async def check():
                rendered, handled = asyncio.Event(), asyncio.Event()
                app.after_render += lambda _: rendered.set()
                app.key_processor.after_key_press += lambda _: handled.set()
                task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.wait_for(rendered.wait(), 5)
                    x, y = max(
                        (x, y)
                        for y, row in app.renderer.last_rendered_screen.data_buffer.items()
                        for x, cell in row.items()
                        if cell.char == "△"
                    )
                    rendered.clear()
                    pipe.send_bytes(f"\x1b[<35;{x + 1};{y + 2}M".encode())
                    await asyncio.wait_for(rendered.wait(), 5)
                    repaints = []
                    app.on_invalidate += lambda _: repaints.append(True)
                    handled.clear()
                    pipe.send_bytes(f"\x1b[<35;{x + 1};{y + 3}M".encode())
                    await asyncio.wait_for(handled.wait(), 5)
                    self.assertEqual(repaints, [])
                    rendered.clear()
                    pipe.send_bytes(f"\x1b[<35;{x};{y + 3}M".encode())
                    await asyncio.wait_for(rendered.wait(), 5)
                    self.assertTrue(repaints)
                    repaints.clear()
                    handled.clear()
                    pipe.send_bytes(b"\x1b[A")
                    await asyncio.wait_for(handled.wait(), 5)
                    self.assertEqual(repaints, [])
                finally:
                    app.exit()
                    await task

            asyncio.run(check())

    def test_inactive_style_desaturates_swatches_and_preserves_host_defaults(self):
        """A panel change only alters the RGB channels of resolved colored text."""
        base = Style.from_dict({"sample": "fg:#ff0000 bold"})
        style = PanelStyle(lambda: base)
        active = style.get_attrs_for_style_str("class:sample bg:#0000ff")
        inactive = style.get_attrs_for_style_str(
            "class:panel.inactive class:sample bg:#0000ff"
        )
        self.assertEqual(inactive, active._replace(color="363636", bgcolor="121212"))
        self.assertEqual(
            style.get_attrs_for_style_str("class:panel.inactive").bgcolor,
            style.get_attrs_for_style_str("").bgcolor,
        )

    def test_scrollbar_uses_the_chosen_background_even_when_it_matches_the_rail(self):
        """A deliberately matching or host-default thumb must not reverse the cell."""
        for token, rgb in (("#123456", "123456"), ("default", "default")):
            with self.subTest(token=token):
                style = ui_style(
                    {"background": token, "accent": token, "muted2": token}
                )
                for part in ("button", "highlight"):
                    cell = style.get_attrs_for_style_str(f"class:scrollbar.{part}")
                    self.assertEqual((cell.bgcolor, cell.reverse), (rgb, False))

    def run_keys(self, screen, pipe, keys, *, until=None):
        """Send real input and, for non-closing actions, wait for their visible result."""

        async def drive():
            ready = asyncio.Event()
            screen.app.before_render += lambda _: ready.set()
            task = asyncio.create_task(screen.app.run_async())
            try:
                await asyncio.wait_for(ready.wait(), 3)
                if until is not None:
                    completed = asyncio.Event()

                    def observe(app):
                        if until():
                            completed.set()

                    screen.app.after_render += observe
                pipe.send_bytes(keys)
                if until is not None:
                    await asyncio.wait_for(completed.wait(), 5)
                    self.assertFalse(
                        task.done(), "The action unexpectedly closed the form"
                    )
                    screen.app.exit(result=False)
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
                input=pipe,
                output=DummyOutput(),
            )
            self.assertFalse(self.run_keys(screen, pipe, b"\x03\x1b[B\r\x1b[D\x1b"))
            self.assertEqual(screen.index, 1)
        self.assertFalse(self.rc.exists())

    def test_language_keys_save_the_next_session_language(self):
        """Opening a field, choosing a language and saving persist to the next session."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.run_keys(
                window.screen,
                pipe,
                b"\r\r\x1b[B\x1b[B\r\t\x1b[C\r",
                until=self.rc.exists,
            )
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
            # Terminal group -> Host features -> first policy -> Off -> save.
            self.run_keys(
                window.screen,
                pipe,
                b"\t\x1b[B\r\r\x1b[B\r\t\x1b[C\r",
                until=self.rc.exists,
            )
        self.assertEqual(load(self.rc).features["hyperlinks"], "off")

    def test_plugin_selection_uses_one_catalog_and_never_imports_the_plugin(self):
        """Navigate groups and fields, then toggle a plugin once and save it."""
        marker = self.plugin()
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())

            def choose(app):
                if window.catalog_choices["plugin"] is not None:
                    app.after_render -= choose
                    pipe.send_bytes(b"\x1b[B" * 2 + b" \t\x1b[C\r")

            window.screen.app.after_render += choose
            self.run_keys(
                window.screen,
                pipe,
                b"\t\t\r",
                until=self.rc.exists,
            )
        self.assertEqual(load(self.rc).plugins, ["chosen"])
        self.assertFalse(marker.exists())

    def test_cancel_discards_a_changed_draft(self):
        """Selecting a different language is not persistent until explicit save."""
        self.rc.write_text("config.lang = 'en'\n")
        original = self.rc.read_bytes()
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            self.run_keys(
                window.screen,
                pipe,
                b"\r\r\x1b[B\r\t\r",
                until=lambda: window.screen.status == "Draft discarded.",
            )
            self.assertEqual(window.language.current_value, "en")
        self.assertEqual(self.rc.read_bytes(), original)

    def test_ctrl_c_and_ctrl_d_leave_the_settings_window_open(self):
        """After Ctrl+C or Ctrl+D, the field can still open the option filter."""
        for key in (b"\x03", b"\x04"):
            with self.subTest(key=key), create_pipe_input() as pipe:
                window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
                screen = window.screen
                self.run_keys(
                    screen,
                    pipe,
                    b"\r\x1b[B\r" + key + b"\x06",
                    until=partial(screen.app.layout.has_focus, screen.filter_input),
                )
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
                        first_read = asyncio.Event()
                        read_keys = pipe.read_keys

                        def finish_sequence():
                            keys = read_keys()
                            if not first_read.is_set():
                                pipe.send_bytes(key[1:])
                                first_read.set()
                            return keys

                        # Two actual reads within one Alt sequence, independent of
                        # how long the host scheduler takes to resume a sleep.
                        with patch.object(pipe, "read_keys", finish_sequence):
                            pipe.send_bytes(key[:1])
                            await asyncio.wait_for(first_read.wait(), 3)
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
                    sidebar = asyncio.Event()

                    def check_focus(app):
                        if app.layout.current_control is screen.nav:
                            sidebar.set()

                    screen.app.after_render += check_focus
                    pipe.send_bytes(b"\x1b")
                    await asyncio.wait_for(sidebar.wait(), 0.25)
                    self.assertFalse(task.done())
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
            window.reload()
            self.assertIn("repair this", window.screen.status_line(80)[0][1])
            self.run_keys(
                window.screen,
                pipe,
                b"\r\t",
                until=lambda: window.screen.app.layout.has_focus(
                    window.screen.buttons[0]
                ),
            )
        plugin_file = self.root / "not-a-directory"
        plugin_file.write_text("original")
        self.rc.write_text(f"config.plugin_dir = {str(plugin_file)!r}\n")
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            window.screen.select_page(4)
            self.assertIsInstance(window.plugin_error, NotADirectoryError)
            window.reload()
            self.run_keys(
                window.screen,
                pipe,
                b"\r\t",
                until=lambda: window.screen.app.layout.has_focus(
                    window.screen.buttons[0]
                ),
            )
        self.assertEqual(plugin_file.read_text(), "original")

    def test_replaced_lists_are_released_and_other_pages_remember_focus(self):
        """Retire detached list windows without losing the last field on a live page."""
        self.plugin()
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            screen = window.screen
            layout = screen.app.layout
            with set_app(screen.app):
                layout.focus(window.inputs["cwd"].control)
                screen.select_page(4)
                for query in ("chosen", ""):
                    layout.focus(window.catalog_choices["plugin"].control)
                    old = weakref.ref(layout.current_window)
                    screen.open_filter()
                    screen.filter_input.text = query
                    screen.apply_filter()
                    gc.collect()
                    self.assertIsNone(old())
                    self.assertIn(layout.current_window, layout.find_all_windows())
                screen.select_page(0)
                screen.focus_body()
                self.assertIs(layout.current_control, window.inputs["cwd"].control)
                old_field = weakref.ref(layout.current_window)
                for replace in (window.cancel, window.reload):
                    screen.select_page(4)
                    layout.focus(window.catalog_choices["plugin"].control)
                    old = weakref.ref(layout.current_window)
                    replace()
                    gc.collect()
                    self.assertIsNone(old())
                    self.assertIn(layout.current_window, layout.find_all_windows())
                self.assertIsNone(old_field())

    def test_empty_catalog_can_be_reloaded_repeatedly(self):
        """An empty selection is a supported state during repeated catalog changes."""
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            window.reload()
            window.reload()
            self.assertEqual(window.catalog_choices, {"plugin": None, "module": None})
            self.run_keys(
                window.screen,
                pipe,
                b"\r\t",
                until=lambda: window.screen.app.layout.has_focus(
                    window.screen.buttons[0]
                ),
            )

    def test_finishing_filtered_directory_keeps_focus_on_a_visible_control(self):
        """Changing the only matching value leaves keyboard navigation on the page."""
        self.rc.write_text("config.plugin_dir = 'first-folder'\n")
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            screen = window.screen
            self.run_keys(
                screen,
                pipe,
                b"\t\t\r\x06first-folder\r\r\x01\x0bsecond-folder\r",
                until=lambda: (
                    window.inputs["plugin_dir"].text == "second-folder"
                    and screen.editing is None
                ),
            )
            self.assertIn(
                screen.app.layout.current_window, screen.app.layout.find_all_windows()
            )
            self.assertFalse(screen.app.layout.has_focus(window.inputs["plugin_dir"]))
            self.assertEqual(
                self.rc.read_text(), "config.plugin_dir = 'first-folder'\n"
            )

    def test_slow_catalog_allows_editing_coalesces_reads_and_retires_on_close(self):
        """Observe real PTK input and close while old or replacement reads are blocked."""
        original = "config.plugin_dir = 'slow'\n"
        self.rc.write_text(original)
        for folder in ("slow", "skipped", "latest", "closing"):
            package = self.home / folder / folder
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("PLUGIN_META = {'version': '1'}")

        async def drive():
            loop = asyncio.get_running_loop()
            calls = []
            gates = {name: threading.Event() for name in ("slow", "closing")}
            entered = {name: asyncio.Event() for name in gates}
            discover = PluginManager.iter_candidates
            read_meta = PluginManager._get_plugin_meta
            files_read = []

            def record_read(manager, path):
                files_read.append(path.parent.name)
                return read_meta(manager, path)

            def delayed(manager, **options):
                name = manager.script_dir.name
                calls.append(name)
                if name in gates:
                    loop.call_soon_threadsafe(entered[name].set)
                    if not gates[name].wait(15):
                        raise AssertionError("Test did not release catalog read")
                yield from discover(manager, **options)

            with (
                patch.object(PluginManager, "iter_candidates", delayed),
                patch.object(PluginManager, "_get_plugin_meta", record_read),
                create_pipe_input() as pipe,
            ):
                window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
                app = window.screen.app
                frame = asyncio.Event()
                app.after_render += lambda _: frame.set()
                task = asyncio.create_task(app.run_async())

                async def wait_for_frame(predicate):
                    async with asyncio.timeout(5):
                        while not predicate():
                            frame.clear()
                            await frame.wait()

                try:
                    await asyncio.wait_for(frame.wait(), 5)
                    await asyncio.wait_for(entered["slow"].wait(), 5)
                    pipe.send_bytes(b"\t\t\r")
                    pipe.send_bytes(b"\r\x01\x0bskipped\r\r\x01\x0blatest\r")
                    await wait_for_frame(
                        lambda: window.inputs["plugin_dir"].text == "latest"
                    )
                    self.assertEqual(calls, ["slow"])
                    self.assertEqual(self.rc.read_text(), original)
                    gates["slow"].set()
                    await wait_for_frame(
                        lambda: window.catalog_choices["plugin"] is not None
                    )
                    self.assertEqual(
                        [n for n, _ in window.catalog_choices["plugin"].values],
                        ["latest"],
                    )
                    self.assertEqual(calls, ["slow", "latest"])
                    self.assertEqual(files_read, ["latest"])
                    pipe.send_bytes(b"\r\x01\x0bclosing\r")
                    await asyncio.wait_for(entered["closing"].wait(), 5)
                    pipe.send_bytes(b"\x1b")
                    await wait_for_frame(
                        lambda: app.layout.current_control is window.screen.nav
                    )
                    pipe.send_bytes(b"\x1b")
                    await asyncio.wait_for(task, 3)
                    self.assertFalse(gates["closing"].is_set())
                    self.assertEqual(self.rc.read_text(), original)
                finally:
                    for gate in gates.values():
                        gate.set()
                    if not task.done():
                        app.exit()
                        await task

        asyncio.run(drive())

    def test_cancel_reuses_only_the_catalog_for_its_restored_directory(self):
        """Explicit refreshes read disk; an ordinary cancel retains known metadata."""
        directory = self.home / "plugin/script"
        other = self.home / "other"
        for path in (directory, other):
            path.mkdir(parents=True)
        with create_pipe_input() as pipe:
            window = SettingsWindow(self.store(), input=pipe, output=DummyOutput())
            module = directory / "integration"
            module.mkdir()
            (module / "__init__.py").write_text(
                "PLUGIN_META = {'category': 'module'}\n"
            )
            window.inputs["shell"].text = "draft"
            window.cancel()
            self.assertIsNone(window.catalog_choices["module"])
            window.reload()
            self.assertEqual(
                [name for name, _ in window.catalog_choices["module"].values],
                ["integration"],
            )
            self.run_keys(
                window.screen,
                pipe,
                b"\t\t\r\r\x01\x0bother\r",
                until=lambda: (
                    window.plugin_rows is not None
                    and window.catalog_choices["module"] is None
                ),
            )
            self.assertIsNone(window.catalog_choices["module"])
            window.cancel()
            self.assertEqual(
                [name for name, _ in window.catalog_choices["module"].values],
                ["integration"],
            )
            self.assertFalse(self.rc.exists())


class PublicManagementTests(ManagementFixture):
    """Use the public python -m ish command, actual controlling PTYs and final files."""

    def terminal(self, option, **options):
        """Start one public management command and always clean its child processes."""
        terminal = ManagementTerminal(self.root, option, **options)
        self.addCleanup(terminal.close)
        return terminal

    def click_label(self, terminal, label, *, same_line=""):
        """Click the first terminal cell of a label using the host's SGR mouse protocol."""

        def location():
            for y, line in enumerate(terminal.screen.display):
                if label in line and same_line in line:
                    return wcswidth(line.partition(label)[0]), y
            return None

        terminal.require(location)
        self.click_cell(terminal, *location())

    def field_bounds(self, terminal, title):
        """Locate a rendered field's title and its independently clickable value box."""
        frame = terminal.screen.display
        for heading, row in enumerate(frame):
            visible_title = row.partition("│")[2].rstrip(" △▽").lstrip()
            if visible_title not in (title, "✎ " + title, "\uf044 " + title):
                continue
            for y, candidate in enumerate(frame[heading + 1 :], heading + 1):
                sidebar, divider, main = candidate.partition("│")
                if not main.lstrip().startswith("│") or main.count("│") < 2:
                    continue
                left = wcswidth(sidebar + divider + main.partition("│")[0])
                right = wcswidth(candidate.rpartition("│")[0])
                return left, right, heading, y
        return None

    def click_field(self, terminal, title):
        """Click inside the value, after its border and one cell of inset."""
        terminal.require(lambda: self.field_bounds(terminal, title))
        left, _, _, y = self.field_bounds(terminal, title)
        self.click_cell(terminal, left + 2, y)

    def apply_action(self, terminal, label):
        """Activate the actual form button and require the form to remain open."""
        status = {
            "Save": "Changes saved.",
            "Cancel": "Draft discarded.",
        }[label]
        self.click_action(terminal, label)
        terminal.require(lambda: status in " ".join(self.status_rows(terminal)))
        self.assertFalse(terminal.exited())

    @staticmethod
    def status_rows(terminal, cancel="Cancel", save="Save"):
        """Read the status cells between the separator and the shortcut bar."""
        frame = terminal.screen.display
        main = frame[-3].partition("│")[2]
        last_line, button, end = main.rpartition(cancel)
        if not button or end.strip() != save:
            return []
        rows = [last_line.strip()]
        for row in reversed(frame[:-3]):
            content = row.partition("│")[2].strip()
            if content and set(content) == {"─"}:
                return rows[::-1]
            rows.append(content)
        return []

    def status_contains(self, terminal, message):
        """Compare status text across physical lines, including mid-word wrapping."""
        text = "".join(self.status_rows(terminal))
        return "".join(message.split()) in "".join(text.split())

    def click_action(self, terminal, label, *, cancel="Cancel", save="Save"):
        """Click the right-hand button even when the status repeats its name."""
        terminal.require(lambda: self.status_rows(terminal, cancel, save))
        row = terminal.screen.display[-3]
        self.click_cell(terminal, wcswidth(row.rpartition(label)[0]), terminal.rows - 3)

    @staticmethod
    def click_cell(terminal, x, y):
        """Send one press/release at a zero-based host cell."""
        terminal.send(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")

    def scrollbar_at_top(self, terminal):
        """Locate the right pane's arrows and inspect its blank cells and colors."""

        def bounds():
            for x in range(terminal.cols - 1, terminal.cols // 2, -1):
                column = [
                    terminal.screen.buffer[y][x].data for y in range(terminal.rows)
                ]
                if "△" in column and "▽" in column:
                    top, bottom = column.index("△"), column.index("▽")
                    if bottom > top + 2:
                        return x, top, bottom
            return None

        terminal.require(bounds)
        x, top, bottom = bounds()
        cells = [terminal.screen.buffer[y][x] for y in range(top + 1, bottom)]
        self.assertTrue(all(cell.data == " " for cell in cells))
        colors = [cell.bg for cell in cells]
        thumb, track = colors[0], colors[-1]
        self.assertNotEqual(thumb, track)
        self.assertEqual(
            colors, [thumb] * colors.count(thumb) + [track] * colors.count(track)
        )
        return x, top, bottom

    def test_help_is_categorized_readonly_and_does_not_execute_rc_or_plugins(self):
        """Help renders with an invalid engine path and executable user-code traps."""
        plugin_marker = self.plugin()
        rc_marker = self.root / "rc-executed"
        self.rc.write_text(
            f"open({str(rc_marker)!r}, 'w').write('executed')\nraise RuntimeError('must not run')\n"
        )
        original = self.rc.read_bytes()
        terminal = self.terminal("-h")
        terminal.require(lambda: "📖  Help" in terminal.screen.display[0], timeout=45)
        terminal.require(lambda: terminal.frame_has("▸ Getting started"))
        y, row = next(
            (y, row)
            for y, row in enumerate(terminal.screen.display)
            if "▸ Getting started" in row
        )
        x = row.index("▸")
        selected = terminal.screen.buffer[y][x].fg
        self.assertEqual(terminal.screen.buffer[y][x].bg, "default")
        self.assertNotIn("C Close", terminal.screen.display[-1])
        terminal.send(b"\r")
        terminal.require(lambda: terminal.screen.buffer[y][x].fg != selected)
        self.assertEqual(terminal.screen.buffer[y][x].bg, "default")
        terminal.send(b"\x03" + terminal.escape_key)
        terminal.require(lambda: terminal.screen.buffer[y][x].fg == selected)
        self.assertEqual(terminal.screen.buffer[y][x].bg, "default")
        terminal.send(b"\x03\x1b[B\x1b[B")
        terminal.require(lambda: terminal.frame_has(".ishrc.py"))
        terminal.finish()
        self.assertEqual(self.rc.read_bytes(), original)
        self.assertFalse(rc_marker.exists())
        self.assertFalse(plugin_marker.exists())
        self.assertFalse((self.home / ".ish.log").exists())

    def test_switching_management_views_keeps_the_alternate_screen(self):
        """Observe the outer terminal protocol in both launch orders and on final exit."""
        for first in ("-h", "-c"):
            with self.subTest(first=first):
                term = self.terminal(first, rows=38, cols=116)
                help_active = first == "-h"
                for _ in range(4):
                    button = "Config" if help_active else "Help"
                    self.click_label(term, button, same_line="Preset")
                    help_active = not help_active
                    title = "Help" if help_active else "Settings"
                    term.require(
                        lambda t=term, title=title: title in t.screen.display[0]
                    )
                    self.assertEqual(term.output.count(b"\x1b[?1049h"), 1)
                    self.assertNotIn(b"\x1b[?1049l", term.output)
                term.close_settings()
                self.assertEqual(term.output.count(b"\x1b[?1049l"), 1)

    def test_configured_editor_and_browser_receive_separate_arguments(self):
        """Save both fields, open the editor, then open a link from both help paths."""
        recorder = self.root / "record arguments.py"
        recorder.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
        )
        env = {
            "VISUAL": "/missing/editor",
            "EDITOR": "/missing/editor",
            "BROWSER": "/missing/browser",
        }
        term = self.terminal("-c", rows=38, cols=116, environment=env)
        term.require(lambda: term.frame_has("Shell executable"))
        term.send(b"\r\x1b[1;3F")
        markers = {}
        for field in ("Default editor", "Web browser"):
            marker = self.root / field
            markers[field] = marker
            self.click_field(term, field)
            term.require(lambda title=field: term.frame_has("✎ " + title))
            command = shlex.join(
                [sys.executable, str(recorder), str(marker), "two words", "semi;colon"]
            )
            term.send("\x1b[200~" + command + "\x1b[201~\r")
            term.require(lambda title=field: not term.frame_has("✎ " + title))
        self.apply_action(term, "Save")
        term.send(b"\x05")
        term.require(
            lambda: (
                term.frame_has("reloaded from disk")
                and markers["Default editor"].exists()
            )
        )
        self.assertEqual(
            json.loads(markers["Default editor"].read_text()),
            ["two words", "semi;colon", str(self.rc)],
        )
        self.click_label(term, "Help", same_line="Preset")

        def open_link(ui):
            marker = markers["Web browser"]
            self.click_label(ui, "More details")
            expected = ["two words", "semi;colon", "https://github.com/E1der0wL/ish"]
            ui.require(
                lambda: marker.is_file() and marker.read_text() == json.dumps(expected)
            )
            marker.unlink()
            ui.close_settings()

        open_link(term)
        trap = self.root / "rc-evaluated"
        self.rc.write_text(f"open({str(trap)!r}, 'w').close()\n" + self.rc.read_text())
        open_link(self.terminal("-h", rows=38, cols=116, environment=env))
        self.assertFalse(trap.exists())

    def test_closing_help_leaves_the_opened_browser_running(self):
        """The link handoff has no claim on the terminal after the user closes help."""
        marker = self.root / "browser-started"
        release = self.root / "browser-may-exit"
        done = self.root / "browser-exited"
        script = self.root / "browser.py"
        script.write_text(
            "import os, time\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(str(os.isatty(0)))\n"
            "deadline = time.monotonic() + 20\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
            f"Path({str(done)!r}).touch()\n"
        )
        self.rc.write_text(
            f"config.browser = {shlex.join([sys.executable, str(script)])!r}\n"
        )
        term = self.terminal("-h")
        try:
            self.click_label(term, "More details")
            term.require(marker.exists)
            self.assertEqual(marker.read_text(), "False")
            term.close_settings()
            self.assertFalse(done.exists())
        finally:
            release.touch()
        deadline = time.monotonic() + 5
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(done.exists())

    def test_help_details_opens_project_and_preserves_help(self):
        """Capture the browser handoff and leave the same help window usable."""
        marker = self.root / "browser-url"
        browser = self.root / "browser"
        browser.write_text(
            f"#!/bin/sh\nprintf '%s' \"$1\" > {shlex.quote(str(marker))}\n"
        )
        browser.chmod(0o700)
        terminal = self.terminal(
            "-h",
            language="ko",
            environment={"BROWSER": str(browser), "LOGNAME": "guest"},
        )
        self.click_label(terminal, "자세히")
        terminal.require(
            lambda: (
                marker.is_file()
                and marker.read_text() == "https://github.com/E1der0wL/ish"
            )
        )
        frame = terminal.screen.display
        self.assertEqual(frame[0].split(), ["📖", "도움말", "×"])
        profile = next(row.partition("│")[0] for row in frame if "ish 2.0.0" in row)
        self.assertEqual(profile.split(), ["☺", "guest", "ish", "2.0.0"])
        self.assertTrue(
            profile.startswith("  ☺  guest") and profile.endswith("2.0.0  ")
        )
        self.assertIn("<F>", frame[-1])
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_sidebar_rules_follow_titles_and_resize_with_the_panel(self):
        """Read category rules from CLI output and click items under them."""
        cases = (
            ("-c", ("ish 설정", "설치된 확장"), "환경 설정", "UI 팔레트"),
            ("-h", ("사용 안내", "참고 자료"), None, "진단·로그"),
        )

        def check(option, sections, subgroup, page):
            terminal = self.terminal(option, rows=36, cols=110, language="ko")

            def rule(title):
                lines = [line.partition("│")[0] for line in terminal.screen.display]
                return next(
                    (
                        lines[i + 1]
                        for i, line in enumerate(lines[:-1])
                        if line.strip() == title
                    ),
                    "",
                )

            terminal.require(lambda: all(rule(h).strip() for h in sections))
            for heading in sections:
                self.assertEqual(rule(heading), " " + "─" * 26 + " ")
            lines = [line.partition("│")[0].strip() for line in terminal.screen.display]
            self.assertEqual(sum(set(line) == {"─"} for line in lines), len(sections))
            if subgroup:
                self.assertIn("언어·시작 설정", rule(subgroup))
                self.assertTrue(
                    any(
                        row.startswith("    공개된 확장 설정 없음")
                        for row in terminal.screen.display
                    )
                )
            terminal.send(b"\x1b[1;5C")  # Ctrl+Right widens the sidebar by one cell.
            terminal.require(lambda: rule(sections[0]) == " " + "─" * 27 + " ")
            self.assertEqual(rule(sections[1]), rule(sections[0]))
            self.click_label(terminal, page)
            terminal.require(
                lambda: page in terminal.screen.display[2].partition("│")[2]
            )
            terminal.close_settings()

        for case in cases:
            with self.subTest(option=case[0]):
                check(*case)

    def test_sidebar_height_drag_uses_comment_colors_and_keeps_focus(self):
        """Drag both real category handles, including a resize before release."""
        self.rc.write_text(
            "config.ui_palette = {'text': '#22ff44', 'muted': '#ff0000', 'muted2': '#0000ff', 'edge': '#ffff00'}\n"
        )

        def check(option, title):
            term = self.terminal(option, rows=36, cols=110)

            def rule():
                return next(
                    (
                        y + 1
                        for y, line in enumerate(term.screen.display)
                        if line.partition("│")[0].strip() == title
                    ),
                    None,
                )

            def point(code, y, suffix="M"):
                term.send(f"\x1b[<{code};6;{y + 1}{suffix}")

            term.require(
                lambda: rule() and term.screen.buffer[rule()][5].fg == "0000ff"
            )
            y = rule()
            point(35, y - 1)
            term.require(lambda: term.screen.buffer[y][5].fg == "ff0000")
            point(0, y - 1)
            point(32, y + 3)
            point(0, y + 3, "m")
            term.require(lambda: rule() == y + 4)
            # Capture a drag at the rule and resize before releasing over the close button.
            point(0, rule())
            term.resize(24, 80)
            term.require(lambda: "×" in term.screen.display[0][70:])
            term.send(b"\x1b[<0;79;1m\x1b[B")
            expected = "UI palette" if option == "-c" else "Keys and navigation"
            term.require(lambda: expected in term.screen.display[2].partition("│")[2])
            self.assertIsNotNone(rule())
            term.close_settings()

        for option, title in (("-h", "Reference"), ("-c", "Installed extensions")):
            with self.subTest(option=option):
                check(option, title)

    def test_color_role_columns_and_button_padding_on_screen(self):
        """Measure Korean captions, both button margins and the rendered color strip."""
        self.rc.write_text("config.ui_icons = 'nerdfont'\n")
        term = self.terminal(
            "-c", language="ko", environment={"LOGNAME": "dev"}, rows=38, cols=119
        )
        term.require(lambda: term.frame_has("\uedca  dev"))
        self.click_label(term, "UI 팔레트")
        term.send(b"\r")
        term.require(lambda: term.frame_has("lime-300"))

        def cell_x(label, same):
            line = next(
                row for row in term.screen.display if label in row and same in row
            )
            return wcswidth(line.partition(label)[0])

        term.require(
            lambda: any("100" in row and "900" in row for row in term.screen.display)
        )
        right = cell_x("900", "100") + 3
        token = cell_x("lime-300", "강조색 1")
        self.assertEqual(token, cell_x("cyan-300", "강조색 2"))
        self.assertGreaterEqual(token + len("lime-300"), right - 5)
        self.assertLessEqual(token + len("lime-300"), right + 1)
        y = term.rows - 3
        for caption in ("취소", "저장"):
            x = cell_x(caption, "저장")
            cells = term.screen.buffer[y]
            bg = cells[x].bg
            width = wcswidth(caption)
            self.assertNotEqual(cells[x - 3].bg, bg)
            self.assertNotEqual(cells[x + width + 2].bg, bg)
            self.assertTrue(all(cells[i].bg == bg for i in range(x - 2, x + width + 2)))
        term.close_settings()

    def test_clicking_toolbar_returns_keyboard_to_the_original_field(self):
        """Close a clicked filter and type into the field selected before the click."""
        term = self.terminal("-c", rows=36, cols=110)
        self.click_field(term, "Shell executable")
        term.send(b"\r")
        term.require(lambda: not term.frame_has("✎ Shell executable"))
        self.click_label(term, "⚲", same_line="Language and startup")
        term.require(lambda: term.frame_has("Apply"))
        term.send(b"\x1b")
        term.require(lambda: not term.frame_has("Apply"))
        self.assertNotIn("Filter options", term.screen.display[2])
        term.send(b"\r")
        term.require(lambda: term.frame_has("✎ Shell executable"))
        term.send(b"\x01\x0bclicked-shell\r\x13")
        term.require(
            lambda: self.rc.exists() and load(self.rc).shell == "clicked-shell"
        )
        # A pointer click must also leave the toolbar if a different tool was
        # selected deliberately using the keyboard before hovering over Filter.
        term.send(b"\t\t\x1b[C")
        term.require(lambda: "Reload settings" in term.screen.display[2])
        x = wcswidth(term.screen.display[2].partition("⚲")[0])
        term.send(f"\x1b[<35;{x + 1};3M")
        term.require(lambda: "Filter options" in term.screen.display[2])
        self.click_cell(term, x, 2)
        term.require(lambda: term.frame_has("Apply"))
        term.send(b"\x1b")
        term.require(lambda: not term.frame_has("Apply"))
        self.assertNotIn("Reload settings", term.screen.display[2])
        self.assertNotIn("Filter options", term.screen.display[2])
        term.close_settings()

    def test_profile_keeps_actions_and_focus_when_sidebar_narrows(self):
        """Read the same account and activate the same preset control after a resize."""
        terminal = self.terminal(
            "-c", language="ko", rows=32, cols=110, environment={"LOGNAME": "ui-tester"}
        )

        def footer():
            rows = [line.partition("│")[0].strip() for line in terminal.screen.display]
            for y, row in enumerate(rows):
                if row.split()[:2] == ["☺", "ui-tester"]:
                    return rows[y : y + 4]
            return []

        terminal.require(lambda: len(footer()) == 4 and "프리셋 ▾" in footer()[2])
        heading, gap, buttons, bottom = footer()
        self.assertEqual(heading.split(), ["☺", "ui-tester", "ish", "2.0.0"])
        self.assertEqual((gap, bottom), ("", ""))
        self.assertEqual(buttons.split(), ["도움말", "프리셋", "▾"])
        self.click_label(terminal, "ui-tester")
        terminal.require(lambda: terminal.frame_has("로컬 사용자 계정"))
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("로컬 사용자 계정"))
        terminal.resize(32, 52)
        terminal.require(lambda: terminal.screen.display[4].index("│") == 4)
        self.click_label(terminal, "▤")
        terminal.require(
            lambda: any(
                row.strip() == "+" or "   +" in row
                for row in terminal.screen.display[:-1]
            )
        )
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("새 프리셋 이름"))
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("새 프리셋 이름"))
        terminal.resize(32, 110)
        terminal.require(lambda: len(footer()) == 4 and "도움말" in footer()[2])
        self.click_label(terminal, "도움말", same_line="프리셋")
        terminal.require(lambda: terminal.frame_has("자세히"))
        terminal.close_settings()

    def test_blank_clicks_focus_the_clicked_pane_and_preserve_the_page(self):
        """Click margins, indentation, inter-item gaps and the space below the list."""
        for option, field, selected in (
            ("-c", "Language and startup", "UI palette"),
            ("-h", "Getting started", "Keys and navigation"),
        ):
            with self.subTest(option=option):
                term = self.terminal(option, rows=42, cols=112)
                self.click_label(term, selected)
                term.require(
                    lambda t=term, title=selected: (
                        title in t.screen.display[2].partition("│")[2]
                    )
                )
                frame = term.screen.display
                y = next(
                    y for y, row in enumerate(frame) if field in row.partition("│")[0]
                )
                edge = frame[y].index("│")
                empty = next(
                    i
                    for i, row in enumerate(frame[y + 1 :], y + 1)
                    if "│" in row and not row.partition("│")[0].strip()
                )
                for x, row in ((0, y), (2, y), (edge - 2, y), (1, empty)):
                    self.click_cell(term, x, row)
                # A Ctrl+Right frame is an ordered acknowledgement of those clicks.
                term.send(b"\x1b[1;5C")
                term.require(
                    lambda t=term, e=edge: t.screen.display[2].index("│") == e + 1
                )
                self.assertIn(selected, term.screen.display[2].partition("│")[2])
                self.click_label(term, field)
                term.require(
                    lambda t=term, title=field: (
                        title in t.screen.display[2].partition("│")[2]
                    )
                )
                if option == "-c":
                    self.click_field(term, "Shell executable")
                    term.send(b"\x01\x0bdraft-shell")
                    term.require(partial(term.frame_has, "draft-shell"))
                else:
                    term.send(b"\r")
                self.click_cell(term, 0, y)
                # Only sidebar navigation handles Ctrl+Right. The blank click also
                # finishes a text edit without selecting another page or saving it.
                term.send(b"\x1b[1;5C")
                term.require(
                    lambda t=term, e=edge: t.screen.display[2].index("│") == e + 2
                )
                self.assertIn(field, term.screen.display[2].partition("│")[2])
                term.require(lambda t=term: not t.frame_has("✎ Shell executable"))
                self.click_cell(term, term.cols - 1, y)
                if option == "-c":
                    term.send(b"\r")
                    term.require(partial(term.frame_has, "✎ Shell executable"))
                    self.assertTrue(term.frame_has("draft-shell"))
                else:
                    # The first Esc from the document goes back to navigation.
                    term.send(b"\x1b")
                    term.require(
                        lambda t=term: (
                            self.label_cell(t, "Getting started", "▸").fg
                            != self.label_cell(
                                t, "Keys and navigation", "Keys and navigation"
                            ).fg
                        )
                    )
                    self.assertIsNone(term.status)
                term.close_settings()
                self.assertFalse(self.rc.exists())

    def test_blank_press_across_divider_routes_keys_before_mouse_release(self):
        """A divider hover or a held motion cannot postpone the next key to a later click."""
        term = self.terminal("-c", rows=38, cols=112)
        self.click_field(term, "Shell executable")
        term.send(b"\r")
        term.require(lambda: not term.frame_has("✎ Shell executable"))
        edge = term.screen.display[2].index("│")
        y = 10

        def mouse(code, x, end="M"):
            return f"\x1b[<{code};{x + 1};{y}{end}"

        def cross(x, direction):
            return "".join(mouse(35, col) for col in (x - direction, x, x + direction))

        # The first keyboard event follows the press in the same PTY write.
        # Only navigation owns Ctrl+Right; it grows the sidebar by one cell.
        term.send(cross(edge, -1) + mouse(0, edge - 1) + "\x1b[1;5C")
        term.require(lambda: term.screen.display[2].index("│") == edge + 1)
        term.send(mouse(0, edge - 1, "m"))
        edge += 1
        # Switch back. A move report while held, even in the same cell, used
        # to cancel the click. Enter must reach the remembered body field.
        term.send(
            cross(edge, 1)
            + mouse(0, edge + 1)
            + mouse(32, edge + 1)
            + mouse(0, edge + 1, "m")
            + "\r"
        )
        term.require(lambda: term.frame_has("✎ Shell executable"))
        term.close_settings()

    def test_pane_background_focus_preserves_scroll_and_requires_a_left_click(self):
        """Retain a manual scroll; a drag release and a modal cannot switch panes."""
        self.rc.write_text("config.ui_palette = {'accent': '#eb1647'}\n")
        term = self.terminal(
            "-c",
            rows=24,
            cols=106,
            environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
        )

        def main_active():
            cell = self.label_cell(term, "Language and startup", "⚲")
            return cell is not None and cell.fg == "eb1647"

        self.click_field(term, "Shell executable")
        term.send(b"\r\x1b[1;3F")  # Finish the edit, then scroll the form to the end.
        term.require(lambda: main_active() and self.field_bounds(term, "Log level"))
        bounds = self.field_bounds(term, "Log level")
        # Press in the main margin and release in the sidebar margin.
        term.send(f"\x1b[<0;{term.cols};6M\x1b[<32;1;6M\x1b[<0;1;6m")
        term.send(b"\x1b[<2;1;6M\x1b[<2;1;6m")  # Right-click the sidebar.
        # Ctrl+Right would resize the rail if either gesture had taken focus.
        width = term.screen.display[2].index("│")
        term.send(b"\x1b[1;5C")
        self.click_cell(term, 0, 5)
        term.require(lambda: not main_active())
        self.assertEqual(term.screen.display[2].index("│"), width)
        self.click_cell(term, term.cols - 1, 5)
        term.require(main_active)
        self.assertEqual(self.field_bounds(term, "Log level"), bounds)

        term.send(b"\x06")
        term.require(
            lambda: any(
                "Apply" in row and "Cancel" in row for row in term.screen.display
            )
        )
        self.click_cell(term, 0, 5)
        term.send(b"escape_delay\r")
        term.require(lambda: term.frame_has("Filter: escape_delay"))
        term.close_settings()

    def test_blank_space_below_installed_extension_does_not_open_it(self):
        """Exercise PTK's last-line fallback in an oversized installed-extension list."""
        root = self.home / "plugin/script/sample"
        root.mkdir(parents=True)
        (root / "__init__.py").write_text(
            "PLUGIN_META = {'version': '1', 'category': 'module', 'settings': "
            "{'caption': {'title': 'Caption', 'description': 'Visible text', 'default': 'abc'}}}"
        )
        term = self.terminal("-c", rows=45, cols=112)
        term.require(lambda: term.frame_has("sample"))
        self.click_label(term, "UI palette")
        term.require(lambda: "UI palette" in term.screen.display[2])
        frame = term.screen.display
        y = next(i for i, row in enumerate(frame) if "sample" in row.partition("│")[0])
        edge = frame[y].index("│")
        self.assertFalse(frame[y + 3].partition("│")[0].strip())
        self.click_cell(term, 5, y + 3)
        self.click_cell(term, edge - 2, y)
        term.send(b"\x1b[1;5C")
        term.require(lambda: term.screen.display[2].index("│") == edge + 1)
        self.assertIn("UI palette", term.screen.display[2])
        self.click_label(term, "sample")
        term.require(lambda: term.frame_has("Caption"))
        term.close_settings()

    def test_hover_in_other_pane_previews_only_the_pointed_widget(self):
        """Assert actual cell colors and immediately send keys to the original pane."""
        self.rc.write_text(
            "config.ui_palette = {'accent': '#c62a71', 'selection': '#213145', 'background': '#102530'}\n"
        )
        term = self.terminal(
            "-c",
            rows=40,
            cols=120,
            environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
        )
        term.require(lambda: self.field_bounds(term, "Language"))

        def hover(x, y):
            term.send(f"\x1b[<35;{x + 1};{y + 1}M")

        def hover_label(label, same):
            term.require(lambda: self.label_cell(term, label, same) is not None)
            y, row = next(
                (y, row)
                for y, row in enumerate(term.screen.display)
                if label in row and same in row
            )
            hover(wcswidth(row.partition(label)[0]), y)

        def color(label, row):
            cell = self.label_cell(term, label, row)
            return cell.fg if cell is not None else None

        edge = term.screen.display[2].index("│")
        for label, same in (("Save", "Cancel"), ("⚲", "Language and startup")):
            hover_label(label, same)
            term.require(lambda label=label, same=same: color(label, same) == "c62a71")
            self.assertNotEqual(color("Language and startup", "⚲"), "c62a71")
        left, _, title, editor = self.field_bounds(term, "Language")
        hover(left + 3, editor)
        term.require(lambda: term.screen.buffer[title][left].fg == "c62a71")
        term.send(b"\x1b[1;5C")
        term.require(lambda: term.screen.display[2].index("│") == edge + 1)
        self.click_field(term, "Shell executable")
        hover_label("UI palette", "UI palette")
        term.require(lambda: color("UI palette", "UI palette") == "c62a71")
        term.send(b"\x01\x0bhover-kept-the-editor")
        term.require(lambda: term.frame_has("hover-kept-the-editor"))
        self.assertTrue(term.frame_has("✎ Shell executable"))
        label = self.label_cell(term, "UI palette", "UI palette")
        row = next(
            i
            for i, line in enumerate(term.screen.display)
            if "UI palette" in line.partition("│")[0]
        )
        self.assertEqual(label.bg, term.screen.buffer[row][0].bg)
        hover(0, 3)
        term.require(lambda: color("UI palette", "UI palette") != "c62a71")
        term.close_settings()

    def test_footer_accent_edits_independently_and_reopens_in_help(self):
        """Persist a chip color without modifying a previously configured comment color."""
        self.rc.write_text("config.ui_palette = {'muted2': '#7094ab'}\n")
        options = {
            "environment": {"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
            "rows": 42,
            "cols": 120,
        }
        term = self.terminal("-c", **options)
        self.click_label(term, "UI palette")
        term.require(lambda: self.label_cell(term, "Comment 2", "#7094ab") is not None)
        self.click_label(term, "Accent 4", same_line="slate-500")
        term.require(
            lambda: term.frame_has("› Accent 4") and term.frame_has("◂ Slate ▸")
        )
        self.click_label(term, "Teal")
        term.require(lambda: term.frame_has("teal-500") and term.frame_has("◂ Teal ▸"))
        self.click_label(term, "200", same_line="900")
        term.require(
            lambda: (
                (cell := self.label_cell(term, "Save", "<S>")) and cell.bg == "99f6e4"
            )
        )
        self.apply_action(term, "Save")
        term.close_settings()
        self.assertEqual(
            load(self.rc).ui_palette, {"muted2": "#7094ab", "accent4": "teal-200"}
        )
        help_ui = self.terminal("-h", **options)
        help_ui.require(
            lambda: (
                (cell := self.label_cell(help_ui, "Find", "<F>"))
                and cell.bg == "99f6e4"
            )
        )
        help_ui.close_settings()

    def test_toolbar_hover_and_activation_leave_keyboard_on_the_field(self):
        """A tooltip can follow the pointer without redirecting Enter to the toolbar."""
        term = self.terminal("-c", rows=38, cols=112)
        self.click_field(term, "Shell executable")
        term.send(b"\r")
        term.require(lambda: not term.frame_has("✎ Shell executable"))
        x = wcswidth(term.screen.display[2].partition("⚲")[0])
        self.click_cell(term, x - 1, 2)
        self.click_cell(term, x + 1, 2)
        term.send(f"\x1b[<35;{x + 1};3M")
        term.require(lambda: "Filter options" in term.screen.display[2])
        term.send(b"\r")
        term.require(lambda: term.frame_has("✎ Shell executable"))
        term.send(b"\r\t\t\r")
        term.require(lambda: term.frame_has("Apply"))
        term.send(b"\x1b")
        term.require(lambda: not term.frame_has("Apply"))
        self.assertNotIn("Filter options", term.screen.display[2])
        term.send(b"\r")
        term.require(lambda: term.frame_has("✎ Shell executable"))
        term.close_settings()

    def test_cross_button_releases_do_not_save_reload_or_close(self):
        """Press on the field, another button, or another cell of the Save button."""
        original = "config.shell = 'initial-shell'\n"
        self.rc.write_text(original)
        term = self.terminal("-c", rows=38, cols=116)
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0bdraft-shell\r")
        term.require(
            lambda: (
                term.frame_has("draft-shell")
                and not term.frame_has("✎ Shell executable")
            )
        )
        left, _, _, field_y = self.field_bounds(term, "Shell executable")

        def point(label, row):
            return wcswidth(term.screen.display[row].rpartition(label)[0]), row

        def gesture(start, end):
            x, y = start
            a, b = end
            term.send(
                f"\x1b[<0;{x + 1};{y + 1}M\x1b[<32;{a + 1};{b + 1}M\x1b[<0;{a + 1};{b + 1}m"
            )

        field = (left + 2, field_y)
        save = point("Save", term.rows - 3)
        cancel = point("Cancel", term.rows - 3)
        for end in (save, point("↻", 2), point("×", 0)):
            gesture(field, end)
        gesture(cancel, save)
        gesture(save, cancel)
        # Wait for a subsequent action before checking that earlier releases had no effect.
        term.send(b"\x06")
        term.require(lambda: term.frame_has("Apply"))
        term.send(term.escape_key)
        term.require(
            lambda: not term.frame_has("Apply") and term.frame_has("draft-shell")
        )
        self.assertEqual(self.rc.read_text(), original)
        gesture(save, (save[0] + 1, save[1]))
        term.require(lambda: term.frame_has("Changes saved"))
        self.assertEqual(load(self.rc).shell, "draft-shell")
        term.close_settings()

    def test_tool_keys_and_footer_cancel_the_same_edit_as_the_toolbar(self):
        """Filter, edit and reload each use one action for all three activation paths."""
        log = self.root / "editor-calls"
        script = self.root / "editor.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"log = Path({str(log)!r})\n"
            "with log.open('a') as f: print(sys.argv[1], file=f)\n"
            "n = len(log.read_text().splitlines())\n"
            "with Path(sys.argv[1]).open('a') as rc: print(f\"config.shell = 'editor-{n}'\", file=rc)\n"
        )
        editor = shlex.join([sys.executable, str(script)])
        disk = "disk-shell"
        self.rc.write_text(f"config.editor = {editor!r}\nconfig.shell = {disk!r}\n")
        term = self.terminal("-c", rows=38, cols=118)
        calls = 0
        for route in ("toolbar", "key", "footer"):
            for key, glyph, code in (
                ("F", "⚲", b"\x06"),
                ("E", "✎", b"\x05"),
                ("R", "↻", b"\x12"),
            ):
                with self.subTest(route=route, key=key):
                    self.click_field(term, "Shell executable")
                    term.require(lambda: term.frame_has("✎ Shell executable"))
                    term.send(b"\x01\x0bdiscard-me")
                    term.require(lambda: term.frame_has("discard-me"))
                    if key == "R":
                        disk = f"reload-{route}"
                        self.rc.write_text(
                            f"config.editor = {editor!r}\nconfig.shell = {disk!r}\n"
                        )
                    if route == "key":
                        term.send(code)
                    elif route == "toolbar":
                        self.click_label(term, glyph, same_line="Language and startup")
                    else:
                        self.click_label(term, f"<{key}>", same_line="<S>")
                    if key == "F":
                        term.require(lambda: term.frame_has("Apply"))
                        term.send(term.escape_key)
                    elif key == "E":
                        calls += 1
                        disk = f"editor-{calls}"
                    term.require(
                        lambda disk=disk: (
                            term.frame_has(disk)
                            and not term.frame_has("discard-me")
                            and not term.frame_has("Apply")
                            and not term.frame_has("✎ Shell executable")
                        )
                    )
        self.assertEqual(log.read_text().splitlines(), [str(self.rc)] * calls)
        term.close_settings()

    def test_extension_integers_have_the_numeric_field_controls(self):
        """Step a negative integer and a large exact integer and persist both operations."""
        source = self.home / "plugin/script/sample/__init__.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "PLUGIN_META = {'version':'1','category':'module','settings':"
            "{'count':{'title':'Quantity','description':'Item count','default':-7}}}\n"
        )
        term = self.terminal("-c", rows=38, cols=118)
        self.click_label(term, "sample")
        self.click_field(term, "Quantity")
        _, right, _, y = self.field_bounds(term, "Quantity")
        term.require(lambda: term.screen.buffer[y][right - 2].data == "↕")
        term.send(b"\x1b[B\r\x13")
        term.require(
            lambda: (
                self.rc.exists()
                and load(self.rc).plugin_settings.get("sample", {}).get("count") == -8
            )
        )
        self.click_field(term, "Quantity")
        term.send(b"\x01\x0b99999999999999999999999999999\x1b[A\r\x13")
        term.require(
            lambda: (
                load(self.rc).plugin_settings["sample"]["count"]
                == 100000000000000000000000000000
            )
        )
        term.close_settings()

    @staticmethod
    def at_reference_end(term):
        """Read the final CLI example even when its argument wraps across rows."""
        body = "".join(
            row.partition("│")[2].strip(" ▽△") for row in term.screen.display[4:-4]
        )
        return "--shell-arg=ARG." in body and not term.frame_has("usage: ish")

    def test_first_config_visit_translates_the_open_help_page_in_place(self):
        """Loading the rc language for settings preserves the position in translated help."""
        self.rc.write_text("config.lang = 'ko'\n")
        term = self.terminal("-h", language=None, rows=28, cols=116)
        term.require(lambda: "Help" in term.screen.display[0])
        term.send(b"\x06Command-line options\r")
        term.require(lambda: term.frame_has("usage: ish"))
        term.send(b"\x1b[1;3F")
        term.require(lambda: self.at_reference_end(term))
        self.click_label(term, "Config", same_line="Preset")
        term.require(lambda: "설정" in term.screen.display[0])
        self.click_label(term, "도움말", same_line="프리셋")
        term.require(
            lambda: (
                "명령줄 옵션" in term.screen.display[2]
                and "도움말" in term.screen.display[0]
            )
        )
        self.assertFalse(term.frame_has("사용법: ish"))
        self.click_label(term, "×")
        term.finish(key=b"")

    def test_help_retains_the_selected_page_and_reading_position(self):
        """Resume the CLI reference at its final example after visiting settings."""
        term = self.terminal("-h", rows=28, cols=116)
        term.require(lambda: term.frame_has("Getting started"))
        term.send(b"\x06Command-line options\r")
        term.require(lambda: term.frame_has("usage: ish"))
        term.send(b"\x1b[1;3F")

        term.require(lambda: self.at_reference_end(term))
        self.click_label(term, "Config", same_line="Preset")
        term.require(lambda: "Settings" in term.screen.display[0])
        self.click_label(term, "Help", same_line="Preset")
        term.require(
            lambda: (
                "Command-line options" in term.screen.display[2]
                and self.at_reference_end(term)
            )
        )
        term.close_settings()

    def test_toolbar_cancels_only_the_open_field_before_filtering(self):
        """Keep the earlier draft and discard only the current value on a tool click."""
        self.rc.write_text("config.shell = 'disk-shell'\n")
        term = self.terminal("-c", rows=38, cols=116)
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0baccepted-shell\r")
        term.require(
            lambda: (
                term.frame_has("accepted-shell")
                and not term.frame_has("✎ Shell executable")
            )
        )
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0bdiscard-this-edit")
        term.require(lambda: term.frame_has("discard-this-edit"))
        self.click_label(term, "⚲", same_line="Language and startup")
        term.require(lambda: term.frame_has("Apply"))
        term.send(b"\x1b")
        term.require(
            lambda: (
                term.frame_has("accepted-shell")
                and not term.frame_has("discard-this-edit")
            )
        )

        self.click_field(term, "Language")
        term.require(lambda: term.frame_has("○ English"))
        x = wcswidth(term.screen.display[2].partition("⚲")[0])
        term.send(f"\x1b[<35;{x + 1};3M")
        term.require(lambda: "Filter options" in term.screen.display[2])
        term.send(b"\x1b[B")
        term.require(lambda: self.label_cell(term, "○ English", "○ English").bold)
        self.click_cell(term, x, 2)
        term.require(lambda: term.frame_has("Apply"))
        term.send(b"\x1b")
        term.require(
            lambda: not term.frame_has("Apply") and not term.frame_has("○ English")
        )
        self.apply_action(term, "Save")
        self.assertEqual(load(self.rc).shell, "accepted-shell")
        self.assertIsNone(load(self.rc).lang)
        term.close_settings()

    def test_toolbar_cancels_before_opening_file_and_reloading(self):
        """Open the configured editor on one click, then reload both disk and form."""
        recorder = self.root / "editor.py"
        log = self.root / "opened"
        recorder.write_text(
            "from pathlib import Path\nimport sys\n"
            f"with Path({str(log)!r}).open('a') as log: log.write(sys.argv[1] + '\\n')\n"
        )
        command = shlex.join([sys.executable, str(recorder)])
        self.rc.write_text(
            f"config.editor = {command!r}\nconfig.shell = 'original-shell'\n"
        )
        term = self.terminal("-c", rows=38, cols=116)
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0btemporary-shell")
        term.require(lambda: term.frame_has("temporary-shell"))
        self.click_label(term, "✎", same_line="Language and startup")
        term.require(lambda: log.exists() and term.frame_has("reloaded from disk"))
        self.assertEqual(log.read_text().splitlines(), [str(self.rc)])
        self.assertTrue(term.frame_has("original-shell"))

        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0bearlier-draft\r")
        term.require(
            lambda: (
                term.frame_has("earlier-draft")
                and not term.frame_has("✎ Shell executable")
            )
        )
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0bsecond-edit")
        term.require(lambda: term.frame_has("second-edit"))
        self.rc.write_text("config.shell = 'reloaded-shell'\n")
        self.click_label(term, "↻", same_line="Language and startup")
        term.require(
            lambda: (
                term.frame_has("reloaded-shell")
                and term.frame_has("reloaded from disk")
            )
        )
        term.close_settings()

    def test_icon_sidebar_hover_card_and_keyboard_expansion(self):
        """Collapse past the profile width, read a different icon and reopen the panel."""
        term = self.terminal("-c", rows=34, cols=105)
        term.require(lambda: term.frame_has("Language and startup"))
        term.send(b"\x1b[1;5D" * 10)
        term.require(
            lambda: (
                term.screen.display[4].index("│") == 4
                and any(
                    row.partition("│")[0].strip() == "▤" for row in term.screen.display
                )
            )
        )
        y = next(
            i
            for i, line in enumerate(term.screen.display)
            if "◐" in line.partition("│")[0]
        )
        sidebar = [line.partition("│")[0].strip() for line in term.screen.display[1:-1]]
        items = [i for i, glyph in enumerate(sidebar) if glyph and set(glyph) != {"─"}]
        self.assertTrue(all(not sidebar[i + 1] for i in items[:-1]))
        self.assertEqual([sidebar[i] for i in items[-3:]], ["☺", "📖", "▤"])
        self.click_cell(term, 1, y - 1)
        self.assertIn("Language and startup", term.screen.display[2])
        term.send(f"\x1b[<35;2;{y + 1}M")
        term.require(lambda: "UI palette" in term.screen.display[y])
        self.assertIn("Language and startup", term.screen.display[2])
        self.click_cell(term, 1, y)
        term.require(
            lambda: (
                "UI palette" in term.screen.display[2]
                and "UI palette" not in term.screen.display[y]
            )
        )
        first = next(
            i
            for i, row in enumerate(term.screen.display)
            if "⚙" in row.partition("│")[0] and i > 0
        )
        self.assertEqual(term.screen.buffer[y][1].fg, term.screen.buffer[first][1].fg)
        self.click_cell(term, 2, first)  # Blank cell beside a one-cell icon.
        self.click_cell(term, 1, y - 1)  # Vertical padding between the two icons.
        term.send(b"\x1b[1;5C")
        term.require(
            lambda: (
                term.screen.display[4].index("│") > 20
                and any(
                    "Help" in line and "Preset" in line for line in term.screen.display
                )
            )
        )
        self.assertIn("UI palette", term.screen.display[2])
        term.close_settings()

    def test_preset_menu_centers_creation_and_consumes_one_outside_click(self):
        """Observe both icon sets, dismiss over another button, then invoke creation."""
        for mode, glyph in (("unicode", "+"), ("nerdfont", "")):
            with self.subTest(mode=mode):
                original = (
                    f"config.ui_icons = {mode!r}\nconfig.presets = {{'saved': {{}}}}\n"
                )
                self.rc.write_text(original)
                term = self.terminal("-c", rows=34, cols=108)
                self.click_label(term, "Preset")
                term.require(partial(term.frame_has, "○ saved"))
                row = next(
                    y for y, s in enumerate(term.screen.display) if "○ saved" in s
                )
                # The ordinary row and the creation row occupy the same painted width.
                line = term.screen.buffer[row]
                marker = term.screen.display[row].index("○ saved")
                start = marker - 1
                end = start
                while end < term.cols and line[end].bg == line[marker].bg:
                    end += 1
                center = start + (end - start - wcswidth(glyph)) // 2
                self.assertEqual(term.screen.buffer[row + 1][center].data, glyph)
                # Release over the profile's Help button after the menu has repainted.
                profile = next(
                    y
                    for y, s in enumerate(term.screen.display)
                    if "Preset" in s and "Help" in s
                )
                x = term.screen.display[profile].index("Help")
                term.send(f"\x1b[<0;{x + 1};{profile + 1}M")
                term.require(lambda term=term: not term.frame_has("○ saved"))
                term.send(
                    f"\x1b[<32;{x + 2};{profile + 1}M\x1b[<0;{x + 2};{profile + 1}m"
                )
                self.click_label(term, "Preset")
                term.require(partial(term.frame_has, "○ saved"))
                self.assertTrue(term.frame_has("Language and startup"))
                self.click_cell(term, center, row + 1)
                term.require(partial(term.frame_has, "New preset name"))
                self.assertNotIn("<S>", term.screen.display[-1])
                term.send(b"\x13")
                term.send(b"\x1b")
                term.require(
                    lambda term=term: (
                        not term.frame_has("New preset name")
                        and "<S>" in term.screen.display[-1]
                    )
                )
                self.assertEqual(self.rc.read_text(), original)
                term.close_settings()

    def test_scrollbar_matches_divider_until_hover_or_drag(self):
        """Observe exact idle and active colors while keyboard focus is in the sidebar."""
        self.rc.write_text(
            "config.ui_palette = {'accent': '#a312df', 'muted2': '#174e75'}\n"
        )
        term = self.terminal(
            "-c",
            rows=24,
            cols=106,
            environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
        )
        x, top, bottom = self.scrollbar_at_top(term)

        def bar(color):
            buf = term.screen.buffer
            return buf[top][x].fg == buf[bottom][x].fg == color and any(
                buf[y][x].bg == color for y in range(top + 1, bottom)
            )

        term.require(lambda: bar("174e75"))
        term.send(f"\x1b[<35;{x + 1};{top + 2}M")
        term.require(lambda: bar("a312df"))
        term.send(f"\x1b[<0;{x + 1};{top + 2}M\x1b[<32;2;{bottom}M")
        term.require(
            lambda: bar("a312df") and term.screen.buffer[bottom - 1][x].bg == "a312df"
        )
        term.send(f"\x1b[<0;2;{bottom}m\x1b[<35;1;2M")
        term.require(lambda: bar("174e75"))
        term.close_settings()

    def test_extension_draft_and_profile_presets_survive_navigation(self):
        """Read literals, edit without activating code and restore a saved draft."""
        source = self.home / "plugin/script/sample/__init__.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "PLUGIN_META = {'version':'1','category':'module','settings':"
            "{'caption':{'title':'Caption','description':'Footer text','default':'first'}}}\n"
            "raise RuntimeError('metadata lookup imported extension')\n"
        )
        terminal = self.terminal("-c", rows=38, cols=118)
        self.click_label(terminal, "sample")
        self.click_field(terminal, "Caption")
        terminal.send(b"\x01\x0bnext\r")
        self.click_label(terminal, "Preset")
        terminal.require(
            lambda: any("   +" in row for row in terminal.screen.display[:-1])
        )
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("New preset name"))
        terminal.send(b"work")
        self.click_label(terminal, "Create preset", same_line="Close")
        terminal.require(
            lambda: any(
                "Help" in row and "work ▾" in row for row in terminal.screen.display
            )
        )
        self.assertFalse(self.rc.exists())
        self.click_label(terminal, "Help")
        terminal.require(lambda: terminal.frame_has("More details"))
        self.click_label(terminal, "Config", same_line="▾")
        terminal.require(
            lambda: any(
                "Help" in row and "work ▾" in row for row in terminal.screen.display
            )
        )
        self.click_field(terminal, "Caption")
        terminal.send(b"\x01\x0bchanged\r")
        self.click_label(terminal, "work ▾", same_line="Help")
        self.click_label(terminal, "● work")
        terminal.require(
            lambda: (
                terminal.frame_has("next") and not terminal.frame_has("Create preset")
            )
        )
        self.apply_action(terminal, "Save")
        terminal.close_settings()
        saved = load(self.rc)
        self.assertEqual(saved.active_preset, "work")
        self.assertEqual(saved.plugin_settings, {"sample": {"caption": "next"}})
        self.assertEqual(
            saved.presets["work"]["plugin_settings"], saved.plugin_settings
        )
        self.assertEqual(saved.plugins, [])

    def test_pointer_targets_match_painted_packages_palette_and_choices(self):
        """Ignore blank cells, highlight hovered choices and keep checked state until click."""
        for name in ("alpha", "beta"):
            package = self.home / "plugin/script" / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("PLUGIN_META = {'category': 'module'}")
        terminal = self.terminal("-c", rows=38, cols=118)
        self.click_label(terminal, "Modules")

        def locate(label, same=""):
            for y, row in enumerate(terminal.screen.display):
                if label in row and same in row:
                    return wcswidth(row.partition(label)[0]), y

        def move(label):
            x, y = locate(label)
            terminal.send(f"\x1b[<35;{x + 1};{y + 1}M")

        terminal.require(lambda: locate("[ ] beta"))
        x, y = locate("[ ] beta")
        before = terminal.screen.buffer[y][x].fg
        move("[ ] beta")
        terminal.require(lambda: terminal.screen.buffer[y][x].fg != before)
        # The sidebar still owns Enter. On entry the checkbox list still points to alpha.
        terminal.send(b"\r ")
        terminal.require(lambda: terminal.frame_has("[*] alpha"))
        self.assertTrue(terminal.frame_has("[ ] beta"))
        terminal.send(b" ")
        terminal.require(lambda: terminal.frame_has("[ ] alpha"))
        alpha = self.label_cell(terminal, "[ ] alpha", "0.0.0").fg
        move("[ ] beta")
        terminal.require(
            lambda: self.label_cell(terminal, "[ ] beta", "0.0.0").fg == alpha
        )
        self.click_cell(terminal, x + 46, y)
        self.click_label(terminal, "[ ] alpha")
        terminal.require(lambda: terminal.frame_has("[*] alpha"))
        self.assertTrue(terminal.frame_has("[ ] beta"))
        self.click_label(terminal, "UI palette")
        terminal.send(b"\r")
        terminal.require(lambda: locate("900", "100"))
        x, y = locate("900", "100")
        self.click_cell(terminal, x + 10, y)
        self.assertFalse(terminal.frame_has("lime-900"))
        self.click_label(terminal, "Language and startup")
        self.click_field(terminal, "Language")
        terminal.require(lambda: locate("○ Korean"))
        x, y = locate("○ Korean")
        old = terminal.screen.buffer[y][x].fg
        move("○ Korean")
        terminal.require(lambda: terminal.screen.buffer[y][x].fg != old)
        self.assertTrue(terminal.frame_has("● Automatic"))
        terminal.send(b"\r\x13")
        terminal.require(lambda: terminal.frame_has("Changes saved"))
        terminal.close_settings()
        saved = load(self.rc)
        self.assertEqual(saved.plugins, ["alpha"])
        self.assertEqual(saved.lang, "ko")
        self.assertEqual(saved.ui_palette, {})

    def test_field_navigation_reveals_heading_and_description(self):
        """Scroll to the bottom and return to the first setting using only field keys."""
        terminal = self.terminal("-c", rows=20)
        terminal.require(lambda: self.field_bounds(terminal, "Language"))
        terminal.send(b"\r" + b"\x1b[B" * 8)
        terminal.require(lambda: self.field_bounds(terminal, "Log level"))
        terminal.send(b"\x1b[A" * 8)
        terminal.require(lambda: self.field_bounds(terminal, "Language"))
        _, _, heading, editor = self.field_bounds(terminal, "Language")
        self.assertIn("LANG", terminal.screen.display[heading + 1])
        self.assertGreater(editor, heading + 1)
        terminal.close_settings()

    def test_help_page_header_offers_the_same_find_by_mouse_and_keyboard(self):
        """Preview categories in the heading; Enter opens a match and Esc cancels."""
        terminal = self.terminal("-h", language="ko")

        def header():
            return next(
                (
                    line.partition("│")[2]
                    for line in terminal.screen.display
                    if "⌕" in line
                ),
                "",
            )

        terminal.require(lambda: header().strip().startswith("시작하기"))
        self.assertTrue(header().rstrip().endswith("⌕"))
        self.click_label(terminal, "⌕")
        terminal.send("명령줄 옵션")
        terminal.require(lambda: header().strip().startswith("명령줄 옵션"))
        terminal.send(terminal.escape_key)
        terminal.require(lambda: header().strip().startswith("시작하기"))
        terminal.send("\x06명령줄 옵션\r")
        terminal.require(
            lambda: (
                terminal.frame_has("usage: ish") or terminal.frame_has("사용법: ish")
            )
        )
        terminal.send(b"\x1b[Z")  # Shift+Tab from the document to its toolbar.
        terminal.require(lambda: "찾기" in header())
        terminal.send(" 시작하기\r")
        terminal.require(lambda: header().strip().startswith("시작하기"))
        self.click_label(terminal, "×")
        terminal.finish(key=b"")
        self.assertFalse(self.rc.exists())

    def test_settings_save_from_keyboard_changes_the_actual_file(self):
        """Ctrl+S writes the next-session language and leaves the settings window open."""
        terminal = self.terminal("--config")
        terminal.require(lambda: terminal.frame_has("⚙  Settings"), timeout=45)
        terminal.send(b"\r\r\x1b[B\x1b[B\r\x13")
        terminal.require(lambda: terminal.frame_has("Changes saved."))
        self.assertEqual(load(self.rc).lang, "ko")
        saved = self.rc.read_bytes()
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\x01\x0b/bin/zsh\r")
        terminal.require(
            lambda: (
                terminal.frame_has("Unsaved changes")
                and "<F> Filter options " in terminal.screen.display[-1]
            )
        )
        footer = terminal.screen.display[-1]
        self.assertTrue(footer.startswith(" Ctrl +"))
        self.assertTrue(footer.endswith("▸ "))
        body = terminal.screen.display[1:-4]
        for x in (0, terminal.cols - 1):
            self.click_cell(terminal, x, terminal.rows - 1)
        # Last cell of the filter segment, immediately before the next shortcut.
        prefix, label, rest = footer.partition("<F> Filter options ")
        self.assertTrue(rest.startswith("▸"))
        self.click_cell(terminal, wcswidth(prefix + label), terminal.rows - 1)
        terminal.require(lambda: terminal.frame_has("Apply"))
        self.assertEqual(self.rc.read_bytes(), saved)
        terminal.send(b"\x13\x1b")
        terminal.require(lambda: not terminal.frame_has("Apply"))
        terminal.require(lambda: terminal.screen.display[1:-4] == body)
        self.assertEqual(self.rc.read_bytes(), saved)
        terminal.send(b"\x13")
        terminal.require(lambda: "config.shell = '/bin/zsh'" in self.rc.read_text())
        self.assertEqual(load(self.rc).shell, "/bin/zsh")
        self.assertFalse(terminal.exited())
        terminal.close_settings()

    def test_only_editable_catalogs_show_their_heading_and_instructions(self):
        """Render a checklist as one setting and leave an empty directory's useful state."""
        self.plugin()
        terminal = self.terminal("-c", rows=36)
        self.click_label(terminal, "Plugins")
        terminal.require(lambda: terminal.frame_has("[ ] chosen"))
        self.assertTrue(terminal.frame_has("Enabled plugins"))
        self.assertTrue(terminal.frame_has("Choose plugins for the next session"))
        self.click_label(terminal, "Modules")
        terminal.require(
            lambda: terminal.frame_has("No modules are declared in the selected")
        )
        self.assertFalse(terminal.frame_has("Enabled modules"))
        self.assertFalse(terminal.frame_has("Choose terminal protocol"))
        terminal.close_settings()

    def test_large_module_catalog_scrolls_and_selects_the_visible_package(self):
        """Visit a fresh catalog, then pick distant rows using both keyboard and mouse."""
        terminal = self.terminal("-c", rows=28)
        terminal.require(lambda: terminal.frame_has("Language and startup"))
        # First visit discovers packages even if the settings window was already open.
        for index in range(160):
            package = self.home / "plugin/script" / f"module{index:03}"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("PLUGIN_META = {'category': 'module'}")
        self.click_label(terminal, "Modules")
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("[ ] module000"))
        terminal.send(b"\x1b[1;3F")
        terminal.require(lambda: terminal.frame_has("[ ] module159"))
        self.click_label(terminal, "module157")
        terminal.require(lambda: terminal.frame_has("[*] module157"))
        terminal.send(b"\x1b[B ")
        terminal.require(lambda: terminal.frame_has("[*] module158"))
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).plugins, ["module157", "module158"])
        terminal.close_settings()

    def test_late_catalog_cannot_save_a_selection_and_exit_abandons_a_read(self):
        """Hold real directory reads to check explicit save validation and process exit."""
        for source, package in (
            ("first", "chosen"),
            ("invalid", "other"),
            ("valid", "chosen"),
            ("valid", "extra"),
        ):
            folder = self.home / source / package
            folder.mkdir(parents=True)
            (folder / "__init__.py").write_text("PLUGIN_META = {'version': '1'}")
        gate = self.root / "catalog-gate"
        os.mkfifo(gate)
        fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        marker = self.root / "scanning"
        (self.root / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            "from ish.plugin.manager import PluginManager\n"
            "discover = PluginManager.iter_candidates\n"
            "def wait_for_test(manager, **options):\n"
            "    if manager.script_dir.name in ('valid', 'invalid'):\n"
            f"        Path({str(marker)!r}).write_text(manager.script_dir.name)\n"
            f"        with open({str(gate)!r}, 'rb', buffering=0) as gate: gate.read(1)\n"
            "    yield from discover(manager, **options)\n"
            "PluginManager.iter_candidates = wait_for_test\n"
        )
        initial = "config.plugin_dir = 'first'\n"
        self.rc.write_text(initial)
        terminal = self.terminal(
            "-c",
            rows=40,
            environment={
                "PYTHONPATH": os.pathsep.join((str(self.root), str(ROOT / "src")))
            },
        )
        self.click_label(terminal, "Plugins")
        self.click_label(terminal, "chosen 1")
        terminal.require(lambda: terminal.frame_has("[*] chosen"))

        def change_directory(name):
            marker.unlink(missing_ok=True)
            self.click_field(terminal, "Plugin directory")
            terminal.send("\x01\x0b" + name + "\r")
            terminal.require(
                lambda: (
                    marker.exists()
                    and marker.read_text() == name
                    and terminal.frame_has("Working")
                )
            )

        change_directory("invalid")
        terminal.require(
            lambda: (
                len(self.status_rows(terminal)) == 1
                and self.status_contains(terminal, "Working")
                and "━" in terminal.screen.display[-3]
            )
        )
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: terminal.frame_has("Save again after the package list")
        )
        self.assertEqual(self.rc.read_text(), initial)
        os.write(fd, b".")
        terminal.require(lambda: terminal.frame_has("[ ] other"))
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: (
                self.status_contains(terminal, "Cannot complete this action")
                and self.status_contains(
                    terminal, "Selected plugin is missing from this directory."
                )
            )
        )
        self.assertEqual(self.rc.read_text(), initial)

        change_directory("valid")
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: terminal.frame_has("Save again after the package list")
        )
        os.write(fd, b".")
        terminal.require(lambda: terminal.frame_has("[*] chosen"))
        self.assertEqual(self.rc.read_text(), initial)
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).plugins, ["chosen"])
        self.assertEqual(load(self.rc).plugin_dir, self.home / "valid")

        # An unchanged selection needs no catalog validation on an ordinary save.
        change_directory("invalid")
        self.click_action(terminal, "Save")
        terminal.require(lambda: load(self.rc).plugin_dir == self.home / "invalid")
        self.assertEqual(load(self.rc).plugins, ["chosen"])
        os.write(fd, b".")
        terminal.require(lambda: terminal.frame_has("[ ] other"))
        change_directory("valid")
        os.write(fd, b".")
        self.click_label(terminal, "extra 1")
        terminal.require(lambda: terminal.frame_has("[*] extra"))
        change_directory("invalid")
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: terminal.frame_has("Save again after the package list")
        )
        terminal.close_settings()
        self.assertEqual(load(self.rc).plugins, ["chosen"])
        self.assertEqual(load(self.rc).plugin_dir, self.home / "invalid")

    def test_plugin_and_module_pages_share_ordered_selections_without_imports(self):
        """Selecting a dual-purpose package on either page updates one next-session list."""
        marker = self.root / "package-code-executed"
        for name, extra in (
            ("plain", {}),
            ("custom", {"category": "custom"}),
            ("xwire", {"category": "module"}),
            ("shared", {"category": "both"}),
        ):
            package = self.home / "plugin/script" / name
            package.mkdir(parents=True)
            meta = {"version": "1.1", "description": "Example", **extra}
            (package / "__init__.py").write_text(
                f"PLUGIN_META = {meta!r}\nopen({str(marker)!r}, 'w').close()\n"
            )
        self.rc.write_text("config.plugins = ['missing', 'plain']\n")
        terminal = self.terminal("-c", rows=38)
        self.click_label(terminal, "Modules")
        terminal.require(lambda: terminal.frame_has("[ ] xwire"))
        self.assertFalse(terminal.frame_has("plain 1.1"))
        self.click_label(terminal, "xwire 1.1")
        self.click_label(terminal, "shared 1.1")
        terminal.require(lambda: terminal.frame_has("[*] shared"))
        self.click_label(terminal, "Plugins")
        terminal.require(
            lambda: (
                terminal.frame_has("[*] shared") and terminal.frame_has("[ ] custom")
            )
        )
        self.assertFalse(terminal.frame_has("xwire 1.1"))
        self.assertTrue(terminal.frame_has("[*] missing"))
        self.click_label(terminal, "shared 1.1")
        self.click_label(terminal, "Modules")
        terminal.require(
            lambda: terminal.frame_has("[ ] shared") and terminal.frame_has("[*] xwire")
        )
        self.click_label(terminal, "shared 1.1")
        terminal.send(b"\x06xwire\r")
        terminal.require(
            lambda: (
                terminal.frame_has("[*] xwire") and not terminal.frame_has("shared 1.1")
            )
        )
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).plugins, ["missing", "plain", "xwire", "shared"])
        self.click_label(terminal, "Plugins")
        terminal.require(
            lambda: (
                terminal.frame_has("[*] shared") and terminal.frame_has("[ ] custom")
            )
        )
        self.click_label(terminal, "Modules")
        terminal.require(
            lambda: (
                terminal.frame_has("[*] xwire") and not terminal.frame_has("shared 1.1")
            )
        )
        self.click_label(terminal, "xwire 1.1")
        self.apply_action(terminal, "Cancel")
        self.assertTrue(terminal.frame_has("[*] xwire"))
        terminal.close_settings()
        self.assertFalse(marker.exists())

    def test_cleared_plugin_directory_refreshes_and_saves_the_default_source_root(self):
        """Accept a relative or blank source without losing another unsaved field."""
        for package in (
            self.home / "plugin/script/from_default",
            self.home / "other/from_other",
            self.home / "another/from_another",
            self.home / "decoy_at_home",
        ):
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "PLUGIN_META = {'category': 'both', 'version': '1'}\n"
            )
        original = "config.plugin_dir = 'other'\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=38)
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\x01\x0bdraft-shell\r")
        terminal.require(lambda: terminal.frame_has("draft-shell"))
        self.click_label(terminal, "Modules")
        terminal.require(lambda: terminal.frame_has("[ ] from_other"))
        self.click_label(terminal, "Plugins")
        self.click_field(terminal, "Plugin directory")
        terminal.send(b"\x01\x0banother\r")
        terminal.require(lambda: terminal.frame_has("[ ] from_another"))
        self.assertFalse(terminal.frame_has("from_other"))
        self.click_field(terminal, "Plugin directory")
        terminal.send(b"\x01\x0b\r")
        terminal.require(lambda: terminal.frame_has("[ ] from_default"))
        self.assertEqual(self.rc.read_text(), original)
        self.click_label(terminal, "Modules")
        terminal.require(
            lambda: (
                terminal.frame_has("Enabled modules")
                and terminal.frame_has("[ ] from_default")
            )
        )
        self.assertFalse(terminal.frame_has("decoy_at_home"))
        self.assertFalse(terminal.frame_has("from_other"))
        self.click_label(terminal, "from_default")
        terminal.require(lambda: terminal.frame_has("[*] from_default"))
        self.apply_action(terminal, "Save")
        terminal.close_settings()
        saved = load(self.rc)
        self.assertIsNone(saved.plugin_dir)
        self.assertEqual(saved.plugins, ["from_default"])
        self.assertEqual(saved.shell, "draft-shell")

    def test_click_on_previous_catalog_cannot_select_a_replacement_package(self):
        """The click that ends a path edit cannot toggle a row from either folder."""
        self.plugin()
        alternate = self.home / "alternative/new_package"
        alternate.mkdir(parents=True)
        (alternate / "__init__.py").write_text("PLUGIN_META = {'version': '1'}\n")
        terminal = self.terminal("-c", rows=40)
        self.click_label(terminal, "Plugins")
        terminal.require(lambda: terminal.frame_has("[ ] chosen"))
        self.click_field(terminal, "Plugin directory")
        terminal.send(b"\x01\x0balternative")
        terminal.require(lambda: terminal.frame_has("alternative"))
        y = next(
            n for n, row in enumerate(terminal.screen.display) if "chosen 1.0" in row
        )
        x = wcswidth(terminal.screen.display[y].partition("chosen 1.0")[0])
        terminal.send(f"\x1b[<0;{x + 1};{y + 1}M")
        # Let the host repaint while the press is held; its release must not toggle a new row.
        terminal.resize(41, terminal.cols)
        terminal.require(lambda: "Scroll" in terminal.screen.display[-1])
        terminal.send(f"\x1b[<0;{x + 1};{y + 1}m")
        terminal.require(lambda: terminal.frame_has("[ ] new_package"))
        self.assertFalse(terminal.frame_has("chosen 1.0"))
        self.click_label(terminal, "new_package")
        terminal.require(lambda: terminal.frame_has("[*] new_package"))
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).plugins, ["new_package"])

        # A new package on disk and an externally changed rc are both visible
        # after Ctrl+R, even while this field contains a different draft path.
        restored = self.home / "plugin/script/addition"
        restored.mkdir()
        (restored / "__init__.py").write_text("PLUGIN_META = {'version': '2'}\n")
        external = "config.plugins = ['addition']\n"
        self.rc.write_text(external)
        self.click_field(terminal, "Plugin directory")
        terminal.send(b"\x01\x0buncommitted\x12")
        terminal.require(
            lambda: (
                terminal.frame_has("[*] addition") and terminal.frame_has("[ ] chosen")
            )
        )
        self.assertFalse(terminal.frame_has("new_package"))
        self.assertFalse(terminal.frame_has("uncommitted"))
        self.assertEqual(self.rc.read_text(), external)
        terminal.close_settings()

    def test_repeated_save_cancel_and_reload_share_the_last_successful_snapshot(self):
        """Cancel can restore even an invalid draft without writing or rereading the rc."""
        loads = self.root / "rc-load-count"
        prefix = f"with open({str(loads)!r}, 'a') as f: f.write('x')\n"
        self.rc.write_text(prefix + "config.shell = 'initial-shell'\n")
        terminal = self.terminal("-c", rows=40)

        def edit_field(label, value):
            self.click_field(terminal, label)
            terminal.send("\x01\x0b" + value + "\r")
            terminal.require(partial(terminal.frame_has, "Unsaved changes"))

        for shell in ("saved-first-shell", "saved-second-shell"):
            edit_field("Shell executable", shell)
            self.apply_action(terminal, "Save")
            self.assertIn(f"config.shell = {shell!r}", self.rc.read_text())
        saved = self.rc.read_bytes()
        edit_field("Shell executable", "discard-me")
        self.apply_action(terminal, "Cancel")
        self.assertTrue(terminal.frame_has("saved-second-shell"))
        self.assertEqual(self.rc.read_bytes(), saved)
        self.assertEqual(loads.read_text(), "x")

        external = prefix + "config.shell = 'external-shell'\n"
        self.rc.write_text(external)
        edit_field("Shell executable", "conflicting-shell")
        self.click_action(terminal, "Save")
        terminal.require(lambda: self.status_contains(terminal, "changed outside"))
        self.apply_action(terminal, "Cancel")
        self.assertTrue(terminal.frame_has("saved-second-shell"))
        self.assertEqual(self.rc.read_text(), external)
        self.assertEqual(loads.read_text(), "x")
        terminal.send(b"\x12")
        terminal.require(lambda: terminal.frame_has("reloaded from disk"))
        self.click_field(terminal, "Esc / Alt wait (s)")
        terminal.send(b"\x01\x0bnot-a-number\r")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ Esc / Alt wait (s)")
                and self.status_contains(terminal, "must be between")
            )
        )
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: terminal.frame_has("not-a-number") and terminal.frame_has("!")
        )
        self.apply_action(terminal, "Cancel")
        self.assertTrue(terminal.frame_has("external-shell"))
        self.assertFalse(terminal.frame_has("not-a-number"))
        self.assertEqual(self.rc.read_text(), external)
        self.assertEqual(loads.read_text(), "xx")
        terminal.close_settings()

    def test_save_leaves_an_unconfirmed_choice_out_of_another_field_change(self):
        """A Save click writes the accepted field while an open list is only highlighted."""
        self.rc.write_text("config.shell = 'original'\nconfig.lang = 'en'\n")
        terminal = self.terminal("-c", rows=38)
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\x01\x0bnew-shell\r")
        self.click_field(terminal, "Language")
        terminal.send(b"\x1b[B")
        terminal.require(lambda: terminal.frame_has("Korean"))
        self.apply_action(terminal, "Save")
        saved = load(self.rc)
        self.assertEqual((saved.shell, saved.lang), ("new-shell", "en"))
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\x01\x0bunwritten-shell\r")
        terminal.require(lambda: terminal.frame_has("Unsaved changes"))
        terminal.send(b"\x1b[D")
        terminal.finish(key=b"\x1b")
        self.assertEqual(load(self.rc).shell, "new-shell")

    def assert_actions_at_right(self, terminal, cancel, save):
        """Cancel precedes Save on the last main row, adjacent to the right margin."""
        terminal.require(lambda: self.status_rows(terminal, cancel, save))
        row = terminal.screen.display[-3]
        body = row.partition("│")[2]
        right = len(body) - len(body.rstrip())
        self.assertLessEqual(right, 3)
        return row

    @staticmethod
    def label_cell(terminal, label, same_line):
        """Read the actual host attributes at the first character of a visible label."""
        for y, row in enumerate(terminal.screen.display):
            if label in row and same_line in row:
                return terminal.screen.buffer[y][wcswidth(row.partition(label)[0])]
        return None

    def test_expanded_dropdown_fills_every_cell_between_its_edges(self):
        """Overlay every candidate cell without moving the form and restore what it covered."""
        self.rc.write_text("config.ui_palette = {'selection': 'blue-800'}\n")
        terminal = self.terminal("-c", rows=36, cols=110)
        terminal.require(lambda: self.field_bounds(terminal, "Starting directory"))
        left, right, _, first = self.field_bounds(terminal, "Language")
        directory = self.field_bounds(terminal, "Starting directory")
        covered = tuple(
            row[left : right + 1]
            for row in terminal.screen.display[first + 1 : first + 4]
        )
        self.click_field(terminal, "Language")
        terminal.require(lambda: terminal.frame_has("○ Korean"))
        self.assertEqual(self.field_bounds(terminal, "Starting directory"), directory)
        self.assertIn("Automatic", terminal.screen.display[first])
        expected = terminal.screen.buffer[first][left + 3].bg
        self.assertNotEqual(expected, "default")
        for y in range(first + 1, first + 4):
            row = terminal.screen.buffer[y]
            self.assertEqual((row[left].data, row[right].data), ("│", "│"))
            self.assertEqual({row[x].bg for x in range(left, right + 1)}, {expected})
            self.assertEqual(row[right + 1].bg, "default")
        terminal.send(b"\x1b[B")
        self.click_cell(terminal, right + 3, first + 2)
        terminal.require(lambda: not terminal.frame_has("○ Korean"))
        self.assertEqual(
            tuple(
                row[left : right + 1]
                for row in terminal.screen.display[first + 1 : first + 4]
            ),
            covered,
        )
        self.click_field(terminal, "Language")
        terminal.require(lambda: terminal.frame_has("○ Korean"))
        self.click_cell(terminal, right - 1, first + 3)
        terminal.require(
            lambda: terminal.frame_has("Korean") and not terminal.frame_has("○ English")
        )
        self.assertNotIn("✎ Shell executable", "\n".join(terminal.screen.display))
        terminal.send(b"\x13")
        terminal.require(lambda: load(self.rc).lang == "ko")
        terminal.close_settings()

    def test_dropdown_flips_above_and_disappears_when_its_anchor_scrolls_away(self):
        """A bottom field's menu follows its visible anchor across resize and scroll."""
        terminal = self.terminal("-c", rows=24, cols=86)
        self.click_field(terminal, "Language")
        terminal.send(b"\x1b[1;3F")  # Alt+End moves the page, not the candidate.
        terminal.require(
            lambda: (
                terminal.frame_has("Log level") and not terminal.frame_has("○ Korean")
            )
        )
        self.click_field(terminal, "Log level")
        terminal.require(lambda: terminal.frame_has("○ CRITICAL"))

        def assert_above():
            rows = terminal.screen.display
            anchor = next(
                i for i, row in enumerate(rows) if "WARNING" in row and "●" not in row
            )
            candidates = [
                i
                for i, row in enumerate(rows)
                if "○ DEBUG" in row or "○ CRITICAL" in row
            ]
            self.assertEqual(len(candidates), 2)
            self.assertTrue(all(row < anchor for row in candidates))
            self.assertIn("Cancel", rows[-3])

        assert_above()
        terminal.resize(23, 78)
        terminal.require(
            lambda: (
                len(terminal.screen.display[0]) == 78
                and "×" in terminal.screen.display[0]
                and terminal.frame_has("○ CRITICAL")
            )
        )
        assert_above()
        terminal.send(b"\x1b[B\x1b")
        terminal.require(
            lambda: (
                not terminal.frame_has("○ CRITICAL") and terminal.frame_has("WARNING")
            )
        )
        self.assertFalse(self.rc.exists())
        # The body now fits only three candidate rows. Click the last visible
        # candidate after the list scrolls to verify its logical item is saved.
        terminal.resize(12, 78)
        terminal.require(lambda: self.field_bounds(terminal, "Log level"))
        self.click_field(terminal, "Log level")
        terminal.require(lambda: terminal.frame_has("○ DEBUG"))
        self.assertFalse(terminal.frame_has("○ CRITICAL"))
        terminal.send(b"\x1b[B\x1b[B")
        terminal.require(lambda: terminal.frame_has("○ CRITICAL"))
        self.assertFalse(terminal.frame_has("○ DEBUG"))
        self.click_label(terminal, "○ CRITICAL")
        terminal.require(lambda: not terminal.frame_has("● WARNING"))
        terminal.send(b"\x13")
        terminal.require(
            lambda: self.rc.exists() and load(self.rc).log_level == "CRITICAL"
        )
        terminal.close_settings()

    def test_button_groups_release_focus_to_the_clicked_content(self):
        """Hover and group arrows select actions; a background click restores the field."""
        terminal = self.terminal(
            "-c",
            rows=38,
            cols=115,
            environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
        )
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\r")
        terminal.require(lambda: not terminal.frame_has("✎ Shell executable"))
        cancel = partial(self.label_cell, terminal, "Cancel", "Save")
        save = partial(self.label_cell, terminal, "Save", "Cancel")
        left, _, heading, _ = self.field_bounds(terminal, "Shell executable")
        terminal.send(b"\t")
        terminal.require(lambda: cancel() and save() and cancel().fg != save().fg)
        # Stop at the first/last item in each horizontal group.
        terminal.send(b"\x1b[D\x1b[C\x1b[C")
        terminal.require(
            lambda: (
                save().fg == self.label_cell(terminal, "Language and startup", "⚲").fg
            )
        )
        self.click_cell(terminal, left, heading + 1)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        terminal.send(b"\r")
        toolbar = next(row for row in terminal.screen.display if "⚲" in row)
        toolbar_y = terminal.screen.display.index(toolbar)
        x = wcswidth(toolbar.partition("⚲")[0])
        terminal.send(f"\x1b[<35;{x + 1};{toolbar_y + 1}M")
        terminal.require(lambda: "Filter options" in terminal.screen.display[toolbar_y])
        self.assertFalse(terminal.frame_has("Apply"))
        self.click_cell(terminal, terminal.cols - 6, heading)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        # Hover highlights the tool while the text field keeps the keyboard.
        terminal.send(f"\x1b[<35;{x + 1};{toolbar_y + 1}M")
        terminal.require(lambda: "Filter options" in terminal.screen.display[toolbar_y])
        self.assertEqual(
            terminal.screen.buffer[toolbar_y][x].fg,
            self.label_cell(terminal, "✎ Shell executable", "Shell executable").fg,
        )
        terminal.send(b"\x01\x0bdraft-shell\r")
        terminal.require(lambda: terminal.frame_has("draft-shell"))
        self.click_label(terminal, "Modules")
        terminal.require(lambda: terminal.frame_has("No modules are declared"))
        terminal.send(b"\r\t\t")
        terminal.require(lambda: "Filter options" in terminal.screen.display[toolbar_y])
        self.click_label(terminal, "No modules are declared")
        terminal.send(b"\x1b")
        terminal.require(
            lambda: self.label_cell(terminal, "Modules", "▸").fg == "bef264"
        )
        terminal.send(b"\x1b[A")
        terminal.require(
            lambda: "Plugins" in terminal.screen.display[2].partition("│")[2]
        )
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_leaving_hovered_buttons_restores_the_previous_focus(self):
        """Hover across both groups, leave the buttons, and operate the original target."""
        terminal = self.terminal("-c", rows=38, cols=115)
        self.click_field(terminal, "Shell executable")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        terminal.send(b"\r")
        terminal.require(lambda: not terminal.frame_has("✎ Shell executable"))
        left, _, heading, _ = self.field_bounds(terminal, "Shell executable")

        def move(x, y):
            terminal.send(f"\x1b[<35;{x + 1};{y + 1}M")

        def hover(label, same_line):
            terminal.require(
                lambda: any(
                    label in row and same_line in row for row in terminal.screen.display
                )
            )
            for y, row in enumerate(terminal.screen.display):
                if label in row and same_line in row:
                    move(wcswidth(row.partition(label)[0]), y)
                    return
            self.fail((label, same_line))

        def active(label):
            button = self.label_cell(
                terminal, label, "Cancel" if label == "Save" else "Save"
            )
            title = self.label_cell(terminal, "Language and startup", "⚲")
            return button and title and button.fg == title.fg

        ink = terminal.screen.buffer[heading][left].fg
        hover("Save", "Cancel")
        terminal.require(lambda: active("Save"))
        self.assertEqual(terminal.screen.buffer[heading][left].fg, ink)
        hover("⚲", "Language and startup")
        terminal.require(
            lambda: any(
                "Filter options" in row and "⚲" in row
                for row in terminal.screen.display
            )
        )
        hover("Cancel", "Save")
        terminal.require(lambda: active("Cancel"))
        move(left, heading + 1)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))

        # Clicking the hovered action keeps its result when the pointer leaves.
        terminal.send(b"\x01\x0bmouse-save-")
        hover("Save", "Cancel")
        terminal.require(
            lambda: active("Save") and terminal.frame_has("✎ Shell executable")
        )
        terminal.send(b"shell")
        terminal.require(lambda: terminal.frame_has("mouse-save-shell"))
        hover("Save", "Cancel")
        terminal.require(lambda: active("Save"))
        self.click_action(terminal, "Save")
        terminal.require(lambda: terminal.frame_has("Changes saved"))
        move(left, heading + 1)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        self.assertEqual(load(self.rc).shell, "mouse-save-shell")

        # Keyboard selection survives a visit to a different button with the pointer.
        terminal.send(b"\x01\x0bhover-focus-shell\r\t")
        terminal.require(lambda: active("Cancel"))
        hover("Save", "Cancel")
        terminal.require(lambda: active("Save"))
        move(left, heading + 1)
        terminal.require(lambda: active("Cancel"))
        hover("Save", "Cancel")
        terminal.require(lambda: active("Save"))
        terminal.send(b"\x1b[D\x1b[C")
        move(left, heading + 1)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("Changes saved"))
        self.assertEqual(load(self.rc).shell, "hover-focus-shell")

        hover("⚲", "Language and startup")
        self.click_label(terminal, "⚲", same_line="Language and startup")
        terminal.require(lambda: terminal.frame_has("Apply"))
        move(left, heading + 1)
        terminal.send(b"ui_icons\r")
        terminal.require(lambda: terminal.frame_has("Filter: ui_icons"))
        terminal.close_settings()

    def test_compact_toolbar_identifies_focused_actions_and_opens_filter(self):
        """Navigate the three visible glyphs and open the filter without saving."""
        terminal = self.terminal("-c", rows=30, cols=120)

        def toolbar():
            return next(
                (
                    line
                    for line in terminal.screen.display
                    if all(icon in line for icon in "⚲↻✎")
                ),
                "",
            )

        terminal.require(
            lambda: (
                toolbar()
                and terminal.frame_has("Ctrl +")
                and self.status_rows(terminal) == [""]
            )
        )
        line = toolbar()
        self.assertIn("Language and startup", line.rsplit("│", 1)[-1])
        self.assertGreater(line.index("⚲"), terminal.cols * 3 // 4)
        self.assertLess(line.index("⚲"), line.index("↻"))
        self.assertLess(line.index("↻"), line.index("✎"))
        for label in ("Filter options", "Reload", ".ishrc.py"):
            self.assertNotIn(label, line)
        actions = self.assert_actions_at_right(terminal, "Cancel", "Save")
        self.assertEqual(self.status_rows(terminal), [""])
        self.assertEqual(terminal.screen.display[-2].partition("│")[2].strip(), "")
        self.assertEqual(
            set(terminal.screen.display[-4].partition("│")[2].strip()), {"─"}
        )
        footer = next(row for row in terminal.screen.display if "Ctrl +" in row)
        self.assertIn("<S> Save", footer)
        self.assertNotRegex(actions, r"[<>]")

        for key, label, other in (
            (b"\r\t", "Cancel", "Save"),
            (b"\x1b[C", "Save", "Cancel"),
        ):
            cell = partial(self.label_cell, terminal, label, other)
            before = cell()
            terminal.send(key)
            terminal.require(
                lambda cell=cell, before=before: (
                    (current := cell()) is not None and current != before
                )
            )
            self.assertFalse(cell().underscore)
        terminal.send(b"\t")
        terminal.require(lambda: "Filter options" in toolbar())
        self.assertEqual(self.status_rows(terminal), [""])
        terminal.send(b"\x1b[C")
        terminal.require(
            lambda: "Reload settings (discard unsaved changes)" in toolbar()
        )
        terminal.send(b"\x1b[C")
        terminal.require(lambda: ".ishrc.py" in toolbar())
        self.assertEqual(self.status_rows(terminal), [""])
        terminal.send(b"\x1b[D\x1b[D\r")
        terminal.require(
            lambda: terminal.frame_has("Apply") and terminal.frame_has("Cancel")
        )
        for row in terminal.screen.display:
            if "Apply" in row or "Cancel" in row:
                self.assertNotRegex(row, r"[<>]")
        for label in ("Apply", "Cancel"):
            cell = partial(self.label_cell, terminal, label, "Apply")
            before = cell()
            terminal.send(b"\t")
            terminal.require(
                lambda cell=cell, before=before: (
                    (current := cell()) is not None and current != before
                )
            )
            self.assertFalse(cell().underscore)
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("Apply"))
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_narrow_korean_page_heading_keeps_actions_clickable_after_resize(self):
        """Keep translated page names, toolbar targets and the footer distinct."""
        terminal = self.terminal("-c", language="ko", rows=26, cols=100)

        def heading(title):
            for line in terminal.screen.display:
                main = line.partition("│")[2]
                if title in main and "⚲" in main:
                    return main
            return ""

        terminal.require(lambda: heading("언어·시작 설정"))
        self.click_label(terminal, "입력·화면")
        terminal.require(lambda: heading("입력·화면"))
        terminal.resize(22, 65)
        terminal.require(lambda: all(icon in heading("입력·화면") for icon in "⚲↻✎"))
        line = heading("입력·화면")
        self.assertLess(line.index("입력·화면"), line.index("⚲"))
        self.assertLess(line.index("⚲"), line.index("↻"))
        self.assertLess(line.index("↻"), line.index("✎"))
        self.assert_actions_at_right(terminal, "취소", "저장")
        self.assertEqual(self.status_rows(terminal, "취소", "저장"), [""])
        self.assertTrue(terminal.screen.display[-1].startswith(" Ctrl +"))
        self.assertTrue(terminal.screen.display[-1].endswith("▸ "))
        terminal.send(b"\r\x1b[Z\x1b[D\r")
        terminal.require(
            lambda: "파일에서" in "".join(self.status_rows(terminal, "취소", "저장"))
        )
        self.click_label(terminal, "⚲", same_line="입력·화면")

        def dialog():
            return any(
                "적용" in row and "취소" in row for row in terminal.screen.display
            )

        terminal.require(dialog)
        terminal.send(b"\x1b")
        terminal.require(lambda: not dialog())
        self.click_action(terminal, "취소", cancel="취소", save="저장")
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_tiny_view_restores_the_same_editor_and_insertion_point(self):
        """Shrink without submitting; keep the edit buffer and its mid-line cursor."""
        self.rc.write_text("config.ui_palette = {'warning': '#bd642a'}\n")
        term = self.terminal(
            "-c", rows=38, environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"}
        )
        self.click_field(term, "Shell executable")
        term.send(b"\x01\x0bABCD\x1b[D\x1b[D")
        term.require(lambda: term.frame_has("ABCD"))
        for rows, cols in ((3, 20), (2, 100), (30, 19)):
            term.resize(rows, cols)
            term.require(lambda: term.frame_has("Enlarge the window"))
            self.assertEqual(
                self.label_cell(term, "Enlarge the window", "").fg, "bd642a"
            )
            self.assertFalse(term.frame_has("ABCD"))
        term.resize(38, 110)
        term.require(lambda: term.frame_has("✎ Shell executable"))
        term.send(b"X")
        term.require(lambda: term.frame_has("ABXCD"))
        self.assertNotIn("ABXCD", self.rc.read_text())
        term.send(b"\r\x13")
        term.require(lambda: "ABXCD" in self.rc.read_text())
        term.close_settings()

    def test_standalone_and_visited_help_use_the_available_palette(self):
        """Resolve a named literal in standalone help or reuse the open settings draft."""
        marker = self.root / "loads"
        direct = (
            f"with open({str(marker)!r}, 'a') as f: f.write('x')\n"
            "theme = {'text': '#357d64'}\nconfig.ui_palette = theme\n"
        )
        self.rc.write_text(direct)
        env = {"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"}

        def header_is(ui, title, color):
            cell = self.label_cell(ui, title, "×")
            return cell is not None and cell.fg == color

        help_ui = self.terminal("-h", environment=env)
        help_ui.require(partial(header_is, help_ui, "Help", "357d64"))
        help_ui.close_settings()
        self.assertFalse(marker.exists())

        self.rc.write_text(direct.replace("= theme\n", "= dict(theme)\n"))
        ui = self.terminal("-c", rows=38, environment=env)
        ui.require(partial(header_is, ui, "Settings", "357d64"))
        self.click_label(ui, "Help", same_line="Preset")
        ui.require(partial(header_is, ui, "Help", "357d64"))
        self.assertEqual(marker.read_text(), "x")
        ui.close_settings()

    def test_icon_style_previews_and_help_reads_only_the_saved_selection(self):
        """Preview an icon set, discard it, then save it for read-only interactive help."""
        marker = self.root / "rc-loads"
        original = (
            f"with open({str(marker)!r}, 'a') as f: f.write('x')\n"
            "config.ui_icons = 'unicode'\n"
        )
        self.rc.write_text(original)
        for save in (False, True):
            with self.subTest(save=save):
                terminal = self.terminal("-c", rows=38)
                terminal.require(partial(terminal.frame_has, "⚙  Settings"))
                self.assertNotIn("\ue0b0".encode(), terminal.output)
                self.click_field(terminal, "Icon style")
                self.click_label(terminal, "Nerd Font")
                terminal.require(partial(terminal.frame_has, "\uf013 Settings"))
                self.click_label(terminal, "\uf0d7", same_line="Nerd Font")
                terminal.require(partial(terminal.frame_has, "\uf0d8"))
                self.click_label(terminal, "\uf0d8")
                terminal.require(
                    lambda terminal=terminal: not terminal.frame_has("\uf0d8")
                )
                self.assertTrue(terminal.frame_has("△"))
                self.assertTrue(terminal.frame_has("▽"))
                self.assertIn("←↓↑→", terminal.screen.display[-1])
                self.assertEqual(self.rc.read_text(), original)
                self.apply_action(terminal, "Save" if save else "Cancel")
                if not save:
                    self.assertEqual(self.rc.read_text(), original)
                    self.assertTrue(terminal.frame_has("⚙  Settings"))
                terminal.close_settings()
        self.assertIn("config.ui_icons = 'nerdfont'", self.rc.read_text())
        self.assertEqual(marker.read_text(), "xx")
        help_ui = self.terminal("-h")
        help_ui.require(
            lambda: (
                "󰋗 Help" in help_ui.screen.display[0] and help_ui.frame_has("\ue0b0")
            )
        )
        help_ui.resize(22, 100)
        help_ui.send(b"\x06Command-line options\r")
        help_ui.require(lambda: help_ui.frame_has("usage: ish"))
        right = "\ueb70"
        help_ui.require(
            lambda: "\u2190\u2193\u2191\u2192" in help_ui.screen.display[-1]
        )
        self.assertTrue(
            any(
                right in row.partition("│")[0]
                and "Command-line options" in row.partition("│")[0]
                for row in help_ui.screen.display
            )
        )
        x, top, bottom = self.scrollbar_at_top(help_ui)
        start = next(
            row.index("usage: ish")
            for row in help_ui.screen.display
            if "usage: ish" in row
        )

        def visible():
            return tuple(
                row[start:x] for row in help_ui.screen.display[top : bottom + 1]
            )

        before = visible()
        self.click_cell(help_ui, x, bottom)
        help_ui.require(lambda: visible()[:-1] == before[1:])
        self.click_label(help_ui, "\u2191", same_line="Alt +")
        help_ui.require(lambda: visible() == before)
        self.click_label(help_ui, "\u2193", same_line="Alt +")
        help_ui.require(lambda: visible()[:-1] == before[1:])
        self.click_cell(help_ui, x, top)
        help_ui.require(lambda: visible() == before)
        self.click_label(help_ui, "×")
        help_ui.finish(key=b"")
        self.assertEqual(marker.read_text(), "xx")

    def test_help_scroll_keys_and_drag_share_the_same_document(self):
        """Move to each end of the help page and drop the thumb outside the pane."""
        terminal = self.terminal("-h", rows=22, cols=112)
        terminal.require(lambda: "📖  Help" in terminal.screen.display[0], timeout=45)
        terminal.send(b"\x06Command-line options\r")
        terminal.require(lambda: terminal.frame_has("usage: ish"))
        x, top, bottom = self.scrollbar_at_top(terminal)
        usage_line = next(
            line for line in terminal.screen.display if "usage: ish" in line
        )
        left = usage_line.index("usage: ish")

        def viewport():
            return tuple(
                line[left:x] for line in terminal.screen.display[top : bottom + 1]
            )

        def at_end():
            # The last CLI example can wrap across two physical terminal rows.
            return "--shell-arg=ARG." in "".join(row.strip() for row in viewport())

        def option_rows():
            # Use two visible options so a partial PTY read cannot become the baseline.
            rows = viewport()
            return tuple(
                next((i for i, row in enumerate(rows) if option in row), -1)
                for option in ("--escape-delay", "--clipboard")
            )

        terminal.require(lambda: all(i > 0 for i in option_rows()))
        initial_rows = option_rows()
        terminal.send(b"\x1b[1;3F")  # Alt+End, xterm modified-key encoding.
        terminal.require(at_end)
        self.assertFalse(terminal.frame_has("usage: ish"))
        terminal.send(b"\x1b\x1b[H")  # Alt+Home, ESC-prefixed Home encoding.
        terminal.require(lambda: option_rows() == initial_rows)
        # Releasing a scrollbar arrow over a different cell must not take a step.
        terminal.send(f"\x1b[<0;{x + 1};{bottom + 1}M\x1b[<0;{x};{bottom + 1}m")
        self.click_cell(terminal, x, bottom)
        terminal.require(lambda: option_rows() == tuple(i - 1 for i in initial_rows))
        self.click_cell(terminal, x, top)
        terminal.require(lambda: option_rows() == initial_rows)

        # The first painted cell beneath the upper arrow is the draggable thumb.
        terminal.send(
            f"\x1b[<0;{x + 1};{top + 2}M"
            f"\x1b[<32;1;{terminal.rows}M\x1b[<0;1;{terminal.rows}m"
        )
        terminal.require(at_end)
        self.assertFalse(terminal.exited())
        terminal.send(b"\x1b[1;3H")
        terminal.require(lambda: terminal.frame_has("usage: ish"))
        self.click_label(terminal, "×")
        terminal.finish(key=b"")
        self.assertFalse(self.rc.exists())

    def test_options_filter_belongs_to_its_page_from_sidebar_or_body(self):
        """Use the same dialog from either pane and recover each page's own filter."""
        original = b"config.shell = 'untouched-shell'\n"
        self.rc.write_bytes(original)
        terminal = self.terminal("-c", rows=38, cols=115)

        def require_page(title):
            terminal.require(
                lambda: title in terminal.screen.display[2].partition("│")[2]
            )

        def open_dialog():
            terminal.send(b"\x06")
            terminal.require(
                lambda: any(
                    "Apply" in row and "Cancel" in row
                    for row in terminal.screen.display
                )
            )

        def apply(query):
            terminal.send("\x01\x0b" + query + "\r")
            terminal.require(lambda: terminal.frame_has("Filter: " + query))

        require_page("Language and startup")
        open_dialog()
        self.click_label(terminal, "Host features")
        terminal.send(b"Host features\r")
        terminal.require(lambda: terminal.frame_has("No matching options"))
        require_page("Language and startup")
        open_dialog()
        apply("ui_icons")
        terminal.require(
            lambda: (
                terminal.frame_has("Icon style")
                and not terminal.frame_has("Shell executable")
            )
        )
        open_dialog()
        terminal.send(b"\x01\x0bnot-applied")
        terminal.require(lambda: terminal.frame_has("not-applied"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: (
                terminal.frame_has("Filter: ui_icons")
                and not terminal.frame_has("not-applied")
            )
        )
        # Cancel from the sidebar leaves Up/Down navigating pages.
        terminal.send(b"\x1b[B")
        require_page("UI palette")
        terminal.require(
            lambda: terminal.frame_has("Background") and terminal.frame_has("Accent")
        )
        terminal.require(lambda: not terminal.frame_has("Filter: ui_icons"))
        open_dialog()
        apply("Accent")
        terminal.require(lambda: not terminal.frame_has("Comment 1"))
        terminal.send(b"\x1b[A")
        require_page("Language and startup")
        terminal.require(lambda: terminal.frame_has("Filter: ui_icons"))
        terminal.send(b"\r")
        open_dialog()
        terminal.send(b"\x01\x0btemporary")
        terminal.require(lambda: terminal.frame_has("temporary"))
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("Apply"))
        # Cancel from the body returns to the field: Enter opens its choices.
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("○ Nerd Font"))
        self.click_label(terminal, "UI palette")
        terminal.require(lambda: terminal.frame_has("Filter: Accent"))
        terminal.require(lambda: not terminal.frame_has("Comment 1"))
        self.click_label(terminal, "Input and display")
        terminal.require(
            lambda: (
                terminal.frame_has("Scrollback line budget")
                and not terminal.frame_has("Filter:")
            )
        )
        terminal.close_settings()
        self.assertEqual(self.rc.read_bytes(), original)

    def test_option_filter_matches_keys_and_draft_values_without_discarding_hidden_edits(
        self,
    ):
        """Apply and cancel field filters before explicitly saving a hidden edit."""
        original = "config.shell = 'initial-shell'\nconfig.log_level = 'INFO'\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=40)
        terminal.require(lambda: terminal.frame_has("⚙  Settings"))
        self.click_label(terminal, "Host features")
        terminal.require(lambda: terminal.frame_has("(OSC 8)"))
        self.click_label(terminal, "Language and startup")
        # Wait for the field's own value: the old page can still occupy lower
        # rows when the first part of the new page arrives through the PTY.
        self.click_label(terminal, "initial-shell")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        terminal.send(b"\x01\x0bfilter-draft-shell\r")
        terminal.require(lambda: terminal.frame_has("filter-draft-shell"))

        terminal.send(b"\x06ui_icons\r")
        terminal.require(
            lambda: (
                terminal.frame_has("Icon style")
                and not terminal.frame_has("Shell executable")
                and terminal.frame_has("Filter: ui_icons")
                and terminal.frame_has("Unsaved changes")
            )
        )
        self.click_field(terminal, "Icon style")
        self.click_label(terminal, "Nerd Font")
        terminal.require(lambda: terminal.frame_has("\uf013 Settings"))
        terminal.send(b"\x06\x01\x0bno-match-for-this-field")
        terminal.require(lambda: terminal.frame_has("no-match-for-this-field"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: (
                terminal.frame_has("Icon style")
                and not terminal.frame_has("no-match-for-this-field")
            )
        )

        terminal.send(b"\x06\x01\x0b" + b"q" * 400 + b"\r")
        terminal.require(
            lambda: (
                terminal.frame_has("No matching options") and terminal.frame_has("qqqq")
            )
        )
        self.assertEqual(sum("qqqq" in row for row in terminal.screen.display), 1)
        for heading in (
            "Logging",
            "Saved settings apply to your next",
            "Shell executable",
            "Enter the name or path",
        ):
            self.assertFalse(terminal.frame_has(heading))
        terminal.send(b"\x06\x01\x0bfilter-draft-shell\r")
        terminal.require(
            lambda: (
                terminal.frame_has("Shell executable")
                and not terminal.frame_has("Icon style")
            )
        )
        self.assertTrue(
            terminal.frame_has("Enter the name or path of the shell to start.")
        )
        self.assertIsNotNone(self.field_bounds(terminal, "Shell executable"))
        self.assertEqual(self.rc.read_text(), original)
        self.apply_action(terminal, "Save")
        terminal.close_settings()
        selected = load(self.rc)
        self.assertEqual(
            (selected.shell, selected.ui_icons, selected.log_level),
            ("filter-draft-shell", "nerdfont", "INFO"),
        )

    def test_mouse_palette_edits_are_written_only_by_the_save_button(self):
        """Click a family and shade, then verify the next session or the original bytes."""
        for button in ("Cancel", "Save"):
            with self.subTest(button=button):
                original = b"config.ui_palette = {'accent': 'orange-400', 'error': 'red-600'}\n"
                if button == "Save":
                    original += b"config.ui_icons = 'nerdfont'\n"
                self.rc.write_bytes(original)
                terminal = self.terminal("-c", rows=38)
                self.click_label(terminal, "UI palette")
                self.click_label(terminal, "Accent", same_line="orange-400")
                if button == "Save":
                    terminal.require(partial(terminal.frame_has, "\uf0da Accent"))
                    terminal.send(b"\r")
                    terminal.require(partial(terminal.frame_has, "\uf044 Accent"))
                    terminal.send(b"\x1b")
                    terminal.require(partial(terminal.frame_has, "\uf0da Accent"))
                    self.click_label(terminal, "\uf0da", same_line="Color family")
                    terminal.require(partial(terminal.frame_has, "amber-400"))
                    self.click_label(terminal, "\uf0d9", same_line="Color family")
                    terminal.require(partial(terminal.frame_has, "orange-400"))
                self.click_label(terminal, "Teal")
                terminal.require(partial(terminal.frame_has, "teal-400"))
                self.click_label(terminal, "700", same_line="900")
                terminal.require(
                    lambda terminal=terminal: (
                        terminal.frame_has("teal-700")
                        and not (
                            terminal.frame_has("✎ Accent")
                            or terminal.frame_has("\uf044 Accent")
                        )
                    )
                )
                selected = partial(self.label_cell, terminal, "700", "900")
                terminal.require(
                    lambda selected=selected: (
                        (cell := selected()) is not None and cell.bold
                    )
                )
                self.assertTrue(selected().underscore)
                terminal.require(partial(terminal.frame_has, "Unsaved changes"))
                self.assertEqual(self.rc.read_bytes(), original)
                self.apply_action(terminal, button)
                if button == "Save":
                    self.assertEqual(
                        load(self.rc).ui_palette,
                        {"accent": "teal-700", "error": "red-600"},
                    )
                else:
                    self.assertEqual(self.rc.read_bytes(), original)
                    self.assertTrue(terminal.frame_has("orange-400"))
                terminal.close_settings()

    def test_text_and_muted_roles_survive_dark_light_and_default_surfaces(self):
        """Compare the actual ink and background of chrome, fields and buttons."""

        def check(term, label, row, fg, bg):
            expected = fg.removeprefix("#"), bg.removeprefix("#"), False
            term.require(
                lambda: (
                    (cell := self.label_cell(term, label, row)) is not None
                    and (cell.fg, cell.bg, cell.reverse) == expected
                )
            )

        def focused(term, label, row):
            cell = self.label_cell(term, label, row)
            return cell is not None and cell.fg == "bef264"

        for fill, text, muted in (
            ("#253043", "#293448", "#334056"),
            ("#ebe4d7", "#e1dccc", "#d5cfc4"),
            ("default", "#776688", "#667788"),
        ):
            with self.subTest(fill=fill):
                palette = {
                    "selection": fill,
                    "text": text,
                    "muted": muted,
                    "accent4": "#b7c4dc",
                    "muted2": "#394151",
                }
                self.rc.write_text(
                    f"config.ui_palette = {palette!r}\nconfig.shell = '/bin/sh'\n"
                )
                term = self.terminal(
                    "-c",
                    rows=38,
                    cols=120,
                    environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
                )
                check(term, "Settings", "×", text, fill)
                term.require(
                    lambda t=term: self.label_cell(t, "Save", "<S>") is not None
                )
                label = self.label_cell(term, "Save", "<S>")
                bar = self.label_cell(term, "Ctrl +", "<S>")
                if label.reverse:
                    ink, chip = label.bg, label.fg
                else:
                    ink, chip = label.fg, label.bg
                self.assertEqual(
                    (ink, chip, bar.bg), (fill.removeprefix("#"), "b7c4dc", ink)
                )
                check(term, "Ctrl +", "<S>", "#b7c4dc", fill)
                check(term, "S>", "<S>", "#67e8f9", "#b7c4dc")
                check(term, "ish 2.0.0", "ish 2.0.0", muted, fill)
                term.send(b"\r")
                term.require(partial(focused, term, "Language and startup", "⚲"))
                check(term, "/bin/sh", "/bin/sh", text, fill)
                term.send(b"\t")
                term.require(partial(focused, term, "Cancel", "Save"))
                check(term, "Save", "Cancel", text, fill)
                term.close_settings()

    def test_secondary_accent_reaches_shortcuts_and_account_without_recoloring(self):
        """Read the requested ink from actual cells during editing and after restart."""
        self.rc.write_text("config.ui_palette = {'accent': 'lime-500'}\n")
        environment = {
            "LOGNAME": "ish-tester",
            "PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT",
        }
        term = self.terminal("-c", rows=45, cols=120, environment=environment)

        def ink(ui, label, row):
            cell = self.label_cell(ui, label, row)
            return cell.fg if cell else None

        term.require(lambda: ink(term, "S>", "<S>") == "67e8f9")
        self.click_label(term, "UI palette")
        self.click_label(term, "Accent 2", same_line="cyan-300")
        term.require(
            lambda: term.frame_has("› Accent 2") and term.frame_has("◂ Cyan ▸")
        )
        self.click_label(term, "Rose")
        term.require(lambda: term.frame_has("rose-300"))
        self.click_label(term, "700", same_line="900")
        term.require(
            lambda: term.frame_has("rose-700") and ink(term, "S>", "<S>") == "be123c"
        )
        self.apply_action(term, "Save")
        self.assertEqual(
            load(self.rc).ui_palette, {"accent": "lime-500", "accent2": "rose-700"}
        )

        self.click_label(term, "Language and startup")
        term.require(lambda: "Language and startup" in term.screen.display[2])
        term.send(b"\r\t")
        term.require(lambda: ink(term, "Cancel", "Save") == "84cc16")
        term.close_settings()

        help_ui = self.terminal("-h", environment=environment)
        help_ui.require(
            lambda: (
                ink(help_ui, "F>", "<F>") == "be123c"
                and ink(help_ui, "ish-tester", "ish 2.0.0") == "be123c"
            )
        )
        help_ui.close_settings()

    def test_palette_default_custom_hex_and_keyboard_editing(self):
        """Preview and commit named, host and direct colors on the real PTY."""
        original = b"config.ui_palette = {'accent': 'rose-600'}\n"
        self.rc.write_bytes(original)
        terminal = self.terminal("-c", rows=40, cols=110)
        self.click_label(terminal, "UI palette")
        self.click_label(terminal, "Accent", same_line="rose-600")
        terminal.send(b" \x1b[D")
        terminal.require(lambda: terminal.frame_has("rose-500"))
        sample = partial(self.label_cell, terminal, "500", "900")
        terminal.require(lambda: sample() is not None and sample().underscore)
        terminal.send(b"\x1b[B")  # Last named family -> Default.
        terminal.require(
            lambda: terminal.frame_has("Default") and not terminal.frame_has("900")
        )
        header = next(
            n for n, row in enumerate(terminal.screen.display) if "Color family" in row
        )
        self.assertFalse(terminal.screen.display[header + 1].partition("│")[2].strip())
        terminal.send(b"\x1b[B")  # Default -> Custom.
        terminal.require(lambda: terminal.frame_has(" HEX #"))
        terminal.send(b"12Z\r")
        terminal.require(
            lambda: (
                terminal.frame_has("HEX #12Z")
                and self.status_contains(terminal, "Enter six HEX digits")
            )
        )
        self.assertEqual(self.rc.read_bytes(), original)
        terminal.send(b"\x01\x0b1A2B3C\r\x13")
        terminal.require(lambda: terminal.frame_has("Changes saved"))
        self.assertEqual(load(self.rc).ui_palette, {"accent": "#1a2b3c"})
        self.click_label(terminal, "Accent", same_line="#1a2b3c")
        terminal.send(b"\r\x01\x7f")
        terminal.require(lambda: terminal.frame_has(" HEX #1a2b3c"))
        terminal.send(b"\x0b\x1b[200~#000000\x1b[201~")
        terminal.require(lambda: terminal.frame_has(" HEX #000000"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: terminal.frame_has("#1a2b3c") and not terminal.frame_has("000000")
        )
        self.click_label(terminal, "HEX #1a2b3c")
        terminal.send(b"\x01\x0bC0FFEE")
        terminal.require(lambda: terminal.frame_has("HEX #C0FFEE"))
        self.click_label(terminal, "Color family")
        terminal.require(
            lambda: (
                terminal.frame_has("HEX #c0ffee") and not terminal.frame_has("✎ Accent")
            )
        )
        self.assertEqual(load(self.rc).ui_palette, {"accent": "#1a2b3c"})
        self.click_label(terminal, "HEX #c0ffee")
        terminal.send(b"\x01\x0b123456")
        terminal.require(lambda: terminal.frame_has("HEX #123456"))
        self.click_label(terminal, "⚲", same_line="UI palette")
        terminal.require(lambda: terminal.frame_has("Apply"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: (
                terminal.frame_has("HEX #c0ffee") and not terminal.frame_has("Apply")
            )
        )
        self.click_label(terminal, "HEX #c0ffee")
        terminal.send(b"\x01\x0b12Q")
        terminal.require(lambda: terminal.frame_has("HEX #12Q"))
        self.click_label(terminal, "Color family")
        terminal.require(
            lambda: (
                terminal.frame_has("HEX #c0ffee")
                and not terminal.frame_has("✎ Accent")
                and self.status_contains(terminal, "Enter six HEX digits")
            )
        )
        self.click_label(terminal, "HEX #c0ffee")
        terminal.send(b"\x01\x0bFF33")
        terminal.require(lambda: terminal.frame_has("HEX #FF33"))
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: (
                terminal.frame_has("HEX #FF33")
                and terminal.frame_has("✎ Accent")
                and self.status_contains(terminal, "Enter six HEX digits")
            )
        )
        self.assertEqual(load(self.rc).ui_palette, {"accent": "#1a2b3c"})
        # Continue typing into the same field after the failed button click.
        terminal.send(b"99\x13")
        terminal.require(lambda: load(self.rc).ui_palette == {"accent": "#ff3399"})
        self.click_label(terminal, "HEX #ff3399")
        terminal.send(b"\x01\x0b0F1E2D")
        terminal.require(lambda: terminal.frame_has("HEX #0F1E2D"))
        self.click_label(terminal, "Error", same_line="rose-400")
        terminal.require(lambda: terminal.frame_has("› Error"))
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).ui_palette, {"accent": "#0f1e2d"})
        self.click_label(terminal, "Accent", same_line="#0f1e2d")
        self.click_label(terminal, "Default", same_line="Custom")
        terminal.send(b"\x13")
        terminal.require(lambda: load(self.rc).ui_palette == {"accent": "default"})
        terminal.close_settings()

    def test_hex_background_matches_the_rendered_shades_after_resize(self):
        """Match the visible shade strip in display and edit modes at two widths."""
        terminal = self.terminal("-c", rows=40, cols=110)
        self.click_label(terminal, "UI palette")

        def band(label):
            for y, row in enumerate(terminal.screen.display):
                if label not in row:
                    continue
                x = row.index(label)
                cells = terminal.screen.buffer[y]
                if cells[x].bg == "default":
                    return None
                left, right = x, x + 1
                while left > 0 and cells[left - 1].bg != "default":
                    left -= 1
                while right < terminal.cols and cells[right].bg != "default":
                    right += 1
                return left, right, y
            return None

        def expect_hex(bounds):
            def matches():
                result = band(" HEX #")
                if not result or result[:2] != bounds:
                    return False
                left, right, y = result
                return (
                    terminal.screen.display[y].index("HEX #") == left + 1
                    and len(
                        {terminal.screen.buffer[y][x].bg for x in range(left, right)}
                    )
                    == 1
                )

            terminal.require(matches)

        terminal.require(lambda: band("100") and terminal.frame_has("900"))
        wide = band("100")[:2]
        terminal.resize(40, 65)
        terminal.require(
            lambda: (
                band("100") and band("100")[1] < wide[1] and terminal.frame_has("900")
            )
        )
        narrow = band("100")[:2]
        self.click_label(terminal, "Custom", same_line="Default")
        expect_hex(narrow)
        terminal.send(b"3A7C91")
        terminal.require(lambda: terminal.frame_has(" HEX #3A7C91"))
        terminal.resize(40, 110)
        expect_hex(wide)
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has(" HEX #3a7c91"))
        expect_hex(wide)
        self.click_label(terminal, "HEX #3a7c91")
        terminal.require(lambda: terminal.frame_has("✎ Accent"))
        _, right, y = band(" HEX #")
        self.click_cell(terminal, right - 1, y)
        terminal.send(b"\x01\x0b123ABC")
        terminal.require(lambda: terminal.frame_has(" HEX #123ABC"))
        self.click_cell(terminal, right, y)
        terminal.require(
            lambda: (
                terminal.frame_has(" HEX #123abc")
                and not terminal.frame_has("✎ Accent")
            )
        )
        terminal.close_settings()

    def test_korean_palette_keeps_its_title_in_each_filter_state(self):
        """Find the color editor by its own title, narrow its roles, clear and restore it."""
        terminal = self.terminal(
            "-c",
            language="ko",
            rows=24,
            cols=64,
            environment={"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"},
        )

        def body():
            return "\n".join(
                row.partition("│")[2].strip() for row in terminal.screen.display[4:-4]
            )

        def set_filter(query):
            self.click_label(terminal, "⚲", same_line="UI 팔레트")
            terminal.require(
                lambda: any(
                    "적용" in row and "취소" in row for row in terminal.screen.display
                )
            )
            terminal.send("\x01\x0b" + query + "\r")
            terminal.require(
                lambda: (
                    not any(
                        "적용" in row and "취소" in row
                        for row in terminal.screen.display
                    )
                )
            )

        def assert_group():
            terminal.require(lambda: "역할별 색상" in body() and "색상 계열" in body())
            text = body()
            self.assertEqual(text.count("역할별 색상"), 1)
            self.assertLess(
                text.index("역할별 색상"), text.index("도움말과 설정창에서 바꿀 역할")
            )
            self.assertLess(
                text.index("도움말과 설정창에서 바꿀 역할"), text.index("강조")
            )

        description = (
            "도움말과 설정창에서 바꿀 역할을 선택하고 색상 계열과 명도를 고르세요."
        )

        def assert_narrow_entry():
            terminal.require(
                lambda: (
                    "옵션 필터" in terminal.screen.display[-1] and "› 강조" in body()
                )
            )
            # Compare the user's complete instruction across physical terminal rows.
            self.assertIn(
                "".join(("역할별 색상" + description).split()), "".join(body().split())
            )

        terminal.require(lambda: "옵션 필터" in terminal.screen.display[-1])
        terminal.send(b"\x1b[B\r")
        assert_narrow_entry()
        terminal.send(b"\x1b")
        terminal.require(
            lambda: self.label_cell(terminal, "UI 팔레트", "▸").fg == "bef264"
        )
        terminal.send(b"\x1b[A")
        terminal.require(
            lambda: "언어·시작 설정" in terminal.screen.display[2].partition("│")[2]
        )
        terminal.send(b"\x1b[B")
        terminal.require(lambda: "역할별 색상" in body())
        terminal.send(b"\r")
        assert_narrow_entry()
        terminal.send(b"\x1b[B")
        terminal.require(lambda: "› 강조색 2" in body())
        terminal.send(b"\x1b[A")
        terminal.require(lambda: "› 강조" in body())
        terminal.resize(39, 110)
        terminal.require(lambda: description in body())
        assert_group()
        self.assertIn("본문 글자", body())
        set_filter("역할별 색상")
        assert_group()
        self.assertIn("본문 글자", body())
        set_filter("강조")
        assert_group()
        self.assertNotIn("본문 글자", body())
        set_filter("no-such-role")
        terminal.require(lambda: "일치하는 옵션이 없습니다" in body())
        for hidden in (
            "역할별 색상",
            "도움말과 설정창에서 바꿀 역할",
            "색상 계열",
            "강조",
        ):
            self.assertNotIn(hidden, body())
        self.assertIn("UI 팔레트", terminal.screen.display[2].partition("│")[2])
        set_filter("")
        assert_group()
        self.assertIn("본문 글자", body())
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_track_click_and_resize_during_drag_do_not_activate_close(self):
        """Reach the end of a form and release over an action after resizing it."""
        terminal = self.terminal("-c", rows=28, cols=95)
        self.click_label(terminal, "Host features")
        terminal.require(lambda: terminal.frame_has("(OSC 8)"))
        x, _, bottom = self.scrollbar_at_top(terminal)
        # The final track cell jumps to the end, with the lower arrow left untouched.
        self.click_cell(terminal, x, bottom - 1)
        terminal.require(lambda: terminal.frame_has("DECRQM-2031"))
        terminal.send(f"\x1b[<0;{x + 1};{bottom}M\x1b[<32;1;1M")
        terminal.require(lambda: terminal.frame_has("(OSC 8)"))

        terminal.resize(34, 120)

        def close_button():
            title = terminal.screen.display[0]
            return title.index("×") + 1 if title.find("×") >= 95 else None

        terminal.require(close_button)
        terminal.send(f"\x1b[<0;{close_button()};1m\x1b[1;3F")
        terminal.require(lambda: terminal.frame_has("DECRQM-2031"))
        self.assertFalse(terminal.exited())
        terminal.close_settings()
        self.assertFalse(self.rc.exists())

    def test_sidebar_divider_highlights_drags_and_preserves_focus(self):
        """The divider resizes both windows without activating a control under its release."""

        def check(option):
            terminal = self.terminal(option, rows=32, cols=100)
            row = 4

            def divider():
                return terminal.screen.display[row].find("│")

            def pointer(button, x, y, suffix="M"):
                terminal.send(f"\x1b[<{button};{x + 1};{y + 1}{suffix}")

            terminal.require(lambda: divider() > 0)
            if option == "-c":
                self.click_field(terminal, "Shell executable")
                terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
            x = divider()
            normal = terminal.screen.buffer[row][x].fg
            pointer(35, x, row)
            terminal.require(lambda: terminal.screen.buffer[row][x].fg != normal)
            pointer(35, x + 1, row)
            terminal.require(lambda: terminal.screen.buffer[row][x].fg == normal)
            # The release can arrive before the moved divider has been painted.
            terminal.send(f"\x1b[<0;{x + 1};{row + 1}M\x1b[<32;41;8M\x1b[<0;41;8m")
            terminal.require(
                lambda: divider() == 40 and terminal.screen.buffer[row][40].fg != normal
            )
            if option == "-h":
                terminal.send(b"\x1b[1;5D")  # Focus is still in the sidebar.
                terminal.require(lambda: divider() == 39)
                terminal.finish()
                return

            pointer(0, 40, row)
            terminal.resize(32, 58)
            terminal.require(
                lambda: 0 < divider() < 30 and "×" in terminal.screen.display[0]
            )
            close = terminal.screen.display[0].index("×")
            pointer(0, close, 0, "m")
            terminal.resize(32, 100)
            terminal.require(
                lambda: divider() == 40 and "×" in terminal.screen.display[0][90:]
            )
            terminal.send(b"\x01\x0bafter-drag\r\x13")
            terminal.require(lambda: terminal.frame_has("Changes saved"))
            self.assertEqual(load(self.rc).shell, "after-drag")
            terminal.close_settings()

        for option in ("-c", "-h"):
            with self.subTest(option=option):
                check(option)

    def test_distant_field_filter_and_click_save_the_visible_feature(self):
        """Filtering from the sidebar exposes a distant option with its own mouse targets."""
        terminal = self.terminal("-c", rows=24, cols=89)
        terminal.require(lambda: terminal.frame_has("⚙  Settings"))
        self.click_label(terminal, "Host features")
        terminal.send(b"\x06Light / dark theme notifications\r")
        terminal.require(lambda: terminal.frame_has("DECRQM-2031"))
        self.click_field(terminal, "Light / dark theme notifications")
        self.click_label(terminal, "○ Off")
        self.apply_action(terminal, "Save")
        terminal.close_settings()
        self.assertEqual(load(self.rc).features["scheme_notifications"], "off")

    def test_long_choice_expands_and_wrapped_rows_select_the_original_value(self):
        """Select by clicking the second physical row of a long translated option."""
        first = "FIRST_" + "가" * 40 + "_ONE"
        second = "SECOND_" + "나" * 40 + "_TWO"
        root = self.home / "plugin/script/long_options"
        root.mkdir(parents=True)
        meta = {
            "version": "1",
            "category": "plugin",
            "settings": {
                "path": {
                    "title": "Which path",
                    "description": "Choose a path",
                    "default": first,
                    "choices": [first, second],
                }
            },
        }
        (root / "__init__.py").write_text("PLUGIN_META = " + repr(meta))
        terminal = self.terminal("-c", rows=36, cols=84)
        self.click_label(terminal, "long_options")
        terminal.require(
            lambda: any(
                "FIRST_" in row and "…" in row for row in terminal.screen.display
            )
        )
        self.click_field(terminal, "Which path")
        terminal.require(
            lambda: terminal.frame_has("_ONE") and terminal.frame_has("_TWO")
        )
        self.click_label(terminal, "_TWO")
        terminal.require(
            lambda: any(
                "SECOND_" in row and "…" in row for row in terminal.screen.display
            )
        )
        self.apply_action(terminal, "Save")
        self.assertEqual(load(self.rc).plugin_settings["long_options"]["path"], second)
        terminal.close_settings()

    def test_late_mouse_burst_does_not_reach_the_calling_shell(self):
        """Delay the final mouse fragments until shutdown and read the next real shell line."""
        off = b"\x1b[?1003l"
        query = b"\x1b[?1003$p"
        reply = b"\x1b[?1003;2$y"
        literal = b"35;60;62M stays literal"
        for option in ("-c", "-h"):
            with self.subTest(option=option):
                result = self.root / option
                term = self.terminal(option, parent_line=result)
                term.require(lambda term=term: query in term.output)
                if not term.host:
                    term.send(reply)
                term.require(lambda term=term: "×" in term.screen.display[0])
                col = wcswidth(term.screen.display[0].partition("×")[0]) + 1
                start = len(term.output)
                term.send(f"\x1b[<0;{col};1M\x1b[<0;{col};1m\x1b[<35;60;")
                term.require(lambda term=term, start=start: off in term.output[start:])
                term.send(b"62M" + b"\x1b[<35;60;61M" * 20)
                if term.host:
                    term.require(lambda term=term: b"CALLER_READY" in term.output)
                    term.send(literal + b"\n")
                else:
                    term.require(
                        lambda term=term, start=start: query in term.output[start:]
                    )
                    # One write includes both the host acknowledgement and the caller's line.
                    term.send(reply + literal + b"\n")
                term.finish(key=b"")
                self.assertEqual(result.read_bytes(), literal)
                self.assertIn(query, term.output[start:])

    def test_focus_reports_in_close_batch_do_not_echo_before_terminal_release(self):
        """Actual PTYs deliver focus events on either side of the close keystroke."""
        for option in ("-c", "-h"):
            with self.subTest(option=option):
                terminal = self.terminal(option)
                terminal.require(lambda term=terminal: "×" in term.screen.display[0])
                column = wcswidth(terminal.screen.display[0].partition("×")[0]) + 1
                click = f"\x1b[<0;{column};1M\x1b[<0;{column};1m".encode()
                terminal.finish(key=b"\x1b[I\x1b[O" + click + b"\x1b[O")
                self.assertNotIn(b"^[[O", terminal.output)
                self.assertNotIn(b"\x1b[?1004h", terminal.output)

    def test_long_field_preview_wraps_only_while_editing(self):
        """Read the entire CJK value and keep its bytes after clicking Save."""
        value = "HEAD" + "가나" * 24 + "TAIL"
        self.rc.write_text(f"config.shell = {value!r}\n")
        terminal = self.terminal("-c", rows=40, cols=115)
        terminal.require(
            lambda: any("HEAD" in r and "…" in r for r in terminal.screen.display)
        )
        self.click_field(terminal, "Shell executable")
        terminal.require(
            lambda: terminal.frame_has("HEAD") and terminal.frame_has("TAIL")
        )
        self.click_action(terminal, "Save")
        terminal.require(lambda: not terminal.frame_has("TAIL"))
        self.assertEqual(load(self.rc).shell, value)
        terminal.send(b"\r")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ Shell executable") and terminal.frame_has("TAIL")
            )
        )
        terminal.close_settings()

    def test_horizontal_pan_preserves_the_edit_cursor_in_a_long_field(self):
        """Read the end of the shell field, then insert at the original beginning."""
        value = "HEAD" + "a" * 100 + "TAIL"
        self.rc.write_text(f"config.ui_icons = 'nerdfont'\nconfig.shell = {value!r}\n")
        terminal = self.terminal("-c", rows=28, cols=88)
        terminal.require(lambda: terminal.frame_has("\uf013 Settings"))
        terminal.send(b"\r\x1b[B\r\x01")
        terminal.require(
            lambda: (
                terminal.frame_has("HEAD")
                and any(
                    "\uf044" in row and "Shell executable" in row
                    for row in terminal.screen.display
                )
            )
        )
        self.click_label(terminal, "\u2192", same_line="Alt +")
        terminal.require(
            lambda: (
                terminal.frame_has("EAD")
                and not terminal.frame_has("HEAD")
                and not terminal.frame_has("\uf044 Shell executable")
            )
        )
        self.click_label(terminal, "\u2190", same_line="Alt +")
        terminal.require(lambda: terminal.frame_has("HEAD"))
        # The footer click finished editing. Resume at the original insertion point.
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("\uf044 Shell executable"))
        terminal.send(b"\x1b[1;3C" * len(value))
        terminal.require(
            lambda: terminal.frame_has("TAIL") and not terminal.frame_has("HEAD")
        )
        terminal.send(b"X\r")
        self.apply_action(terminal, "Save")
        terminal.close_settings()
        self.assertEqual(load(self.rc).shell, "X" + value)

    def test_numeric_field_and_edit_title_icons_do_not_enter_the_saved_value(self):
        """Type into numeric fields under both icon styles and inspect the next session."""

        def check(mode, number, edit):
            self.rc.write_text(
                f"config.ui_icons = {mode!r}\nconfig.escape_delay = 0.075\n"
                "config.scrollback_lines = 321\nconfig.scrollback_bytes = 123456\n"
            )
            terminal = self.terminal("-c", rows=42)
            title = "Esc / Alt wait (s)"

            def title_line():
                return next(
                    (row for row in terminal.screen.display if title in row), ""
                )

            terminal.require(
                lambda: (
                    title_line()
                    and any(
                        number in row and "0.075" in row
                        for row in terminal.screen.display
                    )
                )
            )
            self.assertNotIn(edit, title_line())
            self.click_field(terminal, title)
            terminal.require(lambda: edit in title_line())
            self.assertFalse(
                any(
                    edit in row
                    for row in terminal.screen.display
                    if "0.075" in row or "Shell executable" in row
                )
            )
            terminal.send(b"\x01\x0b0.125\r")
            terminal.require(
                lambda: (
                    edit not in title_line()
                    and any(
                        number in row and "0.125" in row
                        for row in terminal.screen.display
                    )
                )
            )
            self.apply_action(terminal, "Save")
            self.assertEqual(load(self.rc).escape_delay, 0.125)
            self.assertNotIn(number, self.rc.read_text())
            self.assertNotIn(edit, self.rc.read_text())
            self.click_label(terminal, "Input and display")
            terminal.send(b"\r\x06scrollback\r")
            terminal.require(
                lambda: all(
                    any(
                        number in row and value in row
                        for row in terminal.screen.display
                    )
                    for value in ("321", "123456")
                )
            )
            terminal.close_settings()

        for mode, number, edit in (
            ("unicode", "↕", "✎"),
            ("nerdfont", "\uf0dc", "\uf044"),
        ):
            with self.subTest(mode=mode):
                check(mode, number, edit)

    def test_only_the_value_box_starts_mouse_editing(self):
        """Clicks outside a value activate at most its pane; Enter still starts editing."""
        original = "config.shell = 'safe-shell'\nconfig.escape_delay = 0.075\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=40, cols=110)
        title = "Shell executable"
        terminal.require(lambda: self.field_bounds(terminal, title))
        left, right, heading, value_row = self.field_bounds(terminal, title)
        # First click is in an inactive pane; the others arrive in the active pane.
        for x, y in (
            (left, heading),
            (left + 4, heading + 1),
            (left, value_row),
            (right, value_row),
            (right + 2, value_row),
            (left + 2, value_row + 1),
        ):
            self.click_cell(terminal, x, y)
            terminal.send(b"poison")
        terminal.send(b"\x13")
        self.click_label(terminal, "⚲", same_line="Language and startup")
        terminal.require(lambda: terminal.frame_has("Apply"))
        self.assertEqual(self.rc.read_text(), original)
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("Apply"))
        self.click_field(terminal, title)
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        terminal.send(b"\x01\x0btyped-shell\r")
        terminal.require(
            lambda: (
                terminal.frame_has("typed-shell")
                and not terminal.frame_has("✎ Shell executable")
            )
        )
        terminal.send(b"\r")
        terminal.require(lambda: terminal.frame_has("✎ Shell executable"))
        terminal.send(b"\r\x13")
        terminal.require(lambda: "config.shell = 'typed-shell'" in self.rc.read_text())
        # An intervening Enter ends the edit even if the next mouse click arrives
        # within PTK's double-click interval. Both replacements must reach the field.
        left, _, _, y = self.field_bounds(terminal, title)
        click = f"\x1b[<0;{left + 3};{y + 1}M\x1b[<0;{left + 3};{y + 1}m"
        terminal.send(click + "\x01\x0bfirst\r" + click + "\x01\x0bsecond\r\x13")
        terminal.require(lambda: load(self.rc).shell == "second")
        self.click_field(terminal, "Icon style")
        terminal.require(
            lambda: (
                terminal.frame_has("Nerd Font") and terminal.frame_has("✎ Icon style")
            )
        )
        terminal.close_settings()

    def test_outside_click_with_motion_finishes_the_field_on_first_release(self):
        """A held motion in blank space is one blur; the next key cannot edit the old field."""
        self.rc.write_text("config.shell = 'old-shell'\nconfig.escape_delay = 0.02\n")
        term = self.terminal("-c", rows=40, cols=115)
        for title, typed, shift in (
            ("Shell executable", "accepted-shell", 0),
            ("Esc / Alt wait (s)", "9.1", 1),
        ):
            self.click_field(term, title)
            term.send("\x01\x0b" + typed)
            term.require(lambda typed=typed: term.frame_has(typed))
            _, right, _, row = self.field_bounds(term, title)
            x, y = right + 4, row + 1
            term.send(
                f"\x1b[<0;{x};{y}M\x1b[<32;{x + shift};{y}M\x1b[<0;{x + shift};{y}m"
            )
            term.send(b"POISON")
            term.require(lambda title=title: not term.frame_has("✎ " + title))
            self.assertFalse(term.frame_has("POISON"))
        term.require(lambda: self.status_contains(term, "must be between"))
        self.apply_action(term, "Save")
        self.assertEqual(
            (load(self.rc).shell, load(self.rc).escape_delay), ("accepted-shell", 0.02)
        )
        term.close_settings()

    def test_click_outside_ends_text_edits_and_runs_the_clicked_target(self):
        """Keep the field draft on blur and deliver the same click to the next target."""
        original = "config.shell = 'abcd'\nconfig.escape_delay = 0.075\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=40, cols=110)
        shell = "Shell executable"
        delay = "Esc / Alt wait (s)"
        self.click_field(terminal, shell)
        terminal.require(lambda: terminal.frame_has("✎ " + shell))
        terminal.send(b"\r\r")  # Reopen by keyboard, then position the cursor by mouse.
        left, right, heading, row = self.field_bounds(terminal, shell)
        self.click_cell(terminal, left + 4, row)
        terminal.send(b"X")
        terminal.require(lambda: terminal.frame_has("abXcd"))
        # Finishing a selection outside the box is still part of its original press.
        terminal.send(
            f"\x1b[<0;{left + 4};{row + 1}M\x1b[<32;{right + 3};{row + 1}M"
            f"\x1b[<0;{right + 3};{row + 1}m\x01\x0bdragged-shell"
        )
        terminal.require(lambda: terminal.frame_has("dragged-shell"))

        self.click_cell(terminal, left, heading)
        terminal.require(lambda: not terminal.frame_has("✎ " + shell))
        terminal.send(b"poison")
        self.click_field(terminal, shell)
        terminal.require(lambda: terminal.frame_has("✎ " + shell))
        self.click_cell(terminal, right + 2, row)
        terminal.require(lambda: not terminal.frame_has("✎ " + shell))
        self.assertFalse(terminal.frame_has("poison"))
        self.assertEqual(self.rc.read_text(), original)

        self.click_field(terminal, shell)
        terminal.require(lambda: terminal.frame_has("✎ " + shell))
        bar, _, bottom = self.scrollbar_at_top(terminal)
        self.click_cell(terminal, bar, bottom)
        terminal.require(
            lambda: (
                not terminal.frame_has("✎ " + shell)
                and self.field_bounds(terminal, shell)[2] == heading - 1
            )
        )
        self.click_field(terminal, shell)
        terminal.require(lambda: terminal.frame_has("✎ " + shell))
        self.click_field(terminal, delay)
        terminal.require(lambda: terminal.frame_has("✎ " + delay))
        self.assertFalse(terminal.frame_has("✎ " + shell))
        terminal.send(b"\x01\x0b0.125")
        terminal.require(lambda: terminal.frame_has("0.125"))
        self.apply_action(terminal, "Save")
        self.assertFalse(terminal.frame_has("✎ " + delay))
        saved = load(self.rc)
        self.assertEqual((saved.shell, saved.escape_delay), ("dragged-shell", 0.125))
        terminal.close_settings()

    def test_invalid_input_restores_accepted_drafts_only_on_blur_or_escape(self):
        """Keep invalid edits correctable at Enter and Save, including by mouse."""
        original = "config.shell = 'disk-shell'\nconfig.escape_delay = 0.02\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=40, cols=115)
        shell = "Shell executable"
        delay = "Esc / Alt wait (s)"

        self.click_field(terminal, shell)
        terminal.send(b"\x01\x0baccepted-shell\r")
        terminal.require(
            lambda: (
                terminal.frame_has("accepted-shell")
                and not terminal.frame_has("✎ " + shell)
            )
        )
        self.click_field(terminal, shell)
        terminal.send(b"\x01\x0b")
        terminal.require(lambda: not terminal.frame_has("accepted-shell"))
        self.click_field(terminal, delay)
        terminal.require(
            lambda: (
                terminal.frame_has("accepted-shell")
                and terminal.frame_has("✎ " + delay)
                and self.status_contains(terminal, "nonempty executable")
            )
        )
        terminal.send(b"\x01\x0b0.125\r")
        terminal.require(
            lambda: terminal.frame_has("0.125") and not terminal.frame_has("✎ " + delay)
        )

        # Esc cancels one edit. Both of the previously accepted drafts survive.
        self.click_field(terminal, shell)
        terminal.send(b"\x01\x0bdiscard-this-shell")
        terminal.require(lambda: terminal.frame_has("discard-this-shell"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: (
                terminal.frame_has("accepted-shell")
                and not terminal.frame_has("discard-this-shell")
            )
        )
        self.click_field(terminal, delay)
        terminal.send(b"\x01\x0b0.25")
        terminal.require(lambda: terminal.frame_has("0.25"))
        terminal.send(b"\x1b")
        terminal.require(
            lambda: terminal.frame_has("0.125") and not terminal.frame_has("✎ " + delay)
        )

        self.click_field(terminal, delay)
        terminal.send(b"\x01\x0b3.5\r")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ " + delay)
                and terminal.frame_has("3.5")
                and self.status_contains(terminal, "must be between")
            )
        )
        # A non-finite number has the same error even though it parses as a float.
        terminal.send(b"\x01\x0bNaN\x13")
        terminal.require(
            lambda: terminal.frame_has("✎ " + delay) and terminal.frame_has("NaN")
        )
        self.assertEqual(self.rc.read_text(), original)
        self.click_label(terminal, "✎", same_line="Language and startup")
        terminal.require(
            lambda: (
                terminal.frame_has("0.125")
                and not terminal.frame_has("✎ " + delay)
                and self.status_contains(terminal, "before opening the file")
                and not self.status_contains(terminal, "must be between")
            )
        )
        self.assertEqual(self.rc.read_text(), original)
        self.click_field(terminal, delay)
        terminal.send(b"\x01\x0b9.9")
        terminal.require(lambda: terminal.frame_has("9.9"))
        self.click_label(terminal, "<S> Save", same_line="Ctrl +")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ " + delay)
                and terminal.frame_has("9.9")
                and self.status_contains(terminal, "must be between")
            )
        )
        # Request a different result in the same input queue to observe completion
        # even when the prior error was already visible before the footer click.
        terminal.send(b"\x01\x0b0.2\r")
        terminal.require(
            lambda: terminal.frame_has("0.2") and not terminal.frame_has("✎ " + delay)
        )
        self.assertEqual(self.rc.read_text(), original)
        self.click_field(terminal, delay)
        terminal.send(b"\x01\x0bnot-a-number")
        terminal.require(lambda: terminal.frame_has("not-a-number"))
        self.click_action(terminal, "Save")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ " + delay) and terminal.frame_has("not-a-number")
            )
        )
        # A successful correction after clicking Save proves the input owns focus.
        terminal.send(b"\x01\x0b0.375\x13")
        terminal.require(lambda: load(self.rc).escape_delay == 0.375)
        self.assertEqual(load(self.rc).shell, "accepted-shell")
        terminal.close_settings()

    def test_empty_required_path_reverts_but_optional_log_path_can_be_disabled(self):
        """Use each setting's own empty-value policy on a direct click into the next field."""
        original = "config.cwd = 'saved-start'\nconfig.log_file = 'old.log'\n"
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=56, cols=120)
        cwd, log = "Starting directory", "Log file"
        self.click_field(terminal, "Shell executable")
        terminal.send(b"\x01\x0b~:no-ish-home:/bin/sh\r")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ Shell executable")
                and terminal.frame_has("~:no-ish-home:/bin/sh")
                and self.status_contains(terminal, "Cannot resolve shell")
            )
        )
        terminal.send(b"\x1b")
        terminal.require(lambda: not terminal.frame_has("~:no-ish-home:/bin/sh"))
        self.click_field(terminal, cwd)
        terminal.send(b"\x01\x0bnext-start\r")
        terminal.require(
            lambda: (
                terminal.frame_has("next-start") and not terminal.frame_has("✎ " + cwd)
            )
        )
        self.click_field(terminal, cwd)
        terminal.send(b"\x01\x0b")
        terminal.require(lambda: not terminal.frame_has("next-start"))
        self.click_field(terminal, log)
        terminal.require(
            lambda: (
                terminal.frame_has("next-start")
                and terminal.frame_has("✎ " + log)
                and self.status_contains(terminal, "cwd must be a path")
            )
        )
        terminal.send(b"\x01\x0b")
        terminal.require(lambda: not terminal.frame_has("old.log"))
        self.click_label(terminal, log)
        terminal.require(
            lambda: not terminal.frame_has("✎ " + log) and terminal.frame_has("Off")
        )
        self.assertEqual(self.rc.read_text(), original)
        self.apply_action(terminal, "Save")
        saved = load(self.rc)
        self.assertEqual(saved.cwd, self.home / "next-start")
        self.assertIsNone(saved.log_file)
        terminal.close_settings()

    def test_numeric_arrows_step_the_draft_and_respect_public_limits(self):
        """Spin seconds and integers inside their fields before saving the final values."""
        original = (
            "config.escape_delay = 0.01525\n"
            "config.scrollback_lines = 3\n"
            "config.scrollback_bytes = 9223372036854775806\n"
        )
        self.rc.write_text(original)
        terminal = self.terminal("-c", rows=42)
        seconds = "Esc / Alt wait (s)"
        terminal.require(lambda: terminal.frame_has("⚙  Settings"))
        self.click_field(terminal, seconds)
        terminal.send(b"\r\x1b[A\r")
        terminal.require(lambda: terminal.frame_has("✎ Starting directory"))
        terminal.send(b"\r")
        self.click_field(terminal, seconds)

        def expect(title, value):
            def shown():
                box = self.field_bounds(terminal, title)
                if not box:
                    return False
                left, right, _, y = box
                content = "".join(
                    terminal.screen.buffer[y][x].data for x in range(left + 1, right)
                )
                return content.rstrip().removesuffix("↕").strip() == value

            terminal.require(shown)

        terminal.send(b"\x1b[A")
        expect(seconds, "0.01625")
        terminal.send(b"\x1b[B")
        expect(seconds, "0.01525")
        terminal.send(b"\x01\x0b0.005\x1b[B\x1b[A")
        expect(seconds, "0.006")
        terminal.send(b"\x01\x0b2.0\x1b[A\x1b[B")
        expect(seconds, "1.999")
        for invalid in ("NaN", "2.5", "wrong"):
            terminal.send("\x01\x0b" + invalid + "\x1b[A\x1b[B!")
            expect(seconds, invalid + "!")
        terminal.send(b"\r\x13")
        terminal.require(
            lambda: (
                self.status_contains(terminal, "must be between")
                and terminal.frame_has("✎ " + seconds)
            )
        )
        self.assertEqual(self.rc.read_text(), original)
        self.click_field(terminal, seconds)
        terminal.send(b"\x01\x0b\x1b[A\x1b[B0.025\r")
        expect(seconds, "0.025")
        self.click_label(terminal, "Input and display")
        terminal.send(b"\r\x06scrollback\r")
        lines, size = "Scrollback line budget", "Scrollback byte budget (0: disabled)"
        self.click_field(terminal, lines)
        terminal.send(b"\x1b[A")
        expect(lines, "4")
        terminal.send(b"\r")
        terminal.require(lambda: not terminal.frame_has("✎ " + lines))
        self.click_field(terminal, lines)
        terminal.send(b"\x01\x0b3.5\r")
        terminal.require(
            lambda: (
                terminal.frame_has("✎ " + lines)
                and self.status_contains(terminal, "integer")
            )
        )
        expect(lines, "3.5")
        self.click_label(terminal, lines)
        expect(lines, "4")
        terminal.require(lambda: not terminal.frame_has("✎ " + lines))
        self.click_field(terminal, lines)
        terminal.send(b"\x01\x0b0\x1b[B\x1b[A\r")
        expect(lines, "1")
        self.click_field(terminal, size)
        terminal.send(b"\x1b[A\x1b[A")
        expect(size, "9223372036854775807")
        terminal.send(b"\x1b[B\r\x13")
        terminal.require(lambda: load(self.rc).scrollback_lines == 1)
        config = load(self.rc)
        self.assertEqual(
            (config.escape_delay, config.scrollback_bytes), (0.025, 9223372036854775806)
        )
        terminal.close_settings()

    def test_each_named_color_family_is_accepted_in_the_rc(self):
        """Check the public color names without deriving expectations from the UI."""
        families = (
            "red orange amber yellow lime green emerald teal cyan sky blue indigo "
            "violet purple fuchsia pink rose slate gray zinc neutral stone"
        ).split()
        for family in families:
            with self.subTest(family=family):
                colors = {"accent": f"{family}-100", "error": f"{family}-900"}
                self.rc.write_text(f"config.ui_palette = {colors!r}\n")
                self.assertEqual(load(self.rc).ui_palette, colors)

    def test_reloading_shows_progress_and_ignores_duplicate_actions_until_ready(self):
        """A burst of reload and save input evaluates the rc only once more."""
        evaluations = self.root / "evaluations"
        self.rc.write_text(
            f"with open({str(evaluations)!r}, 'a') as counter:\n"
            "    counter.write('read\\n')\n"
        )
        terminal = self.terminal("-c")
        terminal.require(lambda: terminal.frame_has("⚙  Settings"), timeout=45)
        self.assertEqual(evaluations.read_text().splitlines(), ["read"])
        terminal.send(b"\x12\x12\x13\x03")
        terminal.require(lambda: terminal.frame_has("reloaded from disk"))
        self.assertIn(b"Working", terminal.output)
        self.assertEqual(evaluations.read_text().splitlines(), ["read", "read"])
        self.assertFalse(terminal.exited())
        terminal.close_settings()

    def test_failed_rc_preserves_language_precedence_without_using_invalid_fields(self):
        """A broken setting keeps valid rc language; invalid language falls back to LANG."""
        cases = (
            ("ko", "C.UTF-8", None, "⚙  설정"),
            ("../ko", "ko_KR.UTF-8", None, "⚙  설정"),
            ("ko", "ko_KR.UTF-8", "en", "⚙  Settings"),
            ("../ko", "C.UTF-8", None, "⚙  Settings"),
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
                terminal.close_settings()
                self.assertEqual(self.rc.read_text(), original)

    def test_title_close_after_resize_restores_termios_and_keeps_the_file(self):
        """Close a resized settings window with the pointer, including during editing."""
        self.rc.write_text("config.lang = 'en'\n")
        original = self.rc.read_bytes()
        for extra_input in (b"", b"\r\x1b[B\rtyped"):
            with self.subTest(editing=bool(extra_input)):
                terminal = self.terminal("-c")
                terminal.require(
                    lambda terminal=terminal: terminal.frame_has("⚙  Settings"),
                    timeout=45,
                )
                terminal.send(extra_input)
                terminal.resize(20, 86)
                terminal.require(
                    lambda terminal=terminal: terminal.frame_has("⚙  Settings")
                )
                terminal.close_settings()
                self.assertEqual(self.rc.read_bytes(), original)

    def test_external_change_blocks_save_until_reload(self):
        """A wrapped error keeps its cause, original file and both action buttons."""
        self.rc.write_text("config.lang = 'en'\n")
        terminal = self.terminal("-c", rows=25, cols=72)
        terminal.require(lambda: terminal.frame_has("⚙  Settings"), timeout=45)
        external = "config.lang = 'en'\n# changed elsewhere\n"
        self.rc.write_text(external)
        # Initially selected English. Open the list and choose Korean.
        terminal.send(b"\r\r\x1b[B\r\t\x1b[C\r")
        terminal.require(
            lambda: self.status_contains(
                terminal,
                "The settings file changed outside this window. Reload it before saving.",
            )
        )
        rows = self.status_rows(terminal)
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(rows), rows)
        action_row = self.assert_actions_at_right(terminal, "Cancel", "Save")
        first_button = wcswidth(action_row.rpartition("Cancel")[0])
        for line in terminal.screen.display[-len(rows) - 2 : -3]:
            self.assertLess(wcswidth(line.rstrip()), first_button)
        self.assertFalse(terminal.exited())
        self.assertEqual(self.rc.read_text(), external)
        terminal.send(b"\x12")
        terminal.require(lambda: self.status_contains(terminal, "reloaded from disk"))
        terminal.close_settings()
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
        terminal.require(lambda: terminal.frame_has("⚙  Settings"), timeout=45)
        terminal.send(b"\x05")
        terminal.require(lambda: terminal.frame_has("reloaded from disk"))
        terminal.close_settings()
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
        terminal.require(lambda: terminal.frame_has("⚙  Settings"), timeout=45)
        terminal.send(b"\x05")
        terminal.require(pid_file.exists)
        editor_pid = int(pid_file.read_text())
        terminal.send(b"\x03")
        terminal.require(
            lambda: terminal.exited() or terminal.frame_has("reloaded from disk")
        )
        if terminal.exited():
            terminal.finish(key=b"")
        else:
            terminal.close_settings()
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
