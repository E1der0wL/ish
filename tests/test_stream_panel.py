"""Observe the real PTK panel's display, editing, focus and bounded output."""

import asyncio
import contextlib
import gc
import io
import json
import tempfile
import unittest
import warnings
from pathlib import Path

import pyte
from examples.stream_plugins.stream_bar.stream_bar import setup as setup_bar
from examples.stream_plugins.stream_popup.stream_popup import create
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from ish.lang import i18n
from ish.stream.extensions import Extensions
from ish.stream.panel import Panel
from ish.stream.ui import PopupView


class PanelTests(unittest.IsolatedAsyncioTestCase):
    """Exercise public panel input/output through PTK, independent of shell state."""

    async def asyncSetUp(self):
        """Start a hidden panel and collect only externally published effects."""
        self.frames = []
        self.focus = []
        self.results = []
        self.remainders = []
        self.errors = []
        self.failed = asyncio.Event()
        self.changed = asyncio.Event()

        def painted(frame):
            self.frames.append(frame)
            self.changed.set()

        self.painted = painted
        self.host_output = io.StringIO()
        self.panel = None
        content = Extensions()
        setup_bar(content)
        await self.configure(bar=content.bar, popup=create)

    async def configure(self, **content):
        """Replace the public panel with explicitly supplied plugin content."""
        if self.panel is not None:
            await self.panel.close()

        def failed(error):
            self.errors.append(error)
            self.failed.set()

        self.panel = Panel(
            self.painted,
            on_focus=self.focus.append,
            on_result=self.results.append,
            on_remainder=self.remainders.append,
            on_error=failed,
            **content,
        )
        with contextlib.redirect_stdout(self.host_output):
            await self.panel.start(50, 5)

    async def asyncTearDown(self):
        """Close the application without writing terminal teardown sequences."""
        with contextlib.redirect_stdout(self.host_output):
            await self.panel.close()
        self.assertEqual(self.host_output.getvalue(), "")

    async def frame_matching(self, predicate):
        """Use a deadline only to fail a missing externally visible result."""
        async with asyncio.timeout(3):
            while True:
                if self.frames and predicate(self.frames[-1]):
                    return self.frames[-1]
                self.changed.clear()
                await self.changed.wait()

    async def test_hidden_panel_has_no_output_or_input_ownership(self):
        """An unshown panel must not alter display or consume native input."""
        self.assertEqual(self.frames, [])
        self.assertFalse(self.panel.open_popup())
        self.assertFalse(self.panel.feed(b"echo native\r"))
        self.assertEqual(self.focus, [])

    async def test_panel_has_no_builtin_bar_or_popup(self):
        """Without a content plugin the renderer creates no decoration or editor."""
        await self.configure()
        self.panel.show()
        frame = await self.frame_matching(lambda value: not value.popup)
        self.assertTrue(all(not line.strip() for line in frame.lines))
        self.assertFalse(self.panel.open_popup())
        self.assertFalse(self.panel.feed(b"native\r"))
        self.assertEqual(self.focus, [])

    async def test_plugin_bar_callback_and_popup_content_render(self):
        """Content comes from external factories, including a live formatted bar."""
        status = ["FIRST"]

        def popup(accept):
            field = TextArea(
                height=1,
                multiline=False,
                accept_handler=lambda buffer: accept("plugin:" + buffer.text),
            )
            return PopupView(
                HSplit([Window(FormattedTextControl("Custom view"), height=1), field]),
                field,
            )

        await self.configure(bar=lambda: [("bold", status[0])], popup=popup)
        self.panel.show()
        await self.frame_matching(lambda value: "FIRST" in value.lines[-1])
        status[0] = "SECOND"
        self.panel.show()
        await self.frame_matching(lambda value: "SECOND" in value.lines[-1])
        self.assertTrue(self.panel.open_popup())
        self.panel.feed("한글\r".encode())
        self.assertEqual(self.results, ["plugin:한글"])

    async def test_sample_content_uses_language_selected_after_module_import(self):
        """A selected locale reaches the real plugin setup and popup factory."""
        previous = i18n.current_lang, i18n.base_path, i18n.message
        self.addCleanup(setattr, i18n, "current_lang", previous[0])
        self.addCleanup(setattr, i18n, "base_path", previous[1])
        self.addCleanup(setattr, i18n, "message", previous[2])
        temporary = tempfile.TemporaryDirectory(prefix="ish-panel-language-")
        self.addCleanup(temporary.cleanup)
        i18n.base_path = Path(temporary.name)
        i18n.load("ko", create=False)
        content = Extensions()
        setup_bar(content)
        await self.configure(bar=content.bar, popup=create)
        self.panel.show()
        await self.frame_matching(
            lambda frame: "ish | 기본 셸 | F2 팝업" in "\n".join(frame.lines)
        )
        self.assertTrue(self.panel.open_popup())
        await self.frame_matching(
            lambda frame: "플러그인 팝업 | Enter 확인" in "\n".join(frame.lines)
        )
        self.panel.feed("한글 입력\r".encode())
        self.assertEqual(self.results, ["한글 입력"])
        self.assertEqual(self.remainders, [])
        (i18n.base_path / "ko.json").write_text(
            json.dumps(
                {
                    "stream_sample_bar": "사용자 하단 바",
                    "stream_sample_popup": "사용자 팝업",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        i18n.load("ko", create=False)
        content = Extensions()
        setup_bar(content)
        await self.configure(bar=content.bar, popup=create)
        self.panel.show()
        await self.frame_matching(
            lambda frame: "사용자 하단 바" in "\n".join(frame.lines)
        )
        self.assertTrue(self.panel.open_popup())
        await self.frame_matching(lambda frame: "사용자 팝업" in "\n".join(frame.lines))
        self.panel.feed("다른 입력\r".encode())
        self.assertEqual(self.results, ["한글 입력", "다른 입력"])
        self.assertEqual(self.remainders, [])

    async def test_custom_popup_key_returns_exact_native_suffix(self):
        """A plugin may accept with Tab without losing following bytes in that read."""

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("tab", eager=True)
            def selected(event):
                accept(field.text)

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.assertTrue(self.panel.open_popup())
        suffix = b"echo native\r\xff"
        self.panel.feed(b"chosen\t" + suffix)
        self.assertEqual(self.results, ["chosen"])
        self.assertEqual(self.remainders, [suffix])
        self.assertEqual(self.focus, [True, False])

    async def test_old_plugin_callback_cannot_close_a_new_popup(self):
        """Each new view receives a callback tied to only its own input lifetime."""
        callbacks = []

        def popup(accept):
            callbacks.append(accept)
            field = TextArea(height=1)
            return PopupView(field, field)

        await self.configure(popup=popup)
        self.panel.show()
        self.assertTrue(self.panel.open_popup())
        self.assertTrue(callbacks[0]("first"))
        self.assertTrue(self.panel.open_popup())
        self.assertFalse(callbacks[0]("stale"))
        self.assertTrue(self.panel.popup)
        self.assertTrue(callbacks[1]("second"))
        self.assertEqual(self.results, ["first", "second"])
        self.assertEqual(self.focus, [True, False, True, False])

    async def test_invalid_factory_does_not_claim_native_input(self):
        """Malformed plugin content fails before the coordinator grants focus."""
        await self.configure(popup=lambda accept: object())
        self.panel.show()
        with self.assertRaisesRegex(TypeError, "PopupView"):
            self.panel.open_popup()
        self.assertFalse(self.panel.popup)
        self.assertFalse(self.panel.feed(b"native input"))
        self.assertEqual(self.focus, [])

    async def test_bar_render_error_disables_frames_and_reports_once(self):
        """A failing dynamic bar cannot escape through PTK's scheduled redraw."""
        broken = [False]

        def bar():
            if broken[0]:
                raise ValueError("bar failed")
            return "working"

        await self.configure(bar=bar)
        self.panel.show()
        await self.frame_matching(lambda value: "working" in value.lines[-1])
        count = len(self.frames)
        broken[0] = True
        self.panel.show()
        async with asyncio.timeout(3):
            await self.failed.wait()
        self.assertEqual([str(error) for error in self.errors], ["bar failed"])
        self.assertEqual(len(self.frames), count)
        self.assertFalse(self.panel.open_popup())
        self.assertFalse(self.panel.feed(b"native input"))

    async def test_nested_popup_render_error_releases_focus(self):
        """Failures inside external widget content use the same UI error boundary."""

        def popup(accept):
            def text():
                raise RuntimeError("popup render failed")

            field = TextArea(height=1)
            return PopupView(HSplit([Window(FormattedTextControl(text)), field]), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        async with asyncio.timeout(3):
            await self.failed.wait()
        self.assertEqual([str(error) for error in self.errors], ["popup render failed"])
        self.assertEqual(self.focus, [True, False])
        self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [])

    async def test_binding_error_discards_remaining_popup_input(self):
        """Failing plugin input must not replay the rest of its read as shell code."""

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("tab", eager=True)
            def failed(event):
                raise ValueError("popup binding failed")

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"abc\techo must-not-run\r")
        self.assertEqual(
            [str(error) for error in self.errors], ["popup binding failed"]
        )
        self.assertEqual(self.focus, [True, False])
        self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [])
        self.assertEqual(self.remainders, [])

    async def test_async_binding_error_releases_focus_without_console_output(self):
        """An awaited plugin failure uses the same boundary as synchronous input."""
        loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: loop_errors.append(context))

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("tab", eager=True)
            async def failed(event):
                await asyncio.sleep(0)
                raise ValueError("async popup binding failed")

            return PopupView(HSplit([field], key_bindings=bindings), field)

        try:
            await self.configure(popup=popup)
            self.panel.show()
            self.panel.open_popup()
            self.panel.feed(b"abc\techo must-not-run\r")
            async with asyncio.timeout(3):
                await self.failed.wait()
            await asyncio.sleep(0)
            self.assertEqual(
                [str(error) for error in self.errors], ["async popup binding failed"]
            )
            self.assertEqual(self.focus, [True, False])
            self.assertFalse(self.panel.popup)
            self.assertEqual(self.results, [])
            self.assertEqual(self.remainders, [])
            self.assertEqual(loop_errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_async_binding_accepts_the_current_popup_once(self):
        """Wrapping background work preserves a successful PTK async binding."""
        accepted = asyncio.Event()

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("tab", eager=True)
            async def selected(event):
                await asyncio.sleep(0)
                accept(field.text)
                accepted.set()

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"chosen\t")
        async with asyncio.timeout(3):
            await accepted.wait()
        self.assertEqual(self.results, ["chosen"])
        self.assertEqual(self.focus, [True, False])
        self.assertEqual(self.errors, [])
        self.assertFalse(self.panel.feed(b"native input"))

    async def test_retired_popup_tasks_cannot_fail_a_new_popup(self):
        """Late failures and their child tasks keep the old popup's lifetime."""
        started, release = asyncio.Event(), asyncio.Event()
        tasks, loop_errors = [], []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: loop_errors.append(context))

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(
                height=1,
                multiline=False,
                accept_handler=lambda buffer: accept(buffer.text),
            )

            async def child():
                await asyncio.sleep(0)
                raise ValueError("retired child failed")

            @bindings.add("tab", eager=True)
            async def delayed(event):
                tasks.append(asyncio.current_task())
                started.set()
                await release.wait()
                tasks.append(event.app.create_background_task(child()))
                raise ValueError("retired popup failed")

            return PopupView(HSplit([field], key_bindings=bindings), field)

        try:
            await self.configure(popup=popup)
            self.panel.show()
            self.panel.open_popup()
            self.panel.feed(b"old\t")
            async with asyncio.timeout(3):
                await started.wait()
            self.panel.close_popup()
            self.assertTrue(self.panel.open_popup())
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            self.assertTrue(self.panel.popup)
            self.panel.feed(b"new\r")
            self.assertEqual(self.results, [None, "new"])
            self.assertEqual(self.errors, [])
            self.assertEqual(loop_errors, [])
        finally:
            release.set()
            loop.set_exception_handler(previous)

    async def test_factory_background_work_uses_the_current_panel(self):
        """Factories get the owning PTK app before their view has acquired focus."""
        release = asyncio.Event()
        applications, tasks, loop_errors = [], [], []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: loop_errors.append(context))

        async def work():
            await release.wait()
            raise ValueError("current factory task failed")

        def popup(accept):
            applications.append(get_app())
            tasks.append(get_app().create_background_task(work()))
            field = TextArea(height=1)
            return PopupView(field, field)

        try:
            await self.configure(popup=popup)
            self.panel.show()
            self.assertTrue(self.panel.open_popup())
            self.assertIs(applications[0], self.panel.app)
            release.set()
            async with asyncio.timeout(3):
                await self.failed.wait()
            self.assertEqual(
                [str(error) for error in self.errors], ["current factory task failed"]
            )
            self.assertEqual(self.focus, [True, False])
            self.assertFalse(self.panel.popup)
            self.assertEqual(loop_errors, [])
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            loop.set_exception_handler(previous)

    async def test_retired_factory_task_cannot_fail_a_new_popup(self):
        """Task ownership starts during construction, before the popup is focused."""
        release = asyncio.Event()
        tasks = []

        async def work():
            await release.wait()
            raise ValueError("retired factory task failed")

        def popup(accept):
            if not tasks:
                tasks.append(get_app().create_background_task(work()))
            field = TextArea(
                height=1,
                multiline=False,
                accept_handler=lambda buffer: accept(buffer.text),
            )
            return PopupView(field, field)

        await self.configure(popup=popup)
        self.panel.show()
        self.assertTrue(self.panel.open_popup())
        self.panel.close_popup()
        self.assertTrue(self.panel.open_popup())
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"new\r")
        self.assertEqual(self.results, [None, "new"])
        self.assertEqual(self.errors, [])

    async def test_failed_factory_descendants_cannot_fail_a_later_popup(self):
        """Construction failure retires work, including children created later."""
        release = asyncio.Event()
        tasks = []

        async def child():
            raise ValueError("failed factory child")

        async def work():
            await release.wait()
            tasks.append(get_app().create_background_task(child()))
            raise ValueError("failed factory parent")

        def popup(accept):
            if not tasks:
                tasks.append(get_app().create_background_task(work()))
                raise ValueError("factory construction failed")
            field = TextArea(
                height=1,
                multiline=False,
                accept_handler=lambda buffer: accept(buffer.text),
            )
            return PopupView(field, field)

        await self.configure(popup=popup)
        self.panel.show()
        with self.assertRaisesRegex(ValueError, "factory construction failed"):
            self.panel.open_popup()
        self.assertEqual(self.focus, [])
        self.assertTrue(self.panel.open_popup())
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"new\r")
        self.assertEqual(self.results, ["new"])
        self.assertEqual(self.errors, [])

    async def test_shutdown_cancels_async_binding_without_ui_failure(self):
        """PTK still owns task cancellation when the panel application stops."""
        started, cancelled = asyncio.Event(), asyncio.Event()

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("tab", eager=True)
            async def waiting(event):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\t")
        async with asyncio.timeout(3):
            await started.wait()
            await self.panel.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.focus, [True, False])

    async def test_task_cancelled_before_start_does_not_leak_a_coroutine(self):
        """An immediately cancelled public PTK task leaves no unawaited work."""
        tasks = []
        ran = []

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            async def work():
                ran.append(True)

            @bindings.add("tab", eager=True)
            def cancel(event):
                task = event.app.create_background_task(work())
                task.cancel()
                tasks.append(task)

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", RuntimeWarning)
            self.panel.feed(b"\t")
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            tasks.clear()
            gc.collect()
        self.assertEqual(ran, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(observed, [])

    async def test_shutdown_rejects_tasks_spawned_by_cancelled_bindings(self):
        """A cancelled plugin's finally block cannot extend the stopped UI lifetime."""
        started = asyncio.Event()
        child_started, children = [], []

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            async def child():
                child_started.append(True)
                await asyncio.Event().wait()

            @bindings.add("tab", eager=True)
            async def waiting(event):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    children.append(event.app.create_background_task(child()))

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\t")
        async with asyncio.timeout(3):
            await started.wait()
            await self.panel.close()
        self.assertEqual(child_started, [])
        self.assertEqual(len(children), 1)
        self.assertTrue(children[0].cancelled())
        self.assertEqual(self.errors, [])

    async def test_only_reserved_rows_are_painted_with_default_background(self):
        """Panel painting preserves rows above and below its rectangle."""
        self.panel.show()
        frame = await self.frame_matching(lambda value: not value.popup)
        self.assertIn("ish stream | native shell", frame.lines[-1])
        screen = pyte.Screen(50, 12)
        stream = pyte.ByteStream(screen)
        for row in range(1, 13):
            stream.feed(f"\x1b[{row};1Hkeep row {row}".encode())
        old = screen.display.copy()
        payload = frame.render(5)
        stream.feed(payload)
        self.assertEqual(screen.display[:4], old[:4])
        self.assertEqual(screen.display[9:], old[9:])
        self.assertIn("ish stream | native shell", screen.display[8])
        self.assertEqual(screen.buffer[8][0].bg, "default")
        for forbidden in (b"\x1b[?", b"\x1b]", b"\x1bP", b"\x1b[2J"):
            self.assertNotIn(forbidden, payload)

    async def test_actual_ptk_editing_returns_popup_text_once(self):
        """Only the popup consumes text; PTK editing produces the accepted result."""
        self.panel.show()
        self.assertTrue(self.panel.open_popup())
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.focus, [True])
        self.assertTrue(self.panel.feed("hello 한글".encode()))
        self.assertTrue(self.panel.feed(b"\x7f!"))
        frame = await self.frame_matching(lambda value: "hello 한!" in value.lines[1])
        self.assertTrue(frame.popup)
        self.assertEqual(frame.cursor[1], 1)
        self.assertTrue(self.panel.feed(b"\r"))
        self.assertEqual(self.results, ["hello 한!"])
        self.assertEqual(self.focus, [True, False])
        self.assertFalse(self.panel.feed(b"native"))
        self.assertFalse(self.panel.close_popup("stale"))
        self.assertEqual(self.results, ["hello 한!"])

    async def test_cancelled_input_cannot_reach_next_popup(self):
        """Partial UTF-8 and escape fragments belong to the cancelled popup."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"old\xe3")
        self.panel.close_popup()
        self.panel.open_popup()
        self.panel.feed(b"new\rSHOULD_NOT_LEAK")
        self.assertEqual(self.results, [None, "new"])
        self.assertEqual(self.remainders, [b"SHOULD_NOT_LEAK"])
        self.panel.open_popup()
        self.panel.feed(b"\r")
        self.assertEqual(self.results, [None, "new", ""])

    async def test_arrows_and_fragmented_unicode_use_ptk_key_parser(self):
        """A split arrow sequence must edit the popup instead of cancelling it."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"ab\x1b[")
        self.panel.feed(b"D")
        text = "界".encode()
        self.panel.feed(text[:1])
        self.panel.feed(text[1:])
        self.panel.feed(b"\r")
        self.assertEqual(self.results, ["a界b"])

    async def test_hide_revokes_focus_and_suppresses_queued_frames(self):
        """Leaving the prompt cancels UI before any subsequent input routing."""
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        count = len(self.frames)
        self.panel.hide()
        self.assertFalse(self.panel.popup)
        self.assertFalse(self.panel.feed(b"shell input"))
        self.assertEqual(self.results, [None])
        self.assertEqual(self.focus, [True, False])
        await asyncio.sleep(0)
        self.assertEqual(len(self.frames), count)

    async def test_resize_clips_wide_cells_and_cancels_too_small_popup(self):
        """Narrow viewports cannot write beyond the caller's reserved columns."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed("界界界".encode())
        self.panel.resize(7, 4)
        frame = await self.frame_matching(lambda value: value.columns == 7)
        screen = pyte.Screen(15, 10)
        stream = pyte.ByteStream(screen)
        stream.feed(b"\x1b[4;8HKEEP")
        stream.feed(frame.render(3))
        self.assertEqual(
            "".join(screen.buffer[3][col].data for col in range(7, 11)), "KEEP"
        )
        self.panel.resize(7, 2)
        self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [None])
        self.assertFalse(self.panel.open_popup())

    async def test_escape_interrupt_and_eof_cancel_only_popup(self):
        """Popup cancel keys never become shell interrupts or terminal output."""
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        self.panel.feed(b"\x1b")
        # This is a user-visible latency budget, independent of the configured
        # timer. The previous parser + key-binding waits took about 1.5 seconds.
        async with asyncio.timeout(0.35):
            await self.frame_matching(lambda value: not value.popup)
        self.assertEqual(self.results, [None])
        for key in (b"\x03", b"\x04"):
            self.panel.open_popup()
            self.assertTrue(self.panel.feed(key))
            self.assertFalse(self.panel.popup)
        self.assertEqual(self.results, [None, None, None])
        self.assertEqual(self.focus, [True, False, True, False, True, False])

    async def test_accept_and_cancel_return_the_original_unconsumed_suffix(self):
        """One read can end popup input and retain exact native typeahead bytes."""
        self.panel.show()
        tail = b"next command\r\n\xe3\x81\x82\xff"
        for key, result in (
            (b"\r", "value"),
            (b"\n", "value"),
            (b"\x03", None),
            (b"\x04", None),
        ):
            with self.subTest(key=key):
                self.panel.open_popup()
                self.panel.feed(b"value" + key + tail)
                self.assertEqual(self.results[-1], result)
                self.assertEqual(self.remainders[-1], tail)
                self.assertFalse(self.panel.popup)
        self.assertEqual(len(self.remainders), 4)

    async def test_paste_newlines_remain_popup_data_until_explicit_accept(self):
        """Multiline paste cannot become a native command through suffix routing."""
        self.panel.show()
        self.panel.open_popup()
        text = "first\nsecond 한글\x03\x04"
        self.panel.feed(b"\x1b[200~" + text.encode() + b"\x1b[201~")
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.results, [])
        self.assertEqual(self.remainders, [])
        self.panel.feed(b"\rAFTER")
        self.assertEqual(self.results, [text])
        self.assertEqual(self.remainders, [b"AFTER"])

    async def test_alt_editing_does_not_cancel_or_release_popup_input(self):
        """Escape plus a character keeps PTK's normal Meta-key interpretation."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\x1bz")
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"one two\x1bb")
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.remainders, [])
        self.panel.feed(b"X\rTAIL")
        self.assertEqual(self.results, ["one Xtwo"])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_split_alt_and_arrow_keys_retain_editing_and_native_tail(self):
        """Read boundaries do not turn complete Meta or cursor keys into cancel."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"one two\x1b")
        await asyncio.sleep(0)
        self.panel.feed(b"bX\x1b[")
        await asyncio.sleep(0)
        self.panel.feed(b"DZ\rTAIL\xff")
        self.assertEqual(self.results, ["one ZXtwo"])
        self.assertEqual(self.remainders, [b"TAIL\xff"])

    async def test_escape_delay_can_allow_slow_split_alt_input(self):
        """A longer configured ambiguity window preserves delayed Meta suffixes."""
        await self.configure(popup=create, escape_delay=0.3)
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        self.panel.feed(b"one two\x1b")
        await asyncio.sleep(0.1)
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"bX\rTAIL")
        self.assertEqual(self.results, ["one Xtwo"])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_plugin_alt_binding_keeps_custom_result_and_native_suffix(self):
        """A plugin's Meta binding still wins over the unknown-Meta fallback."""

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("escape", "p")
            def selected(event):
                accept("plugin:" + field.text)

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"chosen\x1b")
        await asyncio.sleep(0)
        self.panel.feed(b"pNATIVE")
        self.assertEqual(self.results, ["plugin:chosen"])
        self.assertEqual(self.remainders, [b"NATIVE"])

    async def test_escape_timeout_does_not_shorten_plugin_key_chords(self):
        """Ambiguous terminal bytes and deliberate multi-key chords stay separate."""

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("c-x", "c-y")
            def selected(event):
                accept("chord")

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        self.panel.feed(b"\x18")
        await asyncio.sleep(0.15)
        self.panel.feed(b"\x19TAIL")
        self.assertEqual(self.results, ["chord"])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_alt_escape_keeps_the_complete_key_binding(self):
        """A second Escape completes Meta-Escape instead of cancelling alone."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\x1b\x1b")
        await asyncio.sleep(0.15)
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"value\r")
        self.assertEqual(self.results, ["value"])

        def popup(accept):
            bindings = KeyBindings()
            field = TextArea(height=1)

            @bindings.add("escape", "escape")
            def selected(event):
                accept("meta-escape")

            return PopupView(HSplit([field], key_bindings=bindings), field)

        await self.configure(popup=popup)
        self.panel.show()
        self.panel.open_popup()
        await self.frame_matching(lambda value: value.popup)
        self.panel.feed(b"\x1b\x1b")
        async with asyncio.timeout(0.35):
            await self.frame_matching(lambda value: not value.popup)
        self.assertEqual(self.results, ["value", "meta-escape"])

    async def test_partial_csi_timeout_is_not_a_standalone_escape(self):
        """A parser flush yielding Meta-[ remains popup-owned input."""
        self.panel.show()
        self.panel.open_popup()
        self.panel.feed(b"\x1b[")
        await asyncio.sleep(0.15)
        self.assertTrue(self.panel.popup)
        self.panel.feed(b"value\rTAIL")
        self.assertEqual(self.results, ["value"])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_split_paste_end_survives_the_escape_timeout(self):
        """A quiet gap inside a paste cannot dispatch its Escape as a cancel."""
        self.panel.show()
        self.panel.open_popup()
        text = "first\nsecond 한글\x03\x04"
        self.panel.feed(b"\x1b[200~" + text.encode() + b"\x1b[")
        await asyncio.sleep(0.15)
        self.assertTrue(self.panel.popup)
        self.assertEqual(self.results, [])
        self.panel.feed(b"201~\rTAIL")
        self.assertEqual(self.results, [text])
        self.assertEqual(self.remainders, [b"TAIL"])

    async def test_large_paste_does_not_block_a_pipe_or_reach_shell(self):
        """A popup receives a bracketed paste through the PTK editing path."""
        self.panel.show()
        self.panel.open_popup()
        text = "word " * 2000
        self.assertTrue(self.panel.feed(b"\x1b[200~" + text.encode() + b"\x1b[201~"))
        self.panel.feed(b"\r")
        self.assertEqual(self.results, [text])


if __name__ == "__main__":
    unittest.main()
