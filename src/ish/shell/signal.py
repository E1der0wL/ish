from __future__ import annotations
from typing import Callable

__all__ = ['ShellExitRequest', 'ShellPassRequest']


class ShellExitRequest(Exception):
	pass


class ShellPassRequest(Exception):
	def __init__(self, callback: Callable, command: str, *args, **kwargs):
		self.callback = callback
		self.command = command
		self.args = args
		self.kwargs = kwargs
