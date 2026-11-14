"""Exercise cursor-key replay through the engine and a pipe-backed ish editor."""

import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.enums import EditingMode
from prompt_toolkit.history import InMemoryHistory, ThreadedHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.typeahead import get_typeahead, store_typeahead
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from ish.shell.engine import InteractiveShell
from ish.ui.prompt import Prompt


class DelayedHistory(InMemoryHistory):
    """Pause history loading until the test releases it."""

    def __init__(self):
        """Keep explicit signals for the start and completion of the load."""
        super().__init__(["delayed"])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self):
        """Use the normal history implementation after the delay."""
        self.started.set()
        await self.release.wait()
        async for entry in super().load():
            yield entry


class NativeCursorKeyTests(unittest.IsolatedAsyncioTestCase):
    """Inspect accepted commands and the native bytes still owned by the shell."""

    async def asyncSetUp(self):
        """Use the real prompt loop and replace only the shell's terminal transport."""
        self.source = self.enterContext(create_pipe_input())
        self.enterContext(patch("ish.ui.prompt.get_builtins", return_value=[]))
        self.prompt = Prompt(
            shell="bash",
            input=self.source,
            output=DummyOutput(),
            history=InMemoryHistory(["first", "most recent"]),
        )
        self.prompt.exit_command = "finish"
        self.shell = self.prompt.interactive_shell
        self.shell.continuation_active = asyncio.Event()
        self.shell.command_done_event = asyncio.Event()
        self.shell.command_done_event.set()
        self.shell._accepted_prompt_id = self.shell._prompt_id = (
            self.shell._context_id
        ) = 1
        self.enterContext(
            patch.object(InteractiveShell, "_collect_typeahead", return_value=False)
        )
        self.enterContext(patch.object(InteractiveShell, "_write"))
        self.enterContext(patch.object(InteractiveShell, "_drain_output"))
        self.submissions = []
        self.enterContext(
            patch.object(InteractiveShell, "_exec", self.record_submission)
        )

    async def record_submission(self, command):
        """Record the command and the remaining native bytes at the handoff."""
        self.submissions.append((command, bytes(self.shell._returned_input)))

    async def run_editor(self, *, native=b"", fresh=b"", saved=(), live=b"finish\r"):
        """Run until the live input's exit command follows all pending commands."""
        self.shell._returned_input.extend(native)
        self.shell.typeahead.extend(fresh)
        store_typeahead(self.source, list(saved))
        self.source.send_bytes(live)
        await asyncio.wait_for(self.shell._prompt(), 10)
        return [command for command, _ in self.submissions]

    async def test_saved_keys_follow_returned_history_recall(self):
        """Append already-read keys after the returned arrow recalls history."""
        saved = [KeyPress("!"), KeyPress(Keys.Enter, "\r")]
        commands = await self.run_editor(native=b"\x1b[A", saved=saved)
        self.assertEqual(commands, [b"most recent!\n"])

    async def test_returned_search_enter_submits_the_matching_command(self):
        """Use the selected command for Enter while a native search is focused."""
        commands = await self.run_editor(native=b"\x12firs\n")
        self.assertEqual(commands, [b"first\n"])

    async def test_live_enter_can_finish_a_returned_search(self):
        """Keep a native-started search usable across the editor handoff."""
        commands = await self.run_editor(native=b"\x12fir", live=b"s\rfinish\r")
        self.assertEqual(commands, [b"first\n"])

    async def test_saved_up_and_enter(self):
        """Recall history from keys saved after the preceding prompt accepted."""
        saved = [KeyPress(Keys.Up, "\x1b[A"), KeyPress(Keys.Enter, "\r")]
        commands = await self.run_editor(saved=saved)
        self.assertEqual(commands, [b"most recent\n"])

    async def test_threaded_history(self):
        """Use entries supplied by prompt-toolkit's threaded history backend."""
        self.prompt.default_buffer.history = ThreadedHistory(
            InMemoryHistory(["threaded"])
        )
        commands = await self.run_editor(native=b"\x1b[A\n")
        self.assertEqual(commands, [b"threaded\n"])

    async def test_slow_history_keeps_native_saved_and_live_order(self):
        """Wait for an asynchronous history even when a live Enter is readable."""
        history = DelayedHistory()
        self.prompt.default_buffer.history = history
        read = asyncio.Event()
        original = self.source.read_keys

        def read_keys():
            """Record when the live suffix has actually been read."""
            keys = original()
            read.set()
            return keys

        with patch.object(self.source, "read_keys", read_keys):
            task = asyncio.create_task(
                self.run_editor(
                    native=b"\x1b[A", saved=[KeyPress("S")], live=b"L\rfinish\r"
                )
            )
            try:
                await asyncio.wait_for(read.wait(), 5)
                self.assertTrue(history.started.is_set())
                self.assertFalse(self.submissions)
            finally:
                history.release.set()
                commands = await task
        self.assertEqual(commands, [b"delayedSL\n"])

    async def test_eof_releases_pending_history(self):
        """A closed input ends the prompt and leaves saved keys available."""
        history = DelayedHistory()
        self.prompt.default_buffer.history = history
        saved = [KeyPress(Keys.Up, "\x1b[A")]
        store_typeahead(self.source, saved)
        task = asyncio.create_task(
            self.prompt.prompt_async(
                pre_run=lambda: self.prompt.start_typeahead(
                    lambda: None, bytearray(), bytearray()
                )
            )
        )
        await asyncio.wait_for(history.started.wait(), 5)
        self.source.close()
        with self.assertRaises(EOFError):
            await asyncio.wait_for(task, 5)
        self.assertEqual(get_typeahead(self.source), saved)
        self.assertIsNone(self.prompt.input.hold_keys)

    async def test_live_utf8_fragment_stays_after_slow_history(self):
        """Older literal bytes cannot consume a newer character's first byte."""
        history = DelayedHistory()
        buffer = self.prompt.default_buffer
        buffer.history = history
        original = self.source.read_keys

        def read_keys():
            """Release history only after the decoder has seen the first byte."""
            keys = original()
            history.release.set()
            return keys

        def finish_character(buffer):
            """Send the remaining bytes after the older literal suffix is restored."""
            if buffer.text.endswith("!"):
                self.source.send_bytes(b"\x98\x83\rfinish\r")

        buffer.on_text_changed += finish_character
        with patch.object(self.source, "read_keys", read_keys):
            commands = await self.run_editor(native=b"\x1b[A!", live=b"\xe2")
        self.assertEqual(commands, ["delayed!☃\n".encode()])

    async def test_interrupt_does_not_wait_for_history(self):
        """Cancel the pending navigation and give post-interrupt text to the next prompt."""
        self.prompt.default_buffer.history = DelayedHistory()
        commands = await self.run_editor(native=b"\x1b[A", live=b"\x03finish\r")
        self.assertEqual(commands, [])

    async def test_printable_input_does_not_wait_for_history(self):
        """Ordinary typeahead can submit while an asynchronous provider is stalled."""
        self.prompt.default_buffer.history = DelayedHistory()
        saved = [KeyPress("c"), KeyPress(Keys.Enter, "\r")]
        commands = await self.run_editor(native=b"a", fresh=b"b", saved=saved)
        self.assertEqual(commands, [b"abc\n"])

    async def test_replay_exception_keeps_saved_keys_once(self):
        """Propagate a broken key handler and leave later keys for the next reader."""

        def fail(event):
            """Simulate a key binding whose implementation raises."""
            raise ValueError("failed binding")

        self.prompt.set_key("up", handler=fail)
        saved = [KeyPress("z")]
        store_typeahead(self.source, saved)
        fresh = bytearray(b"\x1b[A")
        with self.assertRaisesRegex(ValueError, "failed binding"):
            await asyncio.wait_for(
                self.prompt.prompt_async(
                    pre_run=lambda: self.prompt.start_typeahead(
                        lambda: self.prompt.replay_raw(fresh, native=False),
                        bytearray(),
                        fresh,
                    )
                ),
                5,
            )
        self.assertEqual(get_typeahead(self.source), saved)
        self.assertIsNone(self.prompt.input.hold_keys)

    async def test_pending_live_input_is_bounded(self):
        """Abort an overflowing wait through the same prompt result as other errors."""
        self.prompt.default_buffer.history = DelayedHistory()
        with (
            patch("ish.ui.prompt.TYPEAHEAD_LIMIT_BYTES", 2),
            self.assertRaisesRegex(BufferError, "typeahead limit"),
        ):
            await self.run_editor(native=b"\x1b[A", live=b"abcd")
        self.assertIsNone(self.prompt.input.hold_keys)

    async def test_native_up_and_enter(self):
        """An application-mode Up and a native newline submit the recalled line."""
        commands = await self.run_editor(native=b"\x1bOA\n")
        self.assertEqual(commands, [b"most recent\n"])

    async def test_fresh_up_and_enter(self):
        """New keys retained before PTY delivery can also recall and accept history."""
        commands = await self.run_editor(fresh=b"\x1b[A\r")
        self.assertEqual(commands, [b"most recent\n"])

    async def test_accepted_native_enter_leaves_suffix_at_handoff(self):
        """A canonical newline leaves the following foreground bytes untouched."""
        commands = await self.run_editor(native=b"before\nafter\n")
        self.assertEqual(commands, [b"before\n", b"after\n"])
        self.assertEqual(self.submissions[0][1], b"after\n")

    async def test_returned_text_and_fresh_text_have_separate_bindings(self):
        """Only text belonging to the live editor runs its printable binding."""
        self.prompt.set_key("x", handler=lambda e: e.current_buffer.insert_text("X"))
        commands = await self.run_editor(native=b"xx plain text\n", fresh=b"x\r")
        self.assertEqual(commands, [b"xx plain text\n", b"X\n"])

    async def test_custom_up_only_applies_when_the_live_key_starts(self):
        """Keep an arrow native even when its last byte arrives at the live prompt."""
        self.prompt.set_key(
            "up", handler=lambda event: event.current_buffer.insert_text("<live>")
        )
        commands = await self.run_editor(native=b"\x1b[", live=b"A\r\x1b[A\rfinish\r")
        self.assertEqual(commands, [b"most recent\n", b"<live>\n"])

    async def test_native_enter_accepts_without_a_live_shortcut(self):
        """Use the default submission for previously unconsumed canonical Enter."""
        self.prompt.set_key(
            "enter", handler=lambda event: event.app.exit(result="finish")
        )
        commands = await self.run_editor(native=b"one\n", live=b"\r")
        self.assertEqual(commands, [b"one\n"])

    async def test_emacs_repeat_argument_applies_to_one_character(self):
        """Resume bulk insertion only after Meta-3 repeats its following key."""
        keys = b"\x1b3xy"
        live = await self.run_editor(live=keys + b"\rfinish\r")
        self.submissions.clear()
        queued = await self.run_editor(native=keys + b"\n")
        self.assertEqual(live, [b"xxxy\n"])
        self.assertEqual(queued, live)

    async def test_vi_replace_and_append_across_insert_runs(self):
        """Interpret vi navigation, replace-one and append between printable runs."""
        self.prompt.editing_mode = EditingMode.VI
        keys = b"abcdef\x1b0rXAend"
        live = await self.run_editor(live=keys + b"\rfinish\r")
        self.submissions.clear()
        queued = await self.run_editor(native=keys + b"\n")
        self.assertEqual(live, [b"Xbcdefend\n"])
        self.assertEqual(queued, live)

    async def test_submitted_literal_tail_does_not_dispatch_keys(self):
        """Restore previously accepted text with its shortcuts and escapes intact."""
        self.prompt.set_key("x", handler=lambda e: e.current_buffer.insert_text("X"))
        literal = b"xx\x1b[A"
        self.shell._literal_input.extend(literal)
        commands = await self.run_editor(live=b"\rfinish\r")
        self.assertEqual(commands, [literal + b"\n"])

    async def test_partial_submitted_unicode_remains_literal(self):
        """Complete a submitted character without invoking its custom printable binding."""
        scalar = "한".encode()
        self.prompt.set_key(
            "한", handler=lambda e: e.current_buffer.insert_text("HOTKEY")
        )
        self.shell._literal_input.extend(b"echo " + scalar[:1])
        commands = await self.run_editor(live=scalar[1:] + b"\rfinish\r")
        self.assertEqual(commands, [b"echo " + scalar + b"\n"])

    async def test_partial_submitted_unicode_in_vi_navigation(self):
        """Completing literal input cannot turn its last character into a vi action."""
        scalar = "한".encode()
        self.prompt.editing_mode = EditingMode.VI

        def start():
            self.prompt.app.vi_state.input_mode = InputMode.NAVIGATION
            self.prompt.feed_literal_input(b"echo " + scalar[:2])

        self.source.send_bytes(scalar[2:] + b"\r")
        result = await asyncio.wait_for(self.prompt.prompt_async(pre_run=start), 2)
        self.assertEqual(result, "echo 한")

    async def test_unread_literal_fragment_transfers_original_bytes(self):
        """A later stdin owner receives undecoded source bytes without paste framing."""
        for prefix, suffix in [(b"\xed", b"\x95\x9c"), (b"\xe2\x82", b"X")]:
            with self.subTest(prefix=prefix):
                self.prompt.feed_literal_input(prefix)
                self.source.send_bytes(suffix)
                store_typeahead(self.source, self.prompt.input.read_keys())
                self.assertEqual(self.prompt.take_typeahead(), prefix + suffix)
                self.assertEqual(self.prompt.take_typeahead(), b"")

    async def test_unknown_sequence_uses_ordinary_ptk_fallback(self):
        """Compare even an unmapped escape with the running editor's interpretation."""
        sequence = b"abc\x1b[99~\x1b[DX"
        ordinary = await self.run_editor(live=sequence + b"\rfinish\r")
        self.submissions.clear()
        returned = await self.run_editor(native=sequence + b"\n")
        self.assertTrue(ordinary)
        self.assertEqual(returned, ordinary)

    async def test_bracketed_paste_is_one_literal_event(self):
        """A marked paste inserts its contents without invoking user bindings."""
        self.prompt.set_key("x", handler=lambda e: e.current_buffer.insert_text("X"))
        raw = b"x\x1b[Atext"
        commands = await self.run_editor(native=b"\x1b[200~" + raw + b"\x1b[201~\n")
        self.assertEqual(commands, [raw + b"\n"])
