"""Verify that every authorized PTK edit returns literal shell text."""

import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ish.shell.handoff import InputKind, InputRequest
from ish.shell.sequencer import Sequencer
from ish.shell.state import TerminalState
from ish.ui.prompt import Prompt

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
ENTER = b"\r"
DEADLINE = 5


class ShellEditingTests(unittest.IsolatedAsyncioTestCase):
    """Use actual editor input and independent expectations for submitted text."""

    async def asyncSetUp(self):
        """Open PTK at a controlled, acknowledged shell input boundary."""
        self.source = self.enterContext(create_pipe_input())
        self.enterContext(patch("ish.ui.prompt.get_builtins", return_value=[]))
        self.prompt = Prompt(
            shell="bash",
            input=self.source,
            output=DummyOutput(),
            history=InMemoryHistory(),
        )
        self.shell = self.prompt.interactive_shell
        self.shell._accepted_prompt_id = self.shell._prompt_id = (
            self.shell._context_id
        ) = 7
        self.shell.command_done_event = asyncio.Event()
        self.shell.command_done_event.set()
        self.shell.continuation_active = asyncio.Event()
        self.prompt.last_exitcode = 3

    def send(self, text):
        """Submit exact text through bracketed paste and the normal Enter binding."""
        self.source.send_bytes(PASTE_START + text.encode() + PASTE_END + ENTER)

    async def test_primary_names_are_uninterpreted_shell_text(self):
        """Old tool/control names, expressions, and whitespace all return unchanged."""
        for text in (
            "probe '한 글'",
            "ish_reload",
            "ish_exit",
            "  echo 한 글  ",
            "probe $VALUE | cat",
            "",
            "cat <<'END'\nprobe 한 글\nish_exit\nEND\n",
        ):
            with self.subTest(text=text):
                self.send(text)
                self.assertEqual(
                    await asyncio.wait_for(self.shell._read_edit(), DEADLINE), text
                )
                self.assertEqual(self.prompt.last_exitcode, 3)
                self.assertIsNone(self.shell.edit_request)

    async def test_continuation_uses_the_same_literal_editor(self):
        """A shell-issued continuation grant gives the same text result as primary."""
        self.shell._semantic_enabled = True
        submission = self.shell._handoff.begin()
        for sequence, text in enumerate(("ish_reload", "ish_exit", "probe 한 글"), 1):
            request = InputRequest(submission, sequence, InputKind.SYNTAX)
            self.assertTrue(self.shell._handoff.receive(request))
            self.send(text)
            self.assertEqual(
                await asyncio.wait_for(self.shell._read_edit(), DEADLINE), text
            )
            self.assertTrue(self.shell._handoff.reply(request))
            self.assertEqual(self.prompt.last_exitcode, 3)

    async def test_ui_key_callback_submits_text_without_a_dispatcher(self):
        """An explicit UI action can still provide a normal shell submission."""
        calls = []

        def submit(event):
            calls.append("key")
            self.prompt.cmd("ish_exit")

        self.prompt.set_key("f6", handler=submit)
        self.source.send_bytes(b"\x1b[17~")
        self.assertEqual(
            await asyncio.wait_for(self.shell._read_edit(), DEADLINE), "ish_exit"
        )
        self.assertEqual(calls, ["key"])

    async def test_interrupt_and_eof_retire_the_edit(self):
        """Preserve explicit editing controls without magic text exit commands."""
        for key, exception in ((b"\x03", KeyboardInterrupt), (b"\x04", EOFError)):
            with self.subTest(key=key):
                self.source.send_bytes(key)
                with self.assertRaises(exception):
                    await asyncio.wait_for(self.shell._read_edit(), DEADLINE)
                self.assertIsNone(self.shell.edit_request)


class ShellModeTests(unittest.TestCase):
    """Keep native terminal cleanup on the existing shell output parser."""

    def test_shell_output_preserves_controls_and_restores_native_modes(self):
        """Split output still restores the screen, cursor, keypad, and text style."""
        payload = b"native\x1b[?1049h\x1b[?25l\x1b=\x1b[31m"
        for split in range(len(payload) + 1):
            with self.subTest(split=split):
                state = TerminalState()
                parser = Sequencer(control_callback=state.observe)
                rendered = parser.feed(payload[:split]) + parser.feed(payload[split:])
                self.assertEqual(rendered + parser.finish(), payload)
                self.assertEqual(state.restore(), b"\x1b[?1049l\x1b[?25h\x1b>\x1b[0m")


if __name__ == "__main__":
    unittest.main()
