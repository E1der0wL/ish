"""Register editable Bash startup policy without owning terminal input or output."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from ish.stream import request
from ish.stream.scripts import render

PLUGIN_META = {
    "version": "1.0.0",
    "description": "Native Bash primary-prompt and popup integration",
}
STREAM_API = 1
SHELL = "bash"
SUPPORTED_ARGS = ((), ("-i",))


def matches(argv):
    """Select Bash without inferring the native editor's input state."""
    return bool(argv) and Path(argv[0]).name == SHELL


def setup(api):
    """Offer this shell's startup builder to the shared stream host."""
    api.add_shell(matches, build)


def build(argv, env, directory: Path, nonce, popup_socket, *, script_dir=None):
    """Prepare one non-login interactive Bash without changing user rc files."""
    if tuple(argv[1:]) not in SUPPORTED_ARGS:
        raise ValueError(
            "Bash prompt decorations require a non-login interactive shell"
        )
    startup = render(
        "bash.sh",
        {
            "@RC@": shlex.quote(str(Path(env["HOME"], ".bashrc"))),
            "@NONCE@": shlex.quote(nonce),
            "@PYTHON@": shlex.quote(sys.executable),
            "@HELPER@": shlex.quote(str(Path(request.__file__))),
            "@SOCKET@": shlex.quote(popup_socket or ""),
        },
        script_dir,
    )
    path = directory / "bashrc"
    path.write_text(startup, encoding="utf-8")
    return [argv[0], "--noprofile", "--rcfile", str(path), "-i"], dict(env)
