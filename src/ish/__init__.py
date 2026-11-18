"""Public version information for the ish package."""

from __future__ import annotations

VERSION: tuple[int, int, int] = (2, 0, 0)
__version__: str = ".".join(map(str, VERSION))
__all__ = ["__version__", "VERSION"]
