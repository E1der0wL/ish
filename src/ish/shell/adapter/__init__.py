"""Expose shell definitions and policy selection without generating runtime scripts."""

from .base import (
    ADAPTERS,
    CSH_BEHAVIOR,
    NATIVE_LIBRARIES,
    POSIX_BEHAVIOR,
    SHELL_CATEGORIES,
    SYNTAXES,
    ShellAdapter,
    ShellBehavior,
    ShellSyntax,
    get_adapter,
    preload_libraries,
)
from .parsing import ParsingPolicy

__all__ = [
    "ADAPTERS",
    "CSH_BEHAVIOR",
    "NATIVE_LIBRARIES",
    "POSIX_BEHAVIOR",
    "ParsingPolicy",
    "SHELL_CATEGORIES",
    "SYNTAXES",
    "ShellAdapter",
    "ShellBehavior",
    "ShellSyntax",
    "get_adapter",
    "preload_libraries",
]
