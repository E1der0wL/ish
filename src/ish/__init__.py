from __future__ import annotations
from typing import Any

__version__: str
VERSION: tuple[int, int, int] = (1, 0, 0)


def __getattr__(name: str) -> Any:
	if name in {"__version__", "VERSION"}:
		return globals()[name]
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list(str):
	return sotred(
		{
			*globals().keys(),
			"__version__",
			"VERSION",
		}
	)


__all__ = ["__version__", "VERSION"]
