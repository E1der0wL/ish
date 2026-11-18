"""Report startup configuration without executing user code or acquiring session files."""

from __future__ import annotations

import os
import platform
import shutil
import sys

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n
from ish.runtime.distribution import bundled_root

_TERM_ENV = "TERM"


def _cache_status() -> tuple[str, str]:
    """Inspect the nearest existing directory without creating or executing a probe."""
    path = config.CACHE_DIR
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        writable = path.is_dir() and os.access(path, os.W_OK | os.X_OK)
        access = i18n.get(
            Message.CLI_DIAGNOSE_CACHE_WRITABLE
            if writable
            else Message.CLI_DIAGNOSE_CACHE_DENIED,
            path=path,
        )
        flags = os.statvfs(path).f_flag
        noexec = i18n.get(
            Message.CLI_DIAGNOSE_YES
            if flags & os.ST_NOEXEC
            else Message.CLI_DIAGNOSE_NO
        )
        return access, noexec
    except (OSError, AttributeError):
        return (i18n.get(Message.CLI_DIAGNOSE_UNAVAILABLE),) * 2


def environment_fields(shell: str) -> dict[Message, object]:
    """Collect shared runtime facts without invoking a shell or user code.

    A successful report does not certify shell health or the ability to execute files.
    Missing dependencies are reported as data; no shell or helper is spawned here.
    """
    missing = i18n.get(Message.CLI_DIAGNOSE_MISSING)
    unavailable = i18n.get(Message.CLI_DIAGNOSE_UNAVAILABLE)
    shell_path = shutil.which(shell)
    if shell_path:
        shell_path = os.path.abspath(shell_path)
    try:
        columns, rows = os.get_terminal_size(sys.stdout.fileno())
        size = i18n.get(
            Message.CLI_DIAGNOSE_TERMINAL_DIMENSIONS, columns=columns, rows=rows
        )
    except (OSError, ValueError):
        size = unavailable
    cache_access, cache_noexec = _cache_status()

    return {
        Message.CLI_DIAGNOSE_VERSION: __version__,
        Message.CLI_DIAGNOSE_RUNTIME: i18n.get(
            Message.CLI_DIAGNOSE_BUNDLED
            if bundled_root() is not None
            else Message.CLI_DIAGNOSE_SOURCE
        ),
        Message.CLI_DIAGNOSE_PYTHON: platform.python_version(),
        Message.CLI_DIAGNOSE_EXECUTABLE: sys.executable,
        Message.CLI_DIAGNOSE_PLATFORM: f"{sys.platform} {platform.release()} {platform.machine()}",
        Message.CLI_DIAGNOSE_LIBC: " ".join(platform.libc_ver()).strip() or unavailable,
        Message.CLI_DIAGNOSE_SHELL: shell,
        Message.CLI_DIAGNOSE_SHELL_PATH: shell_path or missing,
        Message.CLI_DIAGNOSE_SHELL_REALPATH: os.path.realpath(shell_path)
        if shell_path
        else missing,
        Message.CLI_DIAGNOSE_HOME: config.ISH_HOME,
        Message.CLI_DIAGNOSE_LANGUAGE: i18n.current_lang,
        Message.CLI_DIAGNOSE_LANGUAGE_DIR: config.LANG_DIR,
        Message.CLI_DIAGNOSE_CACHE: config.CACHE_DIR,
        Message.CLI_DIAGNOSE_CACHE_ACCESS: cache_access,
        Message.CLI_DIAGNOSE_CACHE_NOEXEC: cache_noexec,
        Message.CLI_DIAGNOSE_TERMINAL: os.environ.get(_TERM_ENV) or unavailable,
        Message.CLI_DIAGNOSE_STDIN_TTY: i18n.get(
            Message.CLI_DIAGNOSE_YES if sys.stdin.isatty() else Message.CLI_DIAGNOSE_NO
        ),
        Message.CLI_DIAGNOSE_STDOUT_TTY: i18n.get(
            Message.CLI_DIAGNOSE_YES if sys.stdout.isatty() else Message.CLI_DIAGNOSE_NO
        ),
        Message.CLI_DIAGNOSE_TERMINAL_SIZE: size,
    }


def display_diagnostics(
    title: Message, scope: Message, values: dict[Message, object]
) -> None:
    """Print already collected facts using the shared translation manager."""
    print(i18n.get(title))
    print(i18n.get(scope))
    for label, value in values.items():
        print(f"{i18n.get(label)}: {value}")
