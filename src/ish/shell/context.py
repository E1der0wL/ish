"""Small state store connecting shell record parsers and change callbacks."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

__all__ = ["ShellContext"]


class ShellContext:
    """Store parsed shell state and change notifications by category."""

    def __init__(self):
        """Initialize state, parser, and update callback mappings."""
        self._data = {}
        self._parsers: Dict[str, Callable] = {}
        self._callbacks: Dict[str, Callable[[Any], None]] = {}

    def __getattr__(self, item: str) -> Any:
        """Read stored state as attributes, returning None for missing categories."""
        return self._data.get(item)

    def register(
        self, category, parser: Callable, callback: Optional[Callable] = None
    ) -> None:
        """Associate a category with a parser and an optional change callback."""
        self._parsers[category] = parser
        if callback:
            self._callbacks[category] = callback

    def update(self, category, raw_data) -> None:
        """Parse and store a raw value, then invoke its registered change callback."""
        parser = self._parsers.get(category)
        if parser:
            parsed_data = parser(raw_data)
            self._data[category] = parsed_data
            callback = self._callbacks.get(category)
            if callback:
                callback(parsed_data)
