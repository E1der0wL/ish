"""Connect zsh syntax reads to the shared editing contract without native ZLE."""

import re
import shlex
from pathlib import Path

from ..constants import bytes_to_shell_escape
from ..handoff import EDIT, MODE, VERSION
from ..protocol import EOT, SOH, US

_NATIVE_WRAP = "_ish_wrap_continuation() {"
_SAVED_WRAP = "_ish_native_continuation() {"
_PRIMARY = '    _ish_update "$1"\n    _ish_forward'
_RAW = re.compile(r"(?m)^(\s*builtin printf '[^\n]*LR;[^\n]*' \"\$_ish_prompt_id\" 0)$")
_EVENT_FORMAT = SOH + EDIT.encode() + US + b"%s;%s;%s;%s;%s" + EOT
_MODE_FORMAT = SOH + MODE.encode() + US + b"%s;%s;%s;%s" + EOT

_FUNCTIONS = r"""
# Semantic events are independent of the existing LI cancellation exchange.
_ish_edit_available() {
    [[ -o promptsubst && ! -o zle && -n ${_ish_line_interrupt_body-} &&
       ${functions[TRAPINT]-} == "$_ish_line_interrupt_body" &&
       -n ${_ish_line_preexec_body-} &&
       ${functions[preexec]-} == "$_ish_line_preexec_body" ]]
}
_ish_edit_mode() {
    local previous=$?
    _ish_edit_enabled=0
    if _ish_edit_available; then _ish_edit_enabled=1; fi
    builtin printf '@MODE_FORMAT@' @VERSION@ @TOKEN@ "$_ish_prompt_id" "$_ish_edit_enabled" >> "$_ish_pipe"
    return "$previous"
}
_ish_edit_event() {
    local previous=$?
    if (( ! ${_ish_edit_enabled:-0} || ZSH_SUBSHELL != 0 )); then return "$previous"; fi
    local incoming=$(<@SUBMISSION@)
    if [[ "$incoming" != <-> || ${#incoming} -gt 19 ]] || (( incoming <= 0 )); then
        return "$previous"
    fi
    if (( incoming != ${_ish_edit_submission:-0} )); then
        if (( incoming < ${_ish_edit_submission:-0} )); then return "$previous"; fi
        _ish_edit_submission=$incoming
        _ish_edit_request=0
    fi
    (( ++_ish_edit_request ))
    builtin printf '@EVENT_FORMAT@' @VERSION@ @TOKEN@ "$_ish_edit_submission" "$_ish_edit_request" "$1" >> "$_ish_pipe"
    return "$previous"
}
_ish_edit_ready() {
    _ish_edit_event C
    # A registered math function returns its last arithmetic value. Keep the
    # native prompt suffix selector at zero without executing a subshell.
    (( 0 )); builtin true
}
functions -M ish_edit_ready 0 0 _ish_edit_ready
typeset -A _ish_edit_prompt=(0 '')
_ish_wrap_continuation() {
    _ish_native_continuation
    if _ish_edit_available; then
        PS2='${_ish_edit_prompt[$((ish_edit_ready()))]}'
    fi
}
"""


def install(source: str, *, submission_path: Path, token: str) -> str:
    """Bind explicit C/R requests and primary capability to a rendered zsh script.

    The parent must withhold all bytes after each submitted physical line until
    a matching event grants their next consumer. The existing primary helper
    still drains native leftovers and blocks parser input until its ACK. Merely
    installing these observations does not activate semantic ownership.
    """
    if (
        source.count(_NATIVE_WRAP) != 1
        or source.count(_PRIMARY) != 1
        or len(_RAW.findall(source)) != 1
        or "_ish_edit_event()" in source
    ):
        raise ValueError("Unexpected zsh integration boundaries")
    source = source.replace(_NATIVE_WRAP, _SAVED_WRAP, 1)
    source = source.replace(
        _PRIMARY, '    _ish_update "$1"\n    _ish_edit_mode\n    _ish_forward', 1
    )
    source = _RAW.sub(
        lambda match: match[1] + "\n                _ish_edit_event R", source, count=1
    )
    bindings = {
        "@EVENT_FORMAT@": bytes_to_shell_escape(_EVENT_FORMAT),
        "@MODE_FORMAT@": bytes_to_shell_escape(_MODE_FORMAT),
        "@VERSION@": shlex.quote(VERSION),
        "@TOKEN@": shlex.quote(token),
        "@SUBMISSION@": shlex.quote(str(submission_path)),
    }
    pattern = re.compile("|".join(map(re.escape, bindings)))
    return source + pattern.sub(lambda match: bindings[match[0]], _FUNCTIONS)
