"""Install Bash parser permissions without changing shared session semantics."""

import shlex

from ..constants import bytes_to_shell_escape
from ..handoff import EDIT, MODE, VERSION
from ..protocol import EOT, SOH, US

_ANCHOR = '    _ish_update "$_ish_shell_exit_code"\n    _ish_forward\n'
_INSTALLED = "# ish Bash editing requests"
MIN_VERSION = (5, 3, 9)
_TEMPLATE = r"""
# ish Bash editing requests
_ish_edit_path=@PATH@
_ish_edit_prefix='${ _ish_edit_event R; }'
_ish_edit_event() {
    local _ish_saved=$? _ish_identity
    builtin read -r _ish_identity < "$_ish_edit_path" || return "$_ish_saved"
    case $_ish_identity in ''|*[!0-9]*|0) return "$_ish_saved" ;; esac
    if [[ $_ish_identity != ${_ish_edit_submission-} ]]; then
        _ish_edit_submission=$_ish_identity
        _ish_edit_request=0
    fi
    _ish_edit_request=$((_ish_edit_request + 1))
    builtin printf @EVENT@ "$_ish_edit_submission" "$_ish_edit_request" "$1" > "$_ish_pipe"
    return "$_ish_saved"
}
_ish_edit_prompts() {
    local _ish_saved=$?
    _ish_edit_enabled=0
    # A later PROMPT_COMMAND hook could replace the prompts after our report.
    # Keep native input when that ordering or prompt expansion is unavailable.
    if (( BASH_VERSINFO[0] > @MAJOR@ ||
          (BASH_VERSINFO[0] == @MAJOR@ &&
           (BASH_VERSINFO[1] > @MINOR@ ||
            (BASH_VERSINFO[1] == @MINOR@ && BASH_VERSINFO[2] >= @PATCH@))) )) &&
       builtin shopt -q promptvars && [[ ${PROMPT_COMMAND[-1]-} == _ish_precmd ]]; then
        _ish_edit_enabled=1
        if [[ $PS0 != "$_ish_edit_prefix"* ]]; then
            _ish_edit_user_ps0=$PS0
        fi
        PS0="$_ish_edit_prefix${_ish_edit_user_ps0-}"
        PS2='${ _ish_edit_event C; }'
    elif [[ $PS0 == "$_ish_edit_prefix"* ]]; then
        PS0=${PS0#"$_ish_edit_prefix"}
    fi
    return "$_ish_saved"
}
_ish_edit_mode() {
    builtin printf @MODE@ "$_ish_prompt_id" "$_ish_edit_enabled" > "$_ish_pipe"
}
"""


def install(source: str, *, submission_path, token: str) -> str:
    """Add parser notifications and a primary capability report to one rendered script.

    The parent writes a submission identity before sending its first physical
    line. Current-shell PS2/PS0 substitutions then correlate each C/R report
    with that identity. The existing primary helper remains the input reclaim
    boundary; this adapter introduces no second acknowledgement handshake.
    """
    if _INSTALLED in source or source.count(_ANCHOR) != 1:
        raise ValueError("Bash edit installation requires one unmodified prompt hook")
    if not token or any(char not in "0123456789abcdef" for char in token):
        raise ValueError("Bash edit installation requires a hexadecimal session token")
    prefix = f"{VERSION};{token};".encode("ascii")
    event = SOH + EDIT.encode() + US + prefix + b"%s;%s;%s" + EOT
    mode = SOH + MODE.encode() + US + prefix + b"%s;%s" + EOT
    functions = (
        _TEMPLATE.replace("@PATH@", shlex.quote(str(submission_path)))
        .replace("@EVENT@", shlex.quote(bytes_to_shell_escape(event)))
        .replace("@MODE@", shlex.quote(bytes_to_shell_escape(mode)))
        .replace("@MAJOR@", str(MIN_VERSION[0]))
        .replace("@MINOR@", str(MIN_VERSION[1]))
        .replace("@PATCH@", str(MIN_VERSION[2]))
    )
    return (
        source.replace(
            _ANCHOR,
            '    _ish_edit_prompts\n    _ish_update "$_ish_shell_exit_code"\n'
            "    _ish_edit_mode\n    _ish_forward\n",
        )
        + functions
    )
