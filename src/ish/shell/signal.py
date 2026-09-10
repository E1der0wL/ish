"""Control-flow exceptions requesting session exit or internal tool execution from the
editor.
"""

from __future__ import annotations

from typing import Callable

__all__ = ["ShellExitRequest", "ShellPassRequest"]


class ShellExitRequest(Exception):
    """Request a normal ish exit from the editing loop."""

    pass


class ShellPassRequest(Exception):
    """Request an asynchronous internal task instead of sending a command to the shell."""

    def __init__(self, callback: Callable, command: str, *args, **kwargs):
        """Store the callback, original command, and arguments to execute."""
        self.callback = callback
        self.command = command
        self.args = args
        self.kwargs = kwargs
