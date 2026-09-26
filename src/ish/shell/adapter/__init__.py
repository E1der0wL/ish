"""Expose shell definitions and policy selection without generating runtime scripts."""

from .parsing import ParsingPolicy
from .registry import (
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
