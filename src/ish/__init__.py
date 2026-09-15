"""Public version information and module discovery for the ish package."""

from __future__ import annotations

from typing import Any

VERSION: tuple[int, int, int] = (1, 1, 0)
__version__: str = ".".join(map(str, VERSION))


def __getattr__(name: str) -> Any:
    """Look up public version names and raise AttributeError for unknown attributes."""
    if name in {"__version__", "VERSION"}:
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return sorted module names, including the version attributes."""
    return sorted(
        {
            *globals().keys(),
            "__version__",
            "VERSION",
        }
    )


__all__ = ["__version__", "VERSION"]
