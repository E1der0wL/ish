"""Resolve translated stream startup options before touching a terminal session."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import shutil
import sys
from logging.handlers import MemoryHandler
from pathlib import Path

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n
from ish.log import LOG_LEVELS, session_logging
from ish.runtime.startup import login_shell

PROGRAM = "ish-stream"
DEFAULT_LOG_LEVEL = "WARNING"
INTERACTIVE_ARGS = ("-i",)
LOGIN_ARGS = ("-l",)
STARTUP_LOG_CAPACITY = 4
PATH_OPTIONS = ("cwd", "plugin_dir", "plugin_lib_dir", "integration_dir", "log_file")
OPTIONS = (
    (("-h", "--help"), Message.CLI_HELP_OPTION_HELP, {"action": "store_true"}),
    (("--version",), Message.CLI_VERSION_OPTION_HELP, {"action": "store_true"}),
    (("--diagnose",), Message.CLI_DIAGNOSE_OPTION_HELP, {"action": "store_true"}),
    (("--home",), Message.CLI_HOME_OPTION_HELP, {"type": Path, "metavar": "DIR"}),
    (("-l", "--lang"), Message.CLI_LANG_OPTION_HELP, {}),
    (("--shell",), Message.STREAM_SHELL_HELP, {"metavar": "SHELL"}),
    (("--cwd",), Message.STREAM_CWD_HELP, {"type": Path, "metavar": "DIR"}),
    (
        ("--plugin",),
        Message.STREAM_PLUGIN_HELP,
        {"action": "append", "metavar": "NAME"},
    ),
    (
        ("--plugin-dir",),
        Message.STREAM_PLUGIN_DIR_HELP,
        {"type": Path, "metavar": "DIR"},
    ),
    (
        ("--plugin-lib-dir",),
        Message.STREAM_PLUGIN_LIB_HELP,
        {"type": Path, "metavar": "DIR"},
    ),
    (
        ("--integration-dir",),
        Message.STREAM_INTEGRATION_HELP,
        {"type": Path, "metavar": "DIR"},
    ),
    (
        ("--log-file",),
        Message.STREAM_LOG_FILE_HELP,
        {"type": Path, "nargs": "?", "metavar": "PATH"},
    ),
    (
        ("--log-level",),
        Message.STREAM_LOG_LEVEL_HELP,
        {"type": str.upper, "choices": tuple(LOG_LEVELS), "default": DEFAULT_LOG_LEVEL},
    ),
)


class Formatter(argparse.HelpFormatter):
    """Translate the usage prefix while retaining argparse's tested layout."""

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(
            usage,
            actions,
            groups,
            i18n.get(Message.STREAM_USAGE) if prefix is None else prefix,
        )


class Parser(argparse.ArgumentParser):
    """Use the shared message catalog for CLI error presentation."""

    def error(self, message):
        """Exit with a translated prefix and argparse's original argument detail."""
        self.print_usage(sys.stderr)
        self.exit(
            2, f"{self.prog}: {i18n.get(Message.STREAM_PARSE_ERROR, error=message)}\n"
        )


def _path(value):
    return Path(value).expanduser().resolve()


def report_logs(records, logger):
    """Apply the selected session policy to diagnostics collected before translation."""
    for record in records:
        if logger.isEnabledFor(record.levelno):
            logger.handle(record)


def parse(argv=None):
    """Load settings paths and translations without creating files or loading plugins."""
    bootstrap = Parser(prog=PROGRAM, add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--home", type=Path)
    bootstrap.add_argument("--lang", "-l")
    first, _ = bootstrap.parse_known_args(argv)
    if first.home is not None:
        try:
            config.ISH_HOME = _path(first.home)
        except (OSError, ValueError) as error:
            bootstrap.error(
                i18n.get(Message.CLI_HOME_ERROR, path=first.home, error=error)
            )
    i18n.base_path = config.LANG_DIR
    # Locale messages are needed to construct the parser. Hold their diagnostics
    # until the full CLI has selected a log level and decided whether to execute.
    records = MemoryHandler(STARTUP_LOG_CAPACITY)
    startup_logger = logging.Logger(PROGRAM)
    startup_logger.addHandler(records)
    try:
        i18n.load(first.lang, create=False, logger=startup_logger)
    except ValueError as error:
        bootstrap.error(str(error))
    parser = Parser(
        prog=PROGRAM,
        description=i18n.get(Message.STREAM_DESCRIPTION),
        add_help=False,
        allow_abbrev=False,
        formatter_class=Formatter,
    )
    parser._optionals.title = i18n.get(Message.CLI_OPTIONS_TITLE)
    parser._positionals.title = i18n.get(Message.CLI_ARGUMENTS_TITLE)
    for names, message, settings in OPTIONS:
        settings = dict(settings)
        if names == ("--log-file",):
            settings["const"] = config.LOG_FILE
        parser.add_argument(*names, help=i18n.get(message), **settings)
    parser.set_defaults(shell=login_shell(), cwd=Path.cwd(), plugin=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--login", action="store_true", help=i18n.get(Message.STREAM_LOGIN_HELP)
    )
    mode.add_argument(
        "--shell-arg",
        action="append",
        metavar="ARG",
        help=i18n.get(Message.STREAM_SHELL_ARG_HELP),
    )
    args = parser.parse_args(argv)
    args.shell_args = list(
        LOGIN_ARGS
        if args.login
        else args.shell_arg
        if args.shell_arg is not None
        else INTERACTIVE_ARGS
    )
    try:
        for name in PATH_OPTIONS:
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, _path(value))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.home = config.ISH_HOME
    args.lang = i18n.current_lang
    args.log_records = tuple(records.buffer)
    records.close()
    if args.help or args.version or args.diagnose:
        with session_logging(level=args.log_level) as log:
            report_logs(args.log_records, log.logger)
    if args.help:
        parser.print_help()
        return parser, args, True
    if args.version:
        print(i18n.get(Message.STREAM_VERSION, version=__version__))
        return parser, args, True
    if args.diagnose:
        diagnose(args)
        return parser, args, True
    return parser, args, False


def diagnose(args):
    """Report selected settings without importing or executing shell/UI plugins."""
    from ish.runtime.diagnostics import display_diagnostics, environment_fields

    from . import extensions, scripts

    # These are resource paths, not evidence of runtime integration support.
    values = environment_fields(args.shell)
    values.update(
        {
            Message.STREAM_DIAGNOSE_CWD: args.cwd,
            Message.STREAM_DIAGNOSE_ARGS: shlex.join(args.shell_args),
            Message.CLI_DIAGNOSE_PLUGINS: args.plugin_dir or extensions.DIRECTORY,
            Message.STREAM_DIAGNOSE_PLUGIN_LIB: args.plugin_lib_dir
            or config.PLUGIN_LIB_DIR,
            Message.STREAM_DIAGNOSE_SELECTED_PLUGINS: ", ".join(args.plugin)
            or i18n.get(Message.CLI_DIAGNOSE_DISABLED),
            Message.STREAM_DIAGNOSE_INTEGRATIONS: args.integration_dir
            or scripts.DIRECTORY,
            Message.STREAM_DIAGNOSE_LOG_FILE: args.log_file
            or i18n.get(Message.CLI_DIAGNOSE_DISABLED),
            Message.STREAM_DIAGNOSE_LOG_LEVEL: args.log_level,
        }
    )
    display_diagnostics(
        Message.STREAM_DIAGNOSE_TITLE, Message.STREAM_DIAGNOSE_SCOPE, values
    )


def command(parser, args):
    """Validate execution prerequisites after read-only information flags finish."""
    if os.name != "posix":
        parser.error(i18n.get(Message.STREAM_PLATFORM_ERROR))
    if not os.isatty(0) or not os.isatty(1):
        parser.error(i18n.get(Message.STREAM_TTY_ERROR))
    executable = shutil.which(args.shell)
    if executable is None:
        parser.error(i18n.get(Message.STREAM_SHELL_ERROR, shell=args.shell))
    if not args.cwd.is_dir():
        parser.error(i18n.get(Message.STREAM_CWD_ERROR, path=args.cwd))
    return [os.path.abspath(executable), *args.shell_args]
