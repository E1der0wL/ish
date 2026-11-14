"""Export the runner without loading it when importing shell contracts."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import InteractiveShell

__all__ = ["InteractiveShell"]


def __getattr__(name: str) -> Any:
    """Load the public runner only when requested, keeping contract imports acyclic."""
    if name == "InteractiveShell":
        from .engine import InteractiveShell

        globals()[name] = InteractiveShell
        return InteractiveShell
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the runner in discovery before it has been imported."""
    return sorted(globals().keys() | set(__all__))
