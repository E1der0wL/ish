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
from ish.lang import i18n

__all__ = ["ArgumentParser"]


class AltArgumentParser(argparse.ArgumentParser):
    """ArgumentParser extension providing ish's help output behavior."""

    def __init__(self, *args, **kwargs):
        """Pass caller-supplied argparse options to the base parser."""
        super().__init__(*args, **kwargs)

    def print_help(self, file=None):
        """Print formatted help to the supplied stream or standard output."""
        help_message = self.format_help()
        print(help_message, end="", file=file)


class Formatter(argparse.RawTextHelpFormatter):
    """Formatter preserving description line breaks and omitting the default usage line."""

    def __init__(self, *args, **kwargs):
        """Forward initialization arguments to RawTextHelpFormatter."""
        super().__init__(*args, **kwargs)

    def _format_usage(self, usage, actions, groups, prefix):
        """Compute the default usage text but omit the rendered usage block."""
        super()._format_usage(usage, actions, groups, prefix)
        return None

    def _format_text(self, text):
        """Preserve whitespace and line breaks in the description."""
        return text

    def _format_action(self, action):
        """Use argparse's default formatting for individual option descriptions."""
        help_message = super()._format_action(action)
        return help_message


class StoreValue(argparse.Action):
    """Argparse action that stores user values in a Namespace without conversion."""

    def __call__(self, parser, namespace, values, option_string=None):
        """Store the original option value in its destination attribute."""
        if not values:
            setattr(namespace, self.dest, values)
        else:
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
        i18n.load_messages(lang, create=False)
        self._description = i18n.get("cli_description")
        self.__dict__.pop("option", None)

    @cached_property
    def option(self) -> AltArgumentParser:
        """Create a parser whose information flags are handled after settings resolve."""
        parser = AltArgumentParser(
            prog="ish",
            description=self._description,
            formatter_class=Formatter,
            add_help=False,
            allow_abbrev=False,
        )
        parser._positionals.title = i18n.get("cli_arguments_title")
        parser._optionals.title = i18n.get("cli_options_title")
        # Defer help/version so --home and --lang work in any argument order.
        parser.add_argument(
            "-h", "--help", action="store_true", help=i18n.get("cli_help_option_help")
        )
        parser.add_argument(
            "shell",
            nargs="?",
            default=self.login_shell,
            help=i18n.get("cli_shell_option_help"),
        )
        parser.add_argument(
            "-l",
            "--lang",
            type=str,
            help=i18n.get("cli_lang_option_help"),
        )
        parser.add_argument(
            "--version", action="store_true", help=i18n.get("cli_version_option_help")
        )
        parser.add_argument(
            "--no-rc", action="store_true", help=i18n.get("cli_no_rc_option_help")
        )
        parser.add_argument(
            "--no-plugins",
            action="store_true",
            help=i18n.get("cli_no_plugins_option_help"),
        )
        parser.add_argument(
            "--home", type=Path, metavar="DIR", help=i18n.get("cli_home_option_help")
        )
        parser.add_argument(
            "--diagnose", action="store_true", help=i18n.get("cli_diagnose_option_help")
        )
        return parser

    def parse(self, args: Optional[list] = None) -> argparse.Namespace:
        """Handle read-only queries or initialize the selected interactive environment."""
        option = self.option.parse_args(args)
        if option.home is not None:
            try:
                option.home = option.home.expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                self.option.error(
                    i18n.get("cli_home_error", path=str(option.home), error=exc)
                )
            config.ISH_HOME = option.home
        self._load_messages(option.lang)

        if option.help:
            self.help()
            self.option.exit()
        if option.version:
            print(i18n.get("cli_version", version=__version__))
            self.option.exit()
        if option.diagnose:
            from ish.runtime.diagnostics import print_diagnostics

            print_diagnostics(option)
            return option

        config.ensure_directories()
        from ish.log import register_logger

        register_logger(name="global")

        # Preserve default translation-file generation for interactive startup only.
        i18n.load_messages(option.lang)

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
        self.option.print_help()
