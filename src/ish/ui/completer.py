from __future__ import annotations

import os
import shlex

from ish.parser.completion import completion_word
from typing import Union, Iterable, Mapping, Pattern, Callable, Sequence

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import AnyFormattedText

__all__ = ['PathCompleter', 'PromptCompleter']


class PathCompleter(Completer):
	def __init__(
		self,
		only_directories: bool = False,
		get_paths: Callable[[], list[str]] | None = None,
		file_filter: Callable[[str], bool] | None = None,
		expanduser: bool = False,
		quote: Callable[[str], str] = shlex.quote,
	) -> None:
		self.only_directories = only_directories
		self.get_paths = get_paths or (lambda: ["."])
		self.file_filter = file_filter or (lambda _: True)
		self.expanduser = expanduser
		self.quote = quote

	def get_completions(self, document, complete_event):
		word = completion_word(document.text_before_cursor)
		if word.dynamic or (document.text_after_cursor and document.text_after_cursor[0] not in ' \t\n;&|'):
			return
		raw_word = document.text_before_cursor[word.start:]
		text = os.path.expanduser(word.value) if self.expanduser and raw_word.startswith('~') else word.value
		dirname, prefix = os.path.split(text)
		try:
			matches = {}
			for root in self.get_paths():
				directory = os.path.join(root or '.', dirname)
				with os.scandir(directory) as entries:
					for entry in entries:
						if entry.name.startswith(prefix) and self.file_filter(entry.path):
							is_dir = entry.is_dir()
							if is_dir or not self.only_directories:
								matches[entry.name] = is_dir
			for name, is_dir in sorted(matches.items()):
				path = os.path.join(dirname, name) + ('/' if is_dir else '')
				yield Completion(
					text=self.quote(path) + ('' if is_dir else ' '),
					start_position=word.start - document.cursor_position,
					display=name + ('/' if is_dir else ''),
				)
		except (TypeError, OSError):
			return


class PromptCompleter(Completer):
	def __init__(
		self,
		words: Union[Sequence[str], Callable[[], Sequence[str]]],
		cwd: Callable[[], list[str]] | None = None,
		ignore_case: bool = False,
		display_dict: Union[Mapping[str, AnyFormattedText], None] = None,
		meta_dict: Union[Mapping[str, AnyFormattedText], None] = None,
		match_middle: bool = False,
		pattern: Union[Pattern[str], None] = None,
		quote: Callable[[str], str] = shlex.quote,
	) -> None:
		self.words = words
		self.ignore_case = ignore_case
		self.display_dict = display_dict or {}
		self.meta_dict = meta_dict or {}
		self.match_middle = match_middle
		self.pattern = pattern
		self.quote = quote
		self.path_completer = PathCompleter(
			expanduser=True,
			get_paths=cwd,
			quote=quote,
		)

	def get_completions(self, document, complete_event) -> Iterable[Completion]:
		words = self.words
		if callable(words):
			words = words()

		word = completion_word(document.text_before_cursor)
		yield from self.path_completer.get_completions(document, complete_event)
		if not word.command or word.dynamic or (document.text_after_cursor and document.text_after_cursor[0] not in ' \t\n;&|'):
			return
		prefix = word.value.casefold() if self.ignore_case else word.value
		for command in words:
			candidate = command.casefold() if self.ignore_case else command
			matches = prefix in candidate if self.match_middle else candidate.startswith(prefix)
			if matches:
				yield Completion(
					text=self.quote(command) + ' ',
					start_position=word.start - document.cursor_position,
					display=self.display_dict.get(command, command),
					display_meta=self.meta_dict.get(command, ''),
				)
