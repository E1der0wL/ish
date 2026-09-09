from __future__ import annotations

from typing import Any, Optional, Dict, Callable

__all__ = ['ShellContext']


class ShellContext:
	def __init__(self):
		self._data = {}
		self._handlers: Dict[str, Callable] = {}
		self._updater: Dict[str, Callable[[Any], None]] = {}

	def __getattr__(self, item: str) -> Any:
		return self._data.get(item)

	def register_handler(self, category, func: Callable, callback: Optional[Callable] = None) -> None:
		self._handlers[category] = func
		if callback:
			self._updater[category] = callback

	def update(self, category, raw_data) -> None:
		handler = self._handlers.get(category)
		if handler:
			parsed_data = handler(raw_data)
			self._data[category] = parsed_data
			updater = self._updater.get(category)
			if updater:
				updater(parsed_data)
