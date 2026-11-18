"""Reuse shared CLI, locale, rc and diagnostic services for the VT entry point."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

from ish.config import config
from ish.lang import Message, i18n
from ish.runtime.cli import OPTIONS as COMMON_OPTIONS
from ish.runtime.cli import Startup, command, report_logs
from ish.runtime.cli import parse as parse_common
from ish.runtime.diagnostics import display_diagnostics, environment_fields
from ish.runtime.settings import PATH_OPTIONS as COMMON_PATHS

from .engine import BRIDGE_ABI, ENGINE_VERSION
from .features import merge, parse_assignment
from .identity import PROGRAM
from .settings import CLIPBOARD_POLICIES, COLOR_DEPTHS, MOUSE_POLICIES, Settings, load
from .vt import DEFAULT_LIBRARY, LIBRARY_ENV

__all__ = ["command", "parse", "report_logs"]


def feature_assignment(value):
    """Translate a registry validation error at argparse's existing input boundary."""
    try:
        return parse_assignment(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


OPTIONS = COMMON_OPTIONS + (
    (("--plugin",), Message.VT_PLUGIN_HELP, {"action": "append", "metavar": "NAME"}),
    (("--plugin-dir",), Message.VT_PLUGIN_DIR_HELP, {"type": Path, "metavar": "DIR"}),
    (
        ("--plugin-lib-dir",),
        Message.STARTUP_PLUGIN_LIB_HELP,
        {"type": Path, "metavar": "DIR"},
    ),
    (("--mouse",), Message.VT_MOUSE_HELP, {"choices": MOUSE_POLICIES}),
    (("--color-depth",), Message.VT_COLOR_DEPTH_HELP, {"choices": tuple(COLOR_DEPTHS)}),
    (("--clipboard",), Message.VT_CLIPBOARD_HELP, {"choices": CLIPBOARD_POLICIES}),
    (
        ("--feature",),
        Message.VT_FEATURE_HELP,
        {
            "action": "append",
            "dest": "features",
            "type": feature_assignment,
            "metavar": "NAME=on|off|auto",
        },
    ),
    (("--scrollback-lines",), Message.VT_SCROLLBACK_LINES_HELP, {"type": int}),
    (("--scrollback-bytes",), Message.VT_SCROLLBACK_BYTES_HELP, {"type": int}),
    # Existing host harnesses explicitly request terminal-text diagnostics.
    # It is not a normal file log and is intentionally absent from public help.
    (("--dump-state",), None, {"type": Path}),
)
PATH_OPTIONS = (*COMMON_PATHS, "dump_state")


def diagnose(args):
    """Report configuration only: never load a native library or probe the host."""
    library = Path(os.environ.get(LIBRARY_ENV, DEFAULT_LIBRARY)).expanduser()
    values = environment_fields(args.shell)
    values.update(
        {
            Message.STARTUP_DIAGNOSE_CWD: args.cwd,
            Message.CLI_DIAGNOSE_RC: config.RC_FILE,
            Message.CLI_DIAGNOSE_RC_POLICY: i18n.get(Message.STARTUP_RC_NOT_EXECUTED),
            Message.VT_DIAGNOSE_NAMESPACE: "config",
            Message.VT_DIAGNOSE_CONTROLS: i18n.get(Message.VT_CONTROL_POLICY),
            Message.VT_DIAGNOSE_HOST: i18n.get(Message.VT_HOST_POLICY),
            Message.STARTUP_DIAGNOSE_ARGS: shlex.join(args.shell_args),
            Message.STARTUP_DIAGNOSE_ESCAPE_DELAY: args.escape_delay,
            Message.CLI_DIAGNOSE_PLUGINS: args.plugin_dir or config.PLUGIN_SCRIPT_DIR,
            Message.STARTUP_DIAGNOSE_PLUGIN_LIB: args.plugin_lib_dir
            or config.PLUGIN_LIB_DIR,
            Message.STARTUP_DIAGNOSE_SELECTED_PLUGINS: ", ".join(args.plugin)
            or i18n.get(Message.CLI_DIAGNOSE_DISABLED),
            Message.STARTUP_DIAGNOSE_LOG_FILE: args.log_file
            or i18n.get(Message.CLI_DIAGNOSE_DISABLED),
            Message.STARTUP_DIAGNOSE_LOG_LEVEL: args.log_level,
            Message.VT_DIAGNOSE_MOUSE: args.mouse,
            Message.VT_DIAGNOSE_COLOR_DEPTH: args.color_depth,
            Message.VT_DIAGNOSE_FEATURES: "; ".join(
                i18n.get(
                    Message.VT_FEATURE_STATUS,
                    name=name,
                    policy=policy,
                    support="not-probed",
                    active="not-evaluated",
                    reason="information-query",
                )
                for name, policy in args.features.items()
            ),
            Message.VT_DIAGNOSE_CLIPBOARD: args.clipboard,
            Message.VT_DIAGNOSE_SCROLLBACK_LINES: args.scrollback_lines,
            Message.VT_DIAGNOSE_SCROLLBACK_BYTES: args.scrollback_bytes,
            Message.VT_DIAGNOSE_ENGINE: ENGINE_VERSION,
            Message.VT_DIAGNOSE_ABI: BRIDGE_ABI,
            Message.VT_DIAGNOSE_LIBRARY: library,
            Message.VT_DIAGNOSE_LIBRARY_PRESENT: i18n.get(
                Message.CLI_DIAGNOSE_YES
                if library.is_file()
                else Message.CLI_DIAGNOSE_NO
            ),
        }
    )
    display_diagnostics(Message.VT_DIAGNOSE_TITLE, Message.VT_DIAGNOSE_SCOPE, values)


def parse(argv=None):
    """Apply terminal policy to shared language precedence and read-only queries."""
    return parse_common(
        argv,
        startup=Startup(
            program=PROGRAM,
            description=Message.VT_DESCRIPTION,
            version=Message.VT_VERSION,
            login_help=Message.VT_LOGIN_HELP,
            options=OPTIONS,
            paths=PATH_OPTIONS,
            settings=Settings,
            load=load,
            diagnose=diagnose,
            field_mergers={"features": merge},
        ),
    )
