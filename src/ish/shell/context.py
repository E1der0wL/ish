"""Small state store connecting shell record parsers and change callbacks."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

__all__ = ["ShellContext"]


class ShellContext:
    """Store parsed shell state and change notifications by category."""

    def __init__(self):
        """Initialize state, parser, and update callback mappings."""
        self._data = {}
        self._handlers: Dict[str, Callable] = {}
        self._updater: Dict[str, Callable[[Any], None]] = {}

    def __getattr__(self, item: str) -> Any:
        """Read stored state as attributes, returning None for missing categories."""
        return self._data.get(item)

    def register_handler(
        self, category, func: Callable, callback: Optional[Callable] = None
    ) -> None:
        """Associate a category with a parser and an optional change callback."""
        self._handlers[category] = func
        if callback:
            self._updater[category] = callback

    def update(self, category, raw_data) -> None:
        """Parse and store a raw value, then invoke its registered change callback."""
        handler = self._handlers.get(category)
        if handler:
            parsed_data = handler(raw_data)
            self._data[category] = parsed_data
            updater = self._updater.get(category)
            if updater:
                updater(parsed_data)
