"""Select the shell and UI language and connect settings, plugins, and the prompt."""

from __future__ import annotations

import argparse
import os
import pwd
import textwrap
from functools import cached_property
from typing import Optional

from ish.lang import i18n

__all__ = ["ArgumentParser"]


class AltArgumentParser(argparse.ArgumentParser):
    """ArgumentParser extension providing ish's help output behavior."""

    def __init__(self, *args, **kwargs):
        """Pass caller-supplied argparse options to the base parser."""
        super().__init__(*args, **kwargs)

    def print_help(self, file=None):
        """Print the current formatter's help text to standard output."""
        help_message = self.format_help()
        print(help_message)


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
        """Prepare the login shell and translations used by help output."""
        self.login_shell: str = str(
            os.path.basename(pwd.getpwuid(os.getuid()).pw_shell)
        )
        i18n.load_messages()
        self._description = textwrap.dedent("""\

		""")

    @cached_property
    def option(self) -> AltArgumentParser:
        """Create and cache the parser containing shell and language options."""
        parser = AltArgumentParser(
            description=self._description, formatter_class=Formatter
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
        return parser

    def parse(self, args: Optional[list] = None) -> argparse.Namespace:
        """Read options, then initialize plugins, user rc, and the prompt in order."""
        from ish.config import config

        config.ensure_directories()

        option = self.option.parse_args(args)
        from ish.log import register_logger

        register_logger(name="global")

        if option.lang:
            i18n.load_messages(option.lang)

        if option.shell:
            from ish.plugin import plugin_manager

            plugin_manager.load()
            from ish.ui.prompt import Prompt

            prompt = Prompt(
                shell=option.shell,
                option=option,
                plugin_manager=plugin_manager,
            )
            if config.RC_FILE.exists():
                prompt.load_rc()
            prompt.run()

        return option

    def help(self):
        """Print help from the cached CLI parser."""
        self.option.print_help()
