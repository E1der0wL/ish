"""Resolve translated stream startup options before touching a terminal session."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import shlex
import shutil
import sys
from logging.handlers import MemoryHandler
from pathlib import Path

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n
from ish.log import LOG_LEVELS, session_logging

from .settings import PATH_OPTIONS, RcError, Settings, load

PROGRAM = "ish"
LOGIN_ARGS = ("-l",)
STARTUP_LOG_CAPACITY = 4
OPTIONS = (
    (("-h", "--help"), Message.CLI_HELP_OPTION_HELP, {"action": "store_true"}),
    (("--version",), Message.CLI_VERSION_OPTION_HELP, {"action": "store_true"}),
    (("--diagnose",), Message.CLI_DIAGNOSE_OPTION_HELP, {"action": "store_true"}),
    (("--home",), Message.CLI_HOME_OPTION_HELP, {"type": Path, "metavar": "DIR"}),
    (("-l", "--lang"), Message.CLI_LANG_OPTION_HELP, {"metavar": "LANG"}),
    (("--no-rc",), Message.STREAM_NO_RC_HELP, {"action": "store_true"}),
    (
        ("--escape-delay",),
        Message.STREAM_ESCAPE_DELAY_HELP,
        {"type": float, "metavar": "SECONDS"},
    ),
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
        {"type": str.upper, "choices": tuple(LOG_LEVELS)},
    ),
)
METAVARS = {
    "DIR": Message.STREAM_META_DIR,
    "SECONDS": Message.STREAM_META_SECONDS,
    "SHELL": Message.STREAM_META_SHELL,
    "NAME": Message.STREAM_META_NAME,
    "PATH": Message.STREAM_META_PATH,
    "ARG": Message.STREAM_META_ARG,
    "LANG": Message.STREAM_META_LANG,
}
# argparse has no per-instance gettext hook. Normalize its pinned Python 3.12
# diagnostics here, keeping the parser's argument handling and exception causes.
ARGUMENT_ERRORS = (
    (
        re.compile(r"unrecognized arguments: (?P<arguments>.*)", re.S),
        Message.STREAM_ARG_UNKNOWN,
    ),
    (re.compile(r"expected one argument"), Message.STREAM_ARG_EXPECTED),
    (
        re.compile(r"not allowed with argument (?P<argument>.*)", re.S),
        Message.STREAM_ARG_CONFLICT,
    ),
    (
        re.compile(
            r"invalid choice: (?P<value>.*?) \(choose from (?P<choices>.*)\)", re.S
        ),
        Message.STREAM_ARG_CHOICE,
    ),
    (
        re.compile(r"invalid (?P<kind>.*?) value: (?P<value>.*)", re.S),
        Message.STREAM_ARG_VALUE,
    ),
    (
        re.compile(r"ignored explicit argument (?P<value>.*)", re.S),
        Message.STREAM_ARG_EXPLICIT,
    ),
)
ARGUMENT_CONTEXT = re.compile(r"argument (?P<argument>[^:]+): (?P<error>.*)", re.S)


def argument_message(message):
    """Translate stdlib-owned diagnostics without changing argparse's global state."""
    context = ARGUMENT_CONTEXT.fullmatch(message)
    if context is not None:
        return i18n.get(
            Message.STREAM_ARG_ERROR,
            argument=context["argument"],
            error=argument_message(context["error"]),
        )
    for pattern, key in ARGUMENT_ERRORS:
        matched = pattern.fullmatch(message)
        if matched is not None:
            return i18n.get(key, **matched.groupdict())
    return message


class Formatter(argparse.HelpFormatter):
    """Translate the usage prefix while retaining argparse's tested layout."""

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(
            usage,
            actions,
            groups,
            i18n.get(Message.STREAM_USAGE) if prefix is None else prefix,
        )

    def _metavar_formatter(self, action, default_metavar):
        metavar = action.metavar or default_metavar
        if metavar in METAVARS:
            # Resolve labels at rendering time: rc validation can select a
            # language after the parser has already checked the command line.
            action = copy.copy(action)
            action.metavar = i18n.get(METAVARS[metavar])
        return super()._metavar_formatter(action, default_metavar)


class Parser(argparse.ArgumentParser):
    """Use the shared message catalog for CLI error presentation."""

    def __init__(self, *args, **kwargs):
        """Keep bootstrap usage and full help on the same translated formatter."""
        kwargs.setdefault("formatter_class", Formatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        """Translate both argument context and detail at the parser boundary."""
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: {i18n.get(Message.STREAM_PARSE_ERROR, error=argument_message(message))}\n",
        )


def _path(value):
    return Path(value).expanduser().resolve()


def report_logs(records, logger):
    """Apply the selected session policy to diagnostics collected before translation."""
    for record in records:
        if logger.isEnabledFor(record.levelno):
            logger.handle(record)


def parse(argv=None):
    """Resolve CLI over ish rc settings; queries never execute the Python rc."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Only the explicit language is interpreted first. Invalid unrelated options
    # must still use a later --lang, without running the arbitrary Python rc.
    language = Parser(
        prog=PROGRAM, add_help=False, allow_abbrev=False, exit_on_error=False
    )
    language.add_argument("--lang", "-l")
    first = argparse.Namespace(lang=None, home=None)
    try:
        language.parse_known_args(argv, namespace=first)
    except argparse.ArgumentError:
        # The complete bootstrap below retains the original failure and order.
        pass
    bootstrap = Parser(
        prog=PROGRAM, add_help=False, allow_abbrev=False, exit_on_error=False
    )
    bootstrap.add_argument("--home", type=Path)
    bootstrap.add_argument("--lang", "-l")
    bootstrap_error = None
    try:
        bootstrap.parse_known_args(argv, namespace=first)
    except argparse.ArgumentError as error:
        bootstrap_error = error
    home_error = None
    try:
        config.reset()
        if first.home is not None:
            config.ISH_HOME = _path(first.home)
    except (OSError, ValueError) as error:
        home_error = error
    i18n.base_path = config.LANG_DIR
    # Locale messages are needed to construct the parser. Hold their diagnostics
    # until the full CLI has selected a log level and decided whether to execute.
    records = MemoryHandler(STARTUP_LOG_CAPACITY)
    startup_logger = logging.Logger(PROGRAM)
    startup_logger.addHandler(records)
    try:
        i18n.load(first.lang, create=False, logger=startup_logger)
    except ValueError:
        # A malformed explicit language has no usable catalog of its own.
        i18n.load(create=False, logger=startup_logger)
        bootstrap.error(i18n.get(Message.I18N_LANGUAGE_ERROR))
    if home_error is not None:
        bootstrap.error(
            i18n.get(Message.CLI_HOME_ERROR, path=first.home, error=home_error)
        )
    if bootstrap_error is not None:
        bootstrap.error(str(bootstrap_error))
    parser = Parser(
        prog=PROGRAM,
        description=i18n.get(Message.STREAM_DESCRIPTION),
        add_help=False,
        allow_abbrev=False,
        formatter_class=Formatter,
    )
    parser._optionals.title = i18n.get(Message.CLI_OPTIONS_TITLE)
    parser._positionals.title = i18n.get(Message.CLI_ARGUMENTS_TITLE)
    plugins = parser.add_mutually_exclusive_group()
    plugins.add_argument(
        "--no-plugins",
        action="store_true",
        help=i18n.get(Message.STREAM_NO_PLUGINS_HELP),
    )
    for names, message, settings in OPTIONS:
        settings = dict(settings)
        if names == ("--log-file",):
            settings["const"] = config.LOG_FILE
        target = plugins if names == ("--plugin",) else parser
        target.add_argument(*names, help=i18n.get(message), **settings)
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
    information = args.help or args.version or args.diagnose
    try:
        # Reject a non-terminal launch before running arbitrary user initialization.
        if not information:
            terminal(parser)
        selected = Settings() if information or args.no_rc else load(config.RC_FILE)
        _rc_language(selected, first.lang, startup_logger, records)
        for name in PATH_OPTIONS:
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, _path(value))
        if args.shell is not None and ("/" in args.shell or args.shell.startswith("~")):
            args.shell = os.path.abspath(os.path.expanduser(args.shell))
        for name in selected.__dataclass_fields__:
            option = "plugin" if name == "plugins" else name
            value = getattr(args, option, None)
            if value is not None:
                setattr(selected, name, value)
        if args.login:
            selected.shell_args = list(LOGIN_ARGS)
        elif args.shell_arg is not None:
            selected.shell_args = args.shell_arg
        if args.no_plugins:
            selected.plugins = []
        selected.validate(config.ISH_HOME)
        for name in selected.__dataclass_fields__:
            option = "plugin" if name == "plugins" else name
            setattr(args, option, getattr(selected, name))
    except RcError as error:
        try:
            _rc_language(error.settings, first.lang, startup_logger, records)
        except ValueError as language_error:
            parser.error(str(RcError(error.path, error.settings, language_error)))
        parser.error(str(error))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.home = config.ISH_HOME
    args.lang = i18n.current_lang
    args.log_records = tuple(records.buffer)
    records.close()
    if information:
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


def _rc_language(settings, explicit, logger, records):
    """Apply rc language only after execution, keeping explicit CLI language first."""
    if (
        explicit is None
        and isinstance(settings.lang, str)
        and settings.lang
        and settings.lang != i18n.current_lang
    ):
        # These records describe only the provisional language load. The final
        # load rechecks its fallback files and reports them in the chosen language.
        records.buffer.clear()
        i18n.load(settings.lang, create=False, logger=logger)


def diagnose(args):
    """Report selected settings without importing or executing shell/UI plugins."""
    from ish.runtime.diagnostics import display_diagnostics, environment_fields

    from . import extensions, scripts

    # These are resource paths, not evidence of runtime integration support.
    values = environment_fields(args.shell)
    values.update(
        {
            Message.STREAM_DIAGNOSE_CWD: args.cwd,
            Message.CLI_DIAGNOSE_RC: config.RC_FILE,
            Message.CLI_DIAGNOSE_RC_POLICY: i18n.get(Message.STREAM_RC_NOT_EXECUTED),
            Message.STREAM_DIAGNOSE_ESCAPE_DELAY: args.escape_delay,
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


def terminal(parser):
    """Require a controlling terminal before executing user startup code."""
    if os.name != "posix":
        parser.error(i18n.get(Message.STREAM_PLATFORM_ERROR))
    if not os.isatty(0) or not os.isatty(1):
        parser.error(i18n.get(Message.STREAM_TTY_ERROR))


def command(parser, args):
    """Resolve the chosen shell after read-only queries and rc evaluation finish."""
    executable = shutil.which(args.shell)
    if executable is None:
        parser.error(i18n.get(Message.STREAM_SHELL_ERROR, shell=args.shell))
    if not args.cwd.is_dir():
        parser.error(i18n.get(Message.STREAM_CWD_ERROR, path=args.cwd))
    return [os.path.abspath(executable), *args.shell_args]
