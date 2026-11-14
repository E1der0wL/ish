"""Verify editing and command dispatch through actual prompt-toolkit input."""

import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ish.app.dispatch import dispatch
from ish.shell.engine import InteractiveShell
from ish.shell.request import ShellExitRequest, ShellPassRequest
from ish.ui.prompt import Prompt

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
ENTER = b"\r"
DEADLINE = 5


class EditDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Keep text replies inert until the caller explicitly dispatches a command."""

    async def asyncSetUp(self):
        """Use the real editor and replace only shell context and worker execution."""
        self.source = self.enterContext(create_pipe_input())
        self.enterContext(patch("ish.ui.prompt.get_builtins", return_value=[]))
        self.context = self.enterContext(
            patch.object(
                InteractiveShell,
                "tool_context",
                return_value=("/work", {"VALUE": "한 글"}),
            )
        )
        self.prompt = Prompt(
            shell="bash",
            input=self.source,
            output=DummyOutput(),
            history=InMemoryHistory(),
        )
        self.calls = []
        self.prompt.set_tool("probe", function=print)
        self.prompt.runner.run_in_context = self.run_tool
        self.prompt.last_exitcode = 3

    async def run_tool(self, function, cwd, environ, *args):
        """Record the externally visible worker invocation and return its exit code."""
        self.calls.append((function, cwd, environ, args))
        return 7

    def send(self, text):
        """Submit exact text through bracketed paste and the real Enter binding."""
        self.source.send_bytes(PASTE_START + text.encode() + PASTE_END + ENTER)

    async def test_editing_never_dispatches_commands_or_body_text(self):
        """Return tool names, internal commands, whitespace, and heredoc data literally."""
        for text in (
            "probe '한 글'",
            "ish_reload",
            "ish_exit",
            "  echo 한 글  ",
            "cat <<'END'\nprobe 한 글\nish_exit\nEND\n",
            "",
        ):
            with self.subTest(text=text):
                self.send(text)
                result = await asyncio.wait_for(self.prompt.prompt_async(), DEADLINE)
                self.assertEqual(result, text)
                self.assertEqual(self.calls, [])
                self.context.assert_not_called()
                self.assertEqual(self.prompt.last_exitcode, 3)

    async def test_dispatch_requires_an_explicit_call_after_editing(self):
        """Editing a tool invocation alone has no effect; explicit dispatch runs it once."""
        self.send("probe '한 글' \"a b\"")
        text = await asyncio.wait_for(self.prompt.prompt_async(), DEADLINE)
        self.assertEqual(self.calls, [])
        self.assertIsNone(await dispatch(self.prompt, text))
        self.assertEqual(
            self.calls, [(print, "/work", {"VALUE": "한 글"}, ("한 글", "a b"))]
        )
        self.assertEqual(self.prompt.last_exitcode, 7)
        self.assertEqual(self.prompt.last_tool_exitcode, 7)

    async def test_compound_shell_text_is_returned_unchanged(self):
        """Keep command blocks and expansions with the shell, including trailing spaces."""
        for text in (
            "  probe x | cat  ",
            "probe $VALUE",
            "cat <<'END'\nprobe\nEND\n",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(await dispatch(self.prompt, text), text)
        self.assertEqual(self.calls, [])
        self.context.assert_not_called()

    async def test_ish_exit_is_only_interpreted_during_dispatch(self):
        """Return the exit word as editor text before raising the session exit request."""
        self.send("ish_exit")
        text = await asyncio.wait_for(self.prompt.prompt_async(), DEADLINE)
        self.assertEqual(text, "ish_exit")
        with self.assertRaises(ShellExitRequest):
            await dispatch(self.prompt, text)

    async def test_internal_command_is_deferred_to_the_session(self):
        """Dispatch an internal command as a request without executing its callback."""
        invoked = []

        async def callback(value):
            """Observe the later session-owned callback execution."""
            invoked.append(value)

        self.prompt.internal_commands["local"] = (callback, ("configured",), {})
        self.send("local")
        text = await asyncio.wait_for(self.prompt.prompt_async(), DEADLINE)
        with self.assertRaises(ShellPassRequest) as caught:
            await dispatch(self.prompt, text)
        self.assertEqual(invoked, [])
        request = caught.exception
        self.assertEqual(request.command, "local")
        await request.callback(*request.args, **request.kwargs)
        self.assertEqual(invoked, ["configured"])

    async def test_existing_command_entry_still_dispatches(self):
        """Preserve the old read-and-dispatch API through actual editor input."""
        self.send("probe legacy")
        self.assertIsNone(
            await asyncio.wait_for(self.prompt.read_and_dispatch(), DEADLINE)
        )
        self.assertEqual(
            self.calls, [(print, "/work", {"VALUE": "한 글"}, ("legacy",))]
        )

    async def test_unavailable_context_does_not_start_a_worker(self):
        """Keep the previous failure status when shell context cannot be acquired."""
        self.context.side_effect = RuntimeError("context unavailable")
        with self.assertLogs(self.prompt.logger, level="ERROR"):
            self.assertIsNone(await dispatch(self.prompt, "probe"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.prompt.last_exitcode, 1)
        self.assertEqual(self.prompt.last_tool_exitcode, 1)

    async def test_worker_failure_propagates_without_fabricating_a_result(self):
        """Transport failure remains a session error, not a completed tool invocation."""

        async def fail(*args):
            """Simulate failure of the worker transport."""
            raise OSError("worker transport failed")

        self.prompt.runner.run_in_context = fail
        with self.assertRaisesRegex(OSError, "transport failed"):
            await dispatch(self.prompt, "probe")
        self.assertEqual(self.prompt.last_exitcode, 3)
        self.assertIsNone(self.prompt.last_tool_exitcode)


if __name__ == "__main__":
    unittest.main()
