"""Resolve the account's shell without starting processes or importing an editor."""

from __future__ import annotations

import os

DEFAULT_SHELL = "sh"


def login_shell() -> str:
    """Prefer the account database, then SHELL, then the portable shell name."""
    try:
        import pwd

        selected = pwd.getpwuid(os.getuid()).pw_shell
    except (ImportError, KeyError, OSError):
        selected = ""
    return selected or os.environ.get("SHELL") or DEFAULT_SHELL
