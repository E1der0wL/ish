"""Register editable ZLE startup policy without owning terminal input or output."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from ish.stream import request
from ish.stream.scripts import render

PLUGIN_META = {
    "version": "1.0.0",
    "description": "Native zsh primary-prompt and popup integration",
}
STREAM_API = 1
SHELL = "zsh"
SUPPORTED_ARGS = ((), ("-i",))


def matches(argv):
    """Select zsh without inferring the native editor's input state."""
    return bool(argv) and Path(argv[0]).name == SHELL


def setup(api):
    """Offer this shell's startup builder to the shared stream host."""
    api.add_shell(matches, build)


def build(argv, env, directory: Path, nonce, popup_socket, *, script_dir=None):
    """Preserve user startup paths and attach the selected external ZLE scripts."""
    if tuple(argv[1:]) not in SUPPORTED_ARGS:
        raise ValueError("zsh prompt decorations require non-login interactive startup")
    private = directory / "zsh"
    script = private / "widgets.zsh"
    source = render(
        "zsh.zsh",
        {
            "@NONCE@": shlex.quote(nonce),
            "@PYTHON@": shlex.quote(sys.executable),
            "@HELPER@": shlex.quote(str(Path(request.__file__))),
            "@SOCKET@": shlex.quote(popup_socket or ""),
        },
        script_dir,
    )
    startup_env = render(
        "zsh.env",
        {
            "@ZDOT_SET@": "1" if "ZDOTDIR" in env else "0",
            "@ZDOTDIR@": shlex.quote(env.get("ZDOTDIR", "")),
            "@PRIVATE@": shlex.quote(str(private)),
        },
        script_dir,
    )
    startup_rc = render("zsh.rc", {"@SCRIPT@": shlex.quote(str(script))}, script_dir)
    private.mkdir()
    script.write_text(source, encoding="utf-8")
    (private / ".zshenv").write_text(startup_env, encoding="utf-8")
    (private / ".zshrc").write_text(startup_rc, encoding="utf-8")
    environment = dict(env, ZDOTDIR=str(private))
    return [argv[0], "-i"], environment
