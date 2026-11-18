"""Locate resources shipped beside ish's dedicated, relocatable CPython runtime."""

from __future__ import annotations

import sys
from pathlib import Path

RUNTIME_MARKER = "ish-runtime.json"
VT_LIBRARY_PATH = Path("libexec/ghostty-vt/libish_vt.so")


def bundled_root() -> Path | None:
    """Return the marked distribution root, independent of cwd and environment.

    A missing helper in a marked installation is a packaging error, not permission
    to compile code on the user's host. Ordinary source environments have no marker.
    """
    prefix = Path(sys.prefix).resolve()
    return prefix.parent if (prefix / RUNTIME_MARKER).is_file() else None


def bundled_vt_library() -> Path | None:
    """Return the installed VT bridge path without hiding a packaging error."""
    root = bundled_root()
    return root / VT_LIBRARY_PATH if root else None
