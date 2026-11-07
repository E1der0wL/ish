"""Public version information and module discovery for the ish package."""

from __future__ import annotations

from typing import Any

VERSION: tuple[int, int, int] = (1, 2, 1)
__version__: str = ".".join(map(str, VERSION))
_VERSION_NAMES = ("__version__", "VERSION")
__all__ = list(_VERSION_NAMES)


def __getattr__(name: str) -> Any:
    """Look up public version names and raise AttributeError for unknown attributes."""
    if name in _VERSION_NAMES:
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return sorted module names, including the version attributes."""
    return sorted(globals().keys() | _VERSION_NAMES)
