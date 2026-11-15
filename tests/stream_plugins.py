"""Assemble explicit example plugins for tests without a product demo mode."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src/ish/stream/bundled"
EXAMPLES = ROOT / "examples/stream_plugins"
SAMPLE_PLUGINS = ("stream_bar", "stream_popup")
SHELL_PLUGINS = ("stream_bash", "stream_zsh", "stream_tcsh")
DEMO_PLUGINS = (*SAMPLE_PLUGINS, *SHELL_PLUGINS)
STAGING_DIRECTORY = ".stream-plugins"


def copy_plugins(destination: Path) -> Path:
    """Combine shell providers and sample UI as a normal external plugin directory."""
    for source in (BUNDLED, EXAMPLES):
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    return destination


def plugin_options(home: Path, *, demo=False, names=(), directory=None):
    """Translate a fixture convenience into explicit public plugin arguments."""
    selected = (*DEMO_PLUGINS, *names) if demo else tuple(names)
    selected = tuple(dict.fromkeys(selected))
    if directory is None and any(name in SAMPLE_PLUGINS for name in selected):
        directory = copy_plugins(home / STAGING_DIRECTORY)
    arguments = []
    for name in selected:
        arguments.extend(("--plugin", name))
    if directory is not None:
        arguments.extend(("--plugin-dir", str(directory)))
    return arguments
