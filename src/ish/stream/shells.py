"""Install optional native-shell notifications without replacing its line editor."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
TCSH_ARGS = ((), ("-i",))


_TCSH_STARTUP = r"""setenv HOME @HOME@
set home = @HOME@
set _ish_stream_rc = "$home/.tcshrc"
if (! -r "$_ish_stream_rc") set _ish_stream_rc = "$home/.cshrc"
if (-r "$_ish_stream_rc") source "$_ish_stream_rc"
set _ish_stream_status = $status
if (1) glob
alias _ish_stream_user_precmd "`alias precmd`"
alias _ish_stream_user_postcmd "`alias postcmd`"
unalias postcmd
set _ish_stream_serial = 0
set _ish_stream_active = 0
@POPUP_SETUP@
alias _ish_stream_prompt 'set _ish_stream_prompt_status = $status; set _ish_stream_active = 0; if (1) glob; set status = $_ish_stream_prompt_status; _ish_stream_user_precmd; if (1) glob; if ("`alias postcmd`" != "_ish_stream_hide") alias _ish_stream_user_postcmd "`alias postcmd`"; alias postcmd _ish_stream_hide; @BINDINGS@ @ _ish_stream_serial ++; set _ish_stream_active = 1; /usr/bin/printf "\033]777;ish;@NONCE@;%s;P\007" "$_ish_stream_serial" >! /dev/tty; set status = $_ish_stream_prompt_status'
alias _ish_stream_hide 'set _ish_stream_hide_status = $status; if (1) glob; @DECISION@ if ($_ish_stream_active) @ _ish_stream_serial ++; if ($_ish_stream_active) /usr/bin/printf "\033]777;ish;@NONCE@;%s;%s\007" "$_ish_stream_serial" "$_ish_stream_event" >! /dev/tty; set _ish_stream_active = 0; set status = $_ish_stream_hide_status; _ish_stream_user_postcmd; set status = $_ish_stream_hide_status'
alias precmd _ish_stream_prompt
alias postcmd _ish_stream_hide
set status = $_ish_stream_status
"""

_POPUP_SETUP = r"""set _ish_stream_python = @PYTHON@
set _ish_stream_helper = @HELPER@
set _ish_stream_socket = @SOCKET@
set _ish_stream_popup_allowed = 0
alias _ish_stream_popup 'set _ish_stream_popup_status = $status ; if ( $_ish_stream_popup_allowed ) $_ish_stream_python:q $_ish_stream_helper:q --socket $_ish_stream_socket:q --nonce @NONCE@ --serial $_ish_stream_serial ; if ( $_ish_stream_popup_allowed ) @ _ish_stream_serial ++ ; if ( $_ish_stream_popup_allowed ) set _ish_stream_active = 1 ; if ( $_ish_stream_popup_allowed ) /usr/bin/printf "\033]777;ish;@NONCE@;%s;P\007" "$_ish_stream_serial" > ! /dev/tty ; set status = $_ish_stream_popup_status'
set _ish_stream_popup_body = "`alias _ish_stream_popup`"
"""

_POPUP_BINDINGS = (
    r'bindkey -c "\eOQ" _ish_stream_popup; bindkey -c "\e[12~" _ish_stream_popup;'
)
_POPUP_DECISION = r'set _ish_stream_popup_allowed = 0; set _ish_stream_event = H; set _ish_stream_command = "\!#:q"; if ($_ish_stream_active && "$_ish_stream_command" == "$_ish_stream_popup_body") set _ish_stream_popup_allowed = 1; if ($_ish_stream_popup_allowed) set _ish_stream_event = B;'


def _quote(value: str) -> str:
    """Quote one tcsh literal without interpolating shell or history expressions."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Shell integration paths must not contain line breaks or NUL")
    return "'" + value.replace("'", "'\\''").replace("!", "\\!") + "'"


class Integration:
    """Own startup files for explicitly enabled prompt-only decorations."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        nonce: str,
        popup_socket: str | None = None,
    ):
        """Prepare tcsh's primary-entry and conservative-hide notifications."""
        if not argv or Path(argv[0]).name != "tcsh" or tuple(argv[1:]) not in TCSH_ARGS:
            raise ValueError(
                "Prompt decorations currently require tcsh without startup flags"
            )
        if not NONCE_PATTERN.fullmatch(nonce):
            raise ValueError("Invalid shell notification nonce")
        home = env.get("HOME")
        if not home or not os.path.isabs(home):
            raise ValueError("Shell integration requires an absolute HOME")
        quoted_home = _quote(home)
        self.argv = list(argv)
        self.env = dict(env)
        self.directory = tempfile.TemporaryDirectory(prefix="ish-stream-tcsh-")
        self.env["HOME"] = self.directory.name
        # tcsh invokes postcmd once per source input line. Keep setup on the
        # original line, which began before the user's postcmd existed, so our
        # bookkeeping never adds calls to the user's hook.
        startup = _TCSH_STARTUP.replace(
            "@POPUP_SETUP@", _POPUP_SETUP if popup_socket else ""
        )
        startup = startup.replace("@BINDINGS@", _POPUP_BINDINGS if popup_socket else "")
        startup = startup.replace(
            "@DECISION@",
            _POPUP_DECISION if popup_socket else "set _ish_stream_event = H;",
        )
        replacements = {
            "@NONCE@": nonce,
            "@PYTHON@": _quote(sys.executable),
            "@HELPER@": _quote(str(Path(__file__).with_name("request.py"))),
            "@SOCKET@": _quote(popup_socket or ""),
            "@HOME@": quoted_home,
        }
        for placeholder, value in replacements.items():
            startup = startup.replace(placeholder, value)
        script = "; ".join(line for line in startup.strip().splitlines() if line) + "\n"
        Path(self.directory.name, ".tcshrc").write_text(script, encoding="utf-8")

    def close(self) -> None:
        """Remove only this integration's private startup directory."""
        self.directory.cleanup()

    def __enter__(self) -> Integration:
        """Keep generated startup files alive for the child session."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Release startup files after the child is finished."""
        self.close()
