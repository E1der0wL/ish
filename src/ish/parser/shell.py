from __future__ import annotations
import shlex
from typing import Optional, Dict
from ish.shell.adapters import get_adapter

__all__ = ['str_parser', 'dict_parser', 'alias_parser', 'simple_command']


def str_parser(raw_data: bytes, encoder: str) -> str:
	try:
		return raw_data.decode(encoder)
	except Exception:
		return ""


def dict_parser(raw_data: bytes, encoder: str, x_sep: Optional[str] = None, y_sep: str = "\n") -> Dict[str, str]:
	res = {}
	str_data = raw_data.decode(encoder)
	for line in str_data.split(y_sep):
		try:
			key, value = line.split(x_sep, maxsplit=1)
			res[key] = value
		except ValueError:
			continue
	return res


def alias_parser(raw_data: bytes, encoder: str, shell: str = 'bash') -> Dict[str, str]:
	"""Decode shell-produced aliases without evaluating their expansion text."""
	text = raw_data.decode(encoder, errors='replace')
	return get_adapter(shell).syntax.parse_aliases(text)


def simple_command(text: str) -> Optional[list[str]]:
	"""Parse literal argv. Expansion, redirection and compound syntax stay in the shell."""
	quote = None
	escaped = False
	for char in text:
		if escaped:
			escaped = False
			continue
		if char == '\\' and quote != "'":
			escaped = True
		elif quote:
			if char == quote:
				quote = None
			elif quote == '"' and char in '$`':
				return None
		elif char in "'\"":
			quote = char
		elif char in ';&|()<>\n$`*?[]{}~#':
			return None
	try:
		return shlex.split(text, posix=True)
	except ValueError:
		return None
