"""Resolve translated startup options before touching a terminal session."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from logging.handlers import MemoryHandler
from pathlib import Path

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n
from ish.log import LOG_LEVELS, session_logging

from .settings import ESCAPE_DELAY, RcError, Settings

PROGRAM = "ish"
LOGIN_ARGS = ("-l",)
STARTUP_LOG_CAPACITY = 4
OPTIONS = (
    (("-h", "--help"), Message.CLI_HELP_OPTION_HELP, {"action": "store_true"}),
    (("-c", "--config"), Message.MANAGE_CONFIG_OPTION, {"action": "store_true"}),
    (("--version",), Message.CLI_VERSION_OPTION_HELP, {"action": "store_true"}),
    (("--diagnose",), Message.CLI_DIAGNOSE_OPTION_HELP, {"action": "store_true"}),
    (("--home",), Message.CLI_HOME_OPTION_HELP, {"type": Path, "metavar": "DIR"}),
    (("-l", "--lang"), Message.CLI_LANG_OPTION_HELP, {"metavar": "LANG"}),
    (("--no-rc",), Message.STARTUP_NO_RC_HELP, {"action": "store_true"}),
    (
        ("--escape-delay",),
        Message.STARTUP_ESCAPE_DELAY_HELP,
        {"type": float, "metavar": "SECONDS"},
    ),
    (("--shell",), Message.STARTUP_SHELL_HELP, {"metavar": "SHELL"}),
    (("--cwd",), Message.STARTUP_CWD_HELP, {"type": Path, "metavar": "DIR"}),
    (
        ("--log-file",),
        Message.STARTUP_LOG_FILE_HELP,
        {"type": Path, "nargs": "?", "metavar": "PATH"},
    ),
    (
        ("--log-level",),
        Message.STARTUP_LOG_LEVEL_HELP,
        {"type": str.upper, "choices": tuple(LOG_LEVELS)},
    ),
)


class Formatter(argparse.HelpFormatter):
    """Translate the usage prefix while retaining argparse's tested layout."""

    METAVARS = {
        "DIR": Message.STARTUP_META_DIR,
        "SECONDS": Message.STARTUP_META_SECONDS,
        "SHELL": Message.STARTUP_META_SHELL,
        "NAME": Message.STARTUP_META_NAME,
        "PATH": Message.STARTUP_META_PATH,
        "ARG": Message.STARTUP_META_ARG,
        "LANG": Message.STARTUP_META_LANG,
    }

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(
            usage,
            actions,
            groups,
            i18n.get(Message.STARTUP_USAGE) if prefix is None else prefix,
        )

    def _metavar_formatter(self, action, default_metavar):
        metavar = action.metavar or default_metavar
        if metavar in self.METAVARS:
            # Resolve labels at rendering time: rc validation can select a
            # language after the parser has already checked the command line.
            action = copy.copy(action)
            action.metavar = i18n.get(self.METAVARS[metavar])
        return super()._metavar_formatter(action, default_metavar)


class Parser(argparse.ArgumentParser):
    """Use the shared message catalog for CLI error presentation."""

    # argparse has no per-instance gettext hook. Normalize its pinned Python 3.12
    # diagnostics here, keeping the parser's argument handling and exception causes.
    ARGUMENT_ERRORS = (
        (
            re.compile(r"unrecognized arguments: (?P<arguments>.*)", re.S),
            Message.STARTUP_ARG_UNKNOWN,
        ),
        (re.compile(r"expected one argument"), Message.STARTUP_ARG_EXPECTED),
        (
            re.compile(r"not allowed with argument (?P<argument>.*)", re.S),
            Message.STARTUP_ARG_CONFLICT,
        ),
        (
            re.compile(
                r"invalid choice: (?P<value>.*?) \(choose from (?P<choices>.*)\)", re.S
            ),
            Message.STARTUP_ARG_CHOICE,
        ),
        (
            re.compile(r"invalid (?P<kind>.*?) value: (?P<value>.*)", re.S),
            Message.STARTUP_ARG_VALUE,
        ),
        (
            re.compile(r"ignored explicit argument (?P<value>.*)", re.S),
            Message.STARTUP_ARG_EXPLICIT,
        ),
    )
    ARGUMENT_CONTEXT = re.compile(r"argument (?P<argument>[^:]+): (?P<error>.*)", re.S)

    @classmethod
    def _message(cls, message):
        """Translate stdlib-owned diagnostics without changing argparse's global state."""
        context = cls.ARGUMENT_CONTEXT.fullmatch(message)
        if context is not None:
            return i18n.get(
                Message.STARTUP_ARG_ERROR,
                argument=context["argument"],
                error=cls._message(context["error"]),
            )
        for pattern, key in cls.ARGUMENT_ERRORS:
            matched = pattern.fullmatch(message)
            if matched is not None:
                return i18n.get(key, **matched.groupdict())
        return message

    def __init__(self, *args, **kwargs):
        """Keep bootstrap usage and full help on the same translated formatter."""
        kwargs.setdefault("formatter_class", Formatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        """Translate both argument context and detail at the parser boundary."""
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: {i18n.get(Message.STARTUP_PARSE_ERROR, error=self._message(message))}\n",
        )


@dataclass(frozen=True)
class Startup:
    """Supply entry-point policy while sharing read-only parsing and precedence."""

    program: str
    description: Message
    version: Message
    login_help: Message
    options: tuple
    paths: tuple
    settings: type[Settings]
    load: Callable[[Path], Settings]
    diagnose: Callable[[argparse.Namespace], None]
    field_mergers: Mapping[str, Callable[[object, object], object]] = field(
        default_factory=dict
    )


def _path(value):
    return Path(value).expanduser().resolve()


def report_logs(records, logger):
    """Apply the selected session policy to diagnostics collected before translation."""
    for record in records:
        if logger.isEnabledFor(record.levelno):
            logger.handle(record)


def parse(argv=None, *, startup):
    """Resolve CLI over ish rc settings; queries never execute the Python rc."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Only the explicit language is interpreted first. Invalid unrelated options
    # must still use a later --lang, without running the arbitrary Python rc.
    language = Parser(
        prog=startup.program, add_help=False, allow_abbrev=False, exit_on_error=False
    )
    language.add_argument("--lang", "-l")
    first = argparse.Namespace(lang=None, home=None)
    try:
        language.parse_known_args(argv, namespace=first)
    except argparse.ArgumentError:
        # The complete bootstrap below retains the original failure and order.
        pass
    bootstrap = Parser(
        prog=startup.program, add_help=False, allow_abbrev=False, exit_on_error=False
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
        i18n.load(first.lang, logger=startup_logger)
    except ValueError:
        # A malformed explicit language has no usable catalog of its own.
        i18n.load(logger=startup_logger)
        bootstrap.error(i18n.get(Message.I18N_LANGUAGE_ERROR))
    if home_error is not None:
        bootstrap.error(
            i18n.get(Message.CLI_HOME_ERROR, path=first.home, error=home_error)
        )
    if bootstrap_error is not None:
        bootstrap.error(str(bootstrap_error))
    parser = Parser(
        prog=startup.program,
        description=i18n.get(startup.description),
        add_help=False,
        allow_abbrev=False,
        formatter_class=Formatter,
    )
    parser._optionals.title = i18n.get(Message.CLI_OPTIONS_TITLE)
    parser._positionals.title = i18n.get(Message.CLI_ARGUMENTS_TITLE)
    plugins = parser.add_mutually_exclusive_group()
    queries = parser.add_mutually_exclusive_group()
    plugins.add_argument(
        "--no-plugins",
        action="store_true",
        help=i18n.get(Message.STARTUP_NO_PLUGINS_HELP),
    )
    for names, message, settings in startup.options:
        settings = dict(settings)
        if names == ("--log-file",):
            settings["const"] = config.LOG_FILE
        target = (
            plugins
            if names == ("--plugin",)
            else queries
            if names[0] in ("-h", "-c", "--version", "--diagnose")
            else parser
        )
        target.add_argument(
            *names,
            help=(
                i18n.get(
                    message, path=config.PLUGIN_SCRIPT_DIR, escape_delay=ESCAPE_DELAY
                )
                if message is not None
                else argparse.SUPPRESS
            ),
            **settings,
        )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help=i18n.get(startup.login_help))
    mode.add_argument(
        "--shell-arg",
        action="append",
        metavar="ARG",
        help=i18n.get(Message.STARTUP_SHELL_ARG_HELP),
    )
    args = parser.parse_args(argv)
    if args.config:
        terminal(parser)
        for name, value in vars(args).items():
            if (
                name not in {"config", "home", "lang", "no_rc"}
                and value is not None
                and value is not False
            ):
                parser.error(i18n.get(Message.MANAGE_OPTION_CONFLICT))
        from ish.manage.app import settings_window

        with session_logging() as log:
            report_logs(tuple(records.buffer), log.logger)
            records.close()
            settings_window(startup, no_rc=args.no_rc, language=first.lang)
        return parser, args, True
    information = args.help or args.version or args.diagnose
    try:
        # Reject a non-terminal launch before running arbitrary user initialization.
        if not information:
            terminal(parser)
        selected = (
            startup.settings()
            if information or args.no_rc
            else startup.load(config.RC_FILE)
        )
        _rc_language(selected, first.lang, startup_logger, records)
        for name in startup.paths:
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, _path(value))
        if args.shell is not None and ("/" in args.shell or args.shell.startswith("~")):
            args.shell = os.path.abspath(os.path.expanduser(args.shell))
        for name in selected.__dataclass_fields__:
            option = "plugin" if name == "plugins" else name
            value = getattr(args, option, None)
            if value is not None:
                merge = startup.field_mergers.get(name)
                if merge is not None:
                    value = merge(getattr(selected, name), value)
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
        from ish.manage.app import help_window

        help_window(parser, no_rc=args.no_rc)
        return parser, args, True
    if args.version:
        print(i18n.get(startup.version, version=__version__))
        return parser, args, True
    if args.diagnose:
        startup.diagnose(args)
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
        i18n.load(settings.lang, logger=logger)


def terminal(parser):
    """Require a controlling terminal before executing user startup code."""
    if os.name != "posix":
        parser.error(i18n.get(Message.STARTUP_PLATFORM_ERROR))
    if not os.isatty(0) or not os.isatty(1):
        parser.error(i18n.get(Message.STARTUP_TTY_ERROR))


def command(parser, args):
    """Resolve the chosen shell after read-only queries and rc evaluation finish."""
    executable = shutil.which(args.shell)
    if executable is None:
        parser.error(i18n.get(Message.STARTUP_SHELL_ERROR, shell=args.shell))
    if not args.cwd.is_dir():
        parser.error(i18n.get(Message.STARTUP_CWD_ERROR, path=args.cwd))
    return [os.path.abspath(executable), *args.shell_args]
