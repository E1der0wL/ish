"""Read editable shell integrations without importing or compiling their code."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

DIRECTORY = Path(__file__).with_name("integrations")
HEADER = "# ish-stream-api: 1"
TOKEN = re.compile(r"@[A-Z][A-Z0-9_]*@")


class Unavailable(ValueError):
    """The optional integration cannot be prepared before starting the shell."""


def render(
    name: str, replacements: Mapping[str, str], directory: Path | None = None
) -> str:
    """Read one session's script and substitute quoted values exactly once."""
    path = (DIRECTORY if directory is None else Path(directory)) / name
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise Unavailable(f"Cannot read shell integration: {path}") from error
    header, separator, source = source.partition("\n")
    if not separator or header != HEADER:
        raise Unavailable(f"Unsupported shell integration contract: {path}")

    def replace(match):
        token = match.group()
        try:
            return replacements[token]
        except KeyError as error:
            raise Unavailable(
                f"Unknown integration placeholder {token}: {path}"
            ) from error

    return TOKEN.sub(replace, source)
