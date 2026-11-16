"""Register editable tcsh startup policy without owning terminal input or output."""

from __future__ import annotations

import sys
from pathlib import Path

from ish.lang import Message, i18n
from ish.stream import request
from ish.stream.scripts import render

PLUGIN_META = {
    "version": "1.0.0",
    "description": "Native tcsh primary-prompt and popup integration",
}
STREAM_API = 1
SHELL = "tcsh"
SUPPORTED_ARGS = ((), ("-i",))


def matches(argv):
    """Select tcsh without inferring the native editor's input state."""
    return bool(argv) and Path(argv[0]).name == SHELL


def setup(api):
    """Offer this shell's startup builder to the shared stream host."""
    api.add_shell(matches, build)


def _quote(value: str) -> str:
    """Quote one tcsh literal without interpolating shell or history expressions."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(i18n.get(Message.STREAM_SHELL_PATH))
    return "'" + value.replace("'", "'\\''").replace("!", "\\!") + "'"


def _fold(source: str) -> str:
    """Keep tcsh setup on one source input line to preserve postcmd ordering."""
    return "; ".join(
        line
        for line in source.strip().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def build(argv, env, directory: Path, nonce, popup_socket, *, script_dir=None):
    """Prepare external tcsh fragments before changing the child environment."""
    if tuple(argv[1:]) not in SUPPORTED_ARGS:
        raise ValueError(i18n.get(Message.STREAM_TCSH_STARTUP))
    values = {
        "@NONCE@": nonce,
        "@PYTHON@": _quote(sys.executable),
        "@HELPER@": _quote(str(Path(request.__file__))),
        "@SOCKET@": _quote(popup_socket or ""),
        "@HOME@": _quote(env["HOME"]),
    }
    values["@POPUP_SETUP@"] = (
        _fold(render("tcsh-popup.csh", values, script_dir)) if popup_socket else ""
    )
    values["@BINDINGS@"] = (
        _fold(render("tcsh-bindings.csh", values, script_dir)) if popup_socket else ""
    )
    values["@DECISION@"] = _fold(
        render(
            "tcsh-decision.csh" if popup_socket else "tcsh-hide.csh", values, script_dir
        )
    )
    source = _fold(render("tcsh.csh", values, script_dir)) + "\n"
    (directory / ".tcshrc").write_text(source, encoding="utf-8")
    return list(argv), dict(env, HOME=str(directory))
