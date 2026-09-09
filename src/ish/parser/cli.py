from __future__ import annotations

import argparse
import os
import pwd
import textwrap
from functools import cached_property
from typing import Optional

from ish.lang import i18n

__all__= ['ArgumentParser']


class AltArgumentParser(argparse.ArgumentParser):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def print_help(self, file=None):
		help_message = self.format_help()
		print(help_message)


class Formatter(argparse.RawTextHelpFormatter):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def _format_usage(self, usage, actions, groups, prefix):
		super()._format_usage(usage, actions, groups, prefix)
		return None

	def _format_text(self, text):
		return text

	def _format_action(self, action):
		help_message = super()._format_action(action)
		return help_message


class StoreValue(argparse.Action):
	def __call__(self, parser, namespace, values, option_string=None):
		if not values:
			setattr(namespace, self.dest, values)
		else:
			setattr(namespace, self.dest, values)


class ArgumentParser:
	def __init__(self):
		self.login_shell: str = str(os.path.basename(pwd.getpwuid(os.getuid()).pw_shell))
		i18n.load_messages()
		self._description = textwrap.dedent(f"""\

		""")

	@cached_property
	def option(self) -> AltArgumentParser:
		parser = AltArgumentParser(description=self._description, formatter_class=Formatter)
		parser.add_argument(
			'shell',
			nargs='?',
			default=self.login_shell,
			help=i18n.get('cli_shell_option_help'),
		)
		parser.add_argument(
			'-l', '--lang',
			type=str,
			help=i18n.get('cli_lang_option_help'),
		)
		return parser

	def parse(self, args: Optional[list] = None) -> argparse.Namespace:
		from ish.config import config
		config.ensure_directories()

		option = self.option.parse_args(args)
		from ish.log import register_logger
		register_logger(name='global')

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
		self.option.print_help()
