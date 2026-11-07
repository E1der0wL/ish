"""Resolve CLI settings and handle information queries before interactive startup."""

from __future__ import annotations

import argparse
import os
import pwd
from functools import cached_property
from pathlib import Path
from typing import Optional

from ish import __version__
from ish.config import config
from ish.lang import Message, i18n

__all__ = ["ArgumentParser"]

_PROGRAM = "ish"
_SWITCH = {"action": "store_true"}
_OPTIONS = (
    (("-h", "--help"), Message.CLI_HELP_OPTION_HELP, _SWITCH),
    (("shell",), Message.CLI_SHELL_OPTION_HELP, {"nargs": argparse.OPTIONAL}),
    (("-l", "--lang"), Message.CLI_LANG_OPTION_HELP, {"type": str}),
    (("--version",), Message.CLI_VERSION_OPTION_HELP, _SWITCH),
    (("--no-rc",), Message.CLI_NO_RC_OPTION_HELP, _SWITCH),
    (("--no-plugins",), Message.CLI_NO_PLUGINS_OPTION_HELP, _SWITCH),
    (("--home",), Message.CLI_HOME_OPTION_HELP, {"type": Path, "metavar": "DIR"}),
    (("--diagnose",), Message.CLI_DIAGNOSE_OPTION_HELP, _SWITCH),
)


class AltArgumentParser(argparse.ArgumentParser):
    """ArgumentParser extension providing ish's help output behavior."""

    def print_help(self, file=None):
        """Print formatted help to the supplied stream or standard output."""
        help_message = self.format_help()
        print(help_message, end="", file=file)


class Formatter(argparse.RawTextHelpFormatter):
    """Formatter preserving description line breaks and omitting the default usage line."""

    def _format_usage(self, usage, actions, groups, prefix):
        """Omit the default usage block."""
        return None

    def _format_text(self, text):
        """Preserve whitespace and line breaks in the description."""
        return text


class StoreValue(argparse.Action):
    """Argparse action that stores user values in a Namespace without conversion."""

    def __call__(self, parser, namespace, values, option_string=None):
        """Store the original option value in its destination attribute."""
        setattr(namespace, self.dest, values)


class ArgumentParser:
    """CLI entry point coordinating shell selection, user settings, and prompt startup."""

    def __init__(self):
        """Prepare defaults without creating files or importing the interactive UI."""
        self.login_shell: str = str(
            os.path.basename(pwd.getpwuid(os.getuid()).pw_shell)
        )
        self._load_messages()

    def _load_messages(self, lang: Optional[str] = None) -> None:
        """Refresh translations and cached help after the home or language changes."""
        i18n.base_path = config.LANG_DIR
        i18n.load(lang, create=False)
        self._description = i18n.get(Message.CLI_DESCRIPTION)
        self.__dict__.pop("parser", None)

    @cached_property
    def parser(self) -> AltArgumentParser:
        """Create a parser whose information flags are handled after settings resolve."""
        parser = AltArgumentParser(
            prog=_PROGRAM,
            description=self._description,
            epilog="\n".join(
                (
                    i18n.get(Message.CLI_SHELL_SUPPORT),
                    i18n.get(Message.CLI_ZSH_MODULE_HELP),
                )
            ),
            formatter_class=Formatter,
            add_help=False,
            allow_abbrev=False,
        )
        parser._positionals.title = i18n.get(Message.CLI_ARGUMENTS_TITLE)
        parser._optionals.title = i18n.get(Message.CLI_OPTIONS_TITLE)
        # Defer help/version so --home and --lang work in any argument order.
        for names, help_key, options in _OPTIONS:
            parser.add_argument(*names, help=i18n.get(help_key), **options)
        parser.set_defaults(shell=self.login_shell)
        return parser

    def parse(self, args: Optional[list] = None) -> argparse.Namespace:
        """Handle read-only queries or initialize the selected interactive environment."""
        option = self.parser.parse_args(args)
        if option.home is not None:
            try:
                option.home = option.home.expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                self.parser.error(
                    i18n.get(Message.CLI_HOME_ERROR, path=str(option.home), error=exc)
                )
            config.ISH_HOME = option.home
        self._load_messages(option.lang)

        if option.help:
            self.help()
            self.parser.exit()
        if option.version:
            print(i18n.get(Message.CLI_VERSION, version=__version__))
            self.parser.exit()
        if option.diagnose:
            from ish.runtime.diagnostics import print_diagnostics

            print_diagnostics(option)
            return option

        config.ensure_directories()
        from ish.log import register_logger

        register_logger()

        # Preserve default translation-file generation for interactive startup only.
        i18n.load(option.lang)

        if option.shell:
            from ish.plugin import plugin_manager

            if not option.no_plugins:
                plugin_manager.load()
            from ish.ui.prompt import Prompt

            prompt = Prompt(
                shell=option.shell,
                option=option,
                plugin_manager=plugin_manager,
            )
            if not option.no_rc:
                config.ensure_rc_file()
                prompt.load_rc()
            prompt.run()

        return option

    def help(self):
        """Print help from the cached CLI parser."""
        self.parser.print_help()
