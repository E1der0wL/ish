"""Parse shell state bytes and literal internal-tool arguments without side effects."""

from __future__ import annotations

import os
from typing import Dict, Optional

from ish.shell.adapter import get_adapter

from .command import literal_argv

__all__ = [
    "str_parser",
    "dict_parser",
    "environ_parser",
    "alias_parser",
    "simple_command",
]


def str_parser(raw_data: bytes, encoder: str) -> str:
    """Decode state bytes into text, returning an empty string on failure."""
    try:
        return raw_data.decode(encoder)
    except Exception:
        return ""


def dict_parser(
    raw_data: bytes, encoder: str, x_sep: Optional[str] = None, y_sep: str = "\n"
) -> Dict[str, str]:
    """Split each record at its first separator and skip malformed key-value records."""
    res = {}
    str_data = raw_data.decode(encoder)
    for line in str_data.split(y_sep):
        try:
            key, value = line.split(x_sep, maxsplit=1)
            res[key] = value
        except ValueError:
            continue
    return res


def environ_parser(raw_data: bytes) -> dict[str, str]:
    """Decode OS environment records losslessly, independently of terminal encoding."""
    text = os.fsdecode(raw_data)
    records = text.split("\0" if "\0" in text else "\n")
    environ = {}
    for record in records:
        key, separator, value = record.partition("=")
        if separator:
            environ[key] = value
    return environ


def alias_parser(raw_data: bytes, encoder: str, shell: str = "bash") -> Dict[str, str]:
    """Decode shell-produced aliases without evaluating their expansion text."""
    text = raw_data.decode(encoder, errors="replace")
    return get_adapter(shell).syntax.parse_aliases(text)


def simple_command(text: str) -> Optional[list[str]]:
    """Keep the legacy POSIX literal helper; dispatch uses the active adapter policy."""
    return literal_argv(text)
