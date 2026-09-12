"""Report startup configuration without executing user code or acquiring session files."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from argparse import Namespace
from pathlib import Path

from ish import __version__
from ish.config import config
from ish.lang import i18n
from ish.shell.adapters import get_adapter
from ish.shell.constants import BUNDLED_FORWARD_DIRECTORY, FORWARD_BINARY


def _cache_status() -> tuple[str, str]:
    """Inspect the nearest existing directory without creating or executing a probe."""
    path = config.CACHE_DIR
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        writable = path.is_dir() and os.access(path, os.W_OK | os.X_OK)
        access = i18n.get(
            "cli_diagnose_cache_writable" if writable else "cli_diagnose_cache_denied",
            path=path,
        )
        flags = os.statvfs(path).f_flag
        noexec = i18n.get(
            "cli_diagnose_yes" if flags & os.ST_NOEXEC else "cli_diagnose_no"
        )
        return access, noexec
    except OSError:
        return (i18n.get("cli_diagnose_unavailable"),) * 2


def _helper_status() -> str:
    """Locate the bundled helper or compiler without importing the build machinery."""
    if "__compiled__" in globals():
        path = Path(__file__).parents[2] / BUNDLED_FORWARD_DIRECTORY / FORWARD_BINARY
        return i18n.get(
            "cli_diagnose_helper_bundled"
            if path.is_file()
            else "cli_diagnose_helper_missing",
            path=path,
        )
    return i18n.get(
        "cli_diagnose_helper_source",
        compiler=shutil.which("gcc") or i18n.get("cli_diagnose_missing"),
    )


def print_diagnostics(option: Namespace) -> None:
    """Print a translated pre-rc snapshot, including unavailable or unsupported resources.

    A successful report does not certify shell health or the ability to execute files.
    Missing dependencies are reported as data; no shell or helper is spawned here.
    """
    missing = i18n.get("cli_diagnose_missing")
    unavailable = i18n.get("cli_diagnose_unavailable")
    shell_path = shutil.which(option.shell)
    if shell_path:
        shell_path = os.path.abspath(shell_path)
    try:
        adapter = get_adapter(option.shell, shell_path).name
    except ValueError:
        adapter = i18n.get("cli_diagnose_unsupported")
    try:
        columns, rows = os.get_terminal_size(sys.stdout.fileno())
        size = i18n.get("cli_diagnose_terminal_dimensions", columns=columns, rows=rows)
    except (OSError, ValueError):
        size = unavailable
    cache_access, cache_noexec = _cache_status()

    values = {
        "version": __version__,
        "runtime": i18n.get(
            "cli_diagnose_compiled"
            if "__compiled__" in globals()
            else "cli_diagnose_source"
        ),
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": f"{sys.platform} {platform.release()} {platform.machine()}",
        "libc": " ".join(platform.libc_ver()).strip() or unavailable,
        "shell": option.shell,
        "shell_path": shell_path or missing,
        "shell_realpath": os.path.realpath(shell_path) if shell_path else missing,
        "adapter": adapter,
        "home": config.ISH_HOME,
        "rc": config.RC_FILE,
        "rc_policy": i18n.get(
            "cli_diagnose_disabled" if option.no_rc else "cli_diagnose_enabled"
        ),
        "plugins": config.PLUGIN_SCRIPT_DIR,
        "plugin_policy": i18n.get(
            "cli_diagnose_disabled" if option.no_plugins else "cli_diagnose_enabled"
        ),
        "language": i18n.current_lang,
        "language_dir": config.LANG_DIR,
        "cache": config.CACHE_DIR,
        "cache_access": cache_access,
        "cache_noexec": cache_noexec,
        "helper": _helper_status(),
        "terminal": os.environ.get("TERM") or unavailable,
        "stdin_tty": i18n.get(
            "cli_diagnose_yes" if sys.stdin.isatty() else "cli_diagnose_no"
        ),
        "stdout_tty": i18n.get(
            "cli_diagnose_yes" if sys.stdout.isatty() else "cli_diagnose_no"
        ),
        "terminal_size": size,
    }
    print(i18n.get("cli_diagnose_title"))
    print(i18n.get("cli_diagnose_scope"))
    for name, value in values.items():
        print(f"{i18n.get('cli_diagnose_' + name)}: {value}")
