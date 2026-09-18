"""Locate resources shipped beside ish's dedicated, relocatable CPython runtime."""

from __future__ import annotations

import sys
from pathlib import Path

RUNTIME_MARKER = "ish-runtime.json"


def bundled_root() -> Path | None:
    """Return the marked distribution root, independent of cwd and environment.

    A missing helper in a marked installation is a packaging error, not permission
    to compile code on the user's host. Ordinary source environments have no marker.
    """
    prefix = Path(sys.prefix).resolve()
    return prefix.parent if (prefix / RUNTIME_MARKER).is_file() else None


def bundled_forward_binary() -> Path | None:
    """Return the expected helper path, including when that file is missing."""
    # Import lazily: shell.__init__ also loads the integration module.
    from ish.shell.constants import BUNDLED_FORWARD_DIRECTORY, FORWARD_BINARY

    root = bundled_root()
    return root / BUNDLED_FORWARD_DIRECTORY / FORWARD_BINARY if root else None
