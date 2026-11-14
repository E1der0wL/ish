"""Dispatch explicitly admitted ish commands independently of text editing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ish.runtime.process import shell_exitcode
from ish.shell.request import ShellExitRequest, ShellPassRequest

if TYPE_CHECKING:
    from ish.ui.prompt import Prompt

TOOL_ERROR = 1


async def dispatch(prompt: Prompt, command: str) -> str | None:
    """Dispatch a command admitted by the session, or return unchanged shell text.

    Call only when the session permits ish exit, internal commands, and Python
    tools. Shell syntax replies and heredoc data must bypass it. Editing alone
    uses Prompt.prompt_async(); it never implies permission to dispatch.

    None means the tool submission was handled without shell input, including
    failure to acquire tool context. Shell requests propagate to the session;
    shell text retains all whitespace and is never executed here.
    """
    key = command.strip()
    if key == prompt.exit_command:
        raise ShellExitRequest

    if key in prompt.internal_commands:
        handler, args, kwargs = prompt.internal_commands[key]
        raise ShellPassRequest(handler, command, *args, **kwargs)

    argv = prompt._tool_argv(command)
    if argv is not None:
        prompt.last_tool_exitcode = None
        try:
            cwd, environ = prompt.interactive_shell.tool_context()
        except Exception:
            prompt.logger.exception("Python tool context unavailable: %s", argv[0])
            status = TOOL_ERROR
        else:
            # Transport failures and application cancellation must still reach
            # session cleanup; they are not ordinary tool exit statuses.
            status = await prompt.runner.run_in_context(
                prompt.internal_tools[argv[0]], cwd, environ, *argv[1:]
            )
        prompt.last_tool_exitcode = status
        # Retain the raw worker status as well as a shell-style display code.
        prompt.last_exitcode = shell_exitcode(status)
        return None

    return command
