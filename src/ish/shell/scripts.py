"""Templates use @tokens@ so shell quoting and braces remain readable."""

import shlex

from .adapters import csh_quote
from .constants import (
    AFTER_CONTINUATION,
    AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT,
    BEFORE_BUFFERED_CONTINUATION,
    BEFORE_CONTINUATION,
    BEFORE_PROMPT,
    CSH_INTEGRATION_SCRIPT,
    CSH_UPDATE_SCRIPT,
    FORWARD_BINARY,
    OSC_TERMINATOR,
    POSIX_INTEGRATION_SCRIPT,
    POSIX_UPDATE_SCRIPT,
    PROMPT_ID_PREFIX,
    TCSH_BIND_HOOKS_SCRIPT,
    TCSH_INTEGRATION_SCRIPT,
    TCSH_PRECMD_SCRIPT,
    TCSH_WATCH_HOOKS_SCRIPT,
    ZSH_INTEGRATION_SCRIPT,
    SessionSignals,
    bytes_to_shell_escape,
)
from .protocol import EOT, RS, SOH, US, VERSION


def make_scripts(directory, *, signals=SessionSignals(), forward_path=None):
    """Return shell-specific script filenames and session-rendered source.

    Quote paths for each shell syntax and use shared constants for state transfer and
    prompt signals.
    """
    scope = signals.scope
    forward_path = forward_path or directory / FORWARD_BINARY
    tokens = {
        "@VERSION@": VERSION.decode("ascii"),
        "@FORWARD@": shlex.quote(str(forward_path)),
        "@CSH_FORWARD@": csh_quote(str(forward_path)),
        "@CSH_UPDATE@": csh_quote(str(directory / CSH_UPDATE_SCRIPT)),
        "@CSH_HOOK@": csh_quote(str(directory / TCSH_PRECMD_SCRIPT)),
        "@CSH_BIND@": csh_quote(str(directory / TCSH_BIND_HOOKS_SCRIPT)),
        "@CSH_WATCH@": csh_quote(str(directory / TCSH_WATCH_HOOKS_SCRIPT)),
        "@POSIX_UPDATE@": shlex.quote(str(directory / POSIX_UPDATE_SCRIPT)),
        "@BASH_SELF@": shlex.quote(str(directory / BASH_INTEGRATION_SCRIPT)),
        "@ZSH_SELF@": shlex.quote(str(directory / ZSH_INTEGRATION_SCRIPT)),
        "@POSIX_SELF@": shlex.quote(str(directory / POSIX_INTEGRATION_SCRIPT)),
        "@CSH_SELF@": csh_quote(str(directory / CSH_INTEGRATION_SCRIPT)),
        "@SOH@": bytes_to_shell_escape(SOH),
        "@EOT@": bytes_to_shell_escape(EOT),
        "@RS@": bytes_to_shell_escape(RS),
        "@US@": bytes_to_shell_escape(US),
        "@START@": bytes_to_shell_escape(scope(BEFORE_PROMPT)),
        "@END@": bytes_to_shell_escape(scope(AFTER_PROMPT)),
        "@CONT_START@": bytes_to_shell_escape(scope(BEFORE_CONTINUATION)),
        "@BUFFERED_CONT_START@": bytes_to_shell_escape(
            scope(BEFORE_BUFFERED_CONTINUATION)
        ),
        "@CONT_END@": bytes_to_shell_escape(scope(AFTER_CONTINUATION)),
        "@PROMPT_ID@": bytes_to_shell_escape(
            scope(PROMPT_ID_PREFIX) + b"%s" + OSC_TERMINATOR
        ),
        # csh checks only the OSC body so both raw and caret prompts match.
        "@CSH_START_PATTERN@": csh_quote(
            scope(BEFORE_PROMPT)[2 : -len(OSC_TERMINATOR)].decode("ascii")
        ),
    }

    def render(template):
        """Replace fixed placeholders with paths, frame separators, and session signals."""
        for key, value in tokens.items():
            template = template.replace(key, value)
        return template.lstrip("\n")

    update = r"""
_ish_update() {
    _ish_prompt_id=$((_ish_prompt_id + 1))
    {
        command printf '@SOH@@VERSION@@RS@exitcode@US@'
        command printf '%s' "$1" | command base64 -w0
        command printf '@RS@alias@US@'
        alias | command base64 -w0
        command printf '@RS@environ@US@'
        command env -0 | command base64 -w0
        command printf '@RS@prompt_id@US@'
        command printf '%s' "$_ish_prompt_id" | command base64 -w0
        command printf '@EOT@'
    } > "$_ish_pipe"
}
_ish_forward() { @FORWARD@ "$_tty_pipe"; }
_ish_status() { return "$1"; }
_ish_mark_prompt() { command printf '@PROMPT_ID@' "$_ish_prompt_id"; }
"""
    init = r"""
_ish_pipe=$1
_tty_pipe=$2
_ish_prompt_id=${_ish_prompt_id:-0}
"""
    bash = (
        init
        + update
        + r"""
ish_recover() { source @BASH_SELF@ "$_ish_pipe" "$_tty_pipe"; }
set +o notify
_ish_capture_status() {
    _ish_shell_exit_code=$?
    return "$_ish_shell_exit_code"
}
_ish_continuation_start() {
    # A zero timeout checks readiness without consuming or assigning input.
    # Probe here, before Bash reads the next line, rather than after its output
    # reaches Python and the shell may already have consumed the queued block.
    if builtin read -t 0; then
        builtin printf '@BUFFERED_CONT_START@'
    else
        builtin printf '@CONT_START@'
    fi
}
_ish_precmd() {
    if [[ "$PS1" != *$'@START@'* ]]; then
        PS1="$(command printf '@START@')${PS1}$(command printf '@END@')"
    fi
    if [[ ${PS2-} != "${_ish_wrapped_ps2-}" ]]; then
        _ish_original_ps2=${PS2-}
        # Users may prepend or append text to the already wrapped PS2. Remove
        # only our own markers before wrapping the edited prompt once again.
        _ish_original_ps2=${_ish_original_ps2//'$(_ish_continuation_start)'/}
        _ish_original_ps2=${_ish_original_ps2//$'@CONT_START@'/}
        _ish_original_ps2=${_ish_original_ps2//$'@CONT_END@'/}
    fi
    if builtin shopt -q promptvars; then
        PS2='$(_ish_continuation_start)'"${_ish_original_ps2-}"$'@CONT_END@'
    else
        # Keep literal prompts literal when the user disables prompt expansion.
        PS2=$'@CONT_START@'"${_ish_original_ps2-}"$'@CONT_END@'
    fi
    _ish_wrapped_ps2=$PS2
    _ish_update "$_ish_shell_exit_code"
    _ish_forward
    _ish_mark_prompt
    return "$_ish_shell_exit_code"
}
# Bash only executes PROMPT_COMMAND arrays starting with 5.1.
if (( BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1) )); then
    # A scalar becomes one array entry; arrays retain order and boundaries.
    if [[ "${PROMPT_COMMAND[0]:-}" != _ish_capture_status ]]; then
        PROMPT_COMMAND=(_ish_capture_status "${PROMPT_COMMAND[@]}" _ish_precmd)
    fi
else
    _ish_prompt_command() {
        local _ish_shell_exit_code=$?
        if [[ -n "$_ish_original_prompt_command" ]]; then
            _ish_status "$_ish_shell_exit_code"
            builtin eval -- "$_ish_original_prompt_command"
        fi
        _ish_precmd
    }
    if [[ "${PROMPT_COMMAND-}" != _ish_prompt_command ]]; then
        # Old Bash uses only element zero when the variable is an array.
        _ish_original_prompt_command=${PROMPT_COMMAND-}
        PROMPT_COMMAND=_ish_prompt_command
    fi
fi
"""
    )
    zsh = (
        init
        + update
        + r"""
ish_recover() { source @ZSH_SELF@ "$_ish_pipe" "$_tty_pipe"; }
unsetopt zle notify promptcr promptsp
_ish_precmd() {
    if [[ "$PS1" != *$'@START@'* ]]; then
        PS1="$(command printf '@START@')${PS1}$(command printf '@END@')"
    fi
    if [[ "$PS2" != *$'@CONT_START@'* ]]; then
        PS2="$(command printf '@CONT_START@')${PS2}$(command printf '@CONT_END@')"
    fi
    _ish_update "$1"
    _ish_forward
    _ish_mark_prompt
}
if [[ ${functions[precmd]-} != *'_ish_precmd '* ]]; then
    functions[_ish_original_precmd]=${functions[precmd]-:}
    _ish_original_precmd_functions=("${precmd_functions[@]}")
    precmd_functions=()
    precmd() {
        local _ish_saved_status=$?
        local _ish_hook
        # Include hooks registered after initialization; integration always runs.
        _ish_original_precmd_functions+=("${precmd_functions[@]}")
        precmd_functions=()
        for _ish_hook in _ish_original_precmd "${_ish_original_precmd_functions[@]}"; do
            # Quoting an unset hook array can produce one empty element.
            # Native zsh silently skips absent functions, never runs executables.
            [[ -n "$_ish_hook" ]] && (( ${+functions[$_ish_hook]} )) || continue
            _ish_status "$_ish_saved_status"
            "$_ish_hook" || break
        done
        _ish_precmd "$_ish_saved_status"
        return "$_ish_saved_status"
    }
fi
"""
    )
    posix = (
        "_ish_prompt_id=${_ish_prompt_id:-0}\n"
        + update
        + r"""
ish_recover() { . @POSIX_SELF@; }
_ish_posix_prompt() {
    case "$PS1" in *"$(command printf '@START@')"*) ;;
        *) PS1="$(command printf '@START@')${PS1}$(command printf '@END@')";;
    esac
    case "$PS2" in *"$(command printf '@CONT_START@')"*) ;;
        *) PS2="$(command printf '@CONT_START@')${PS2}$(command printf '@CONT_END@')";;
    esac
}
. @POSIX_UPDATE@
"""
    )
    posix_refresh = r"""
_ish_shell_exit_code=$?
_ish_posix_prompt
_ish_update "$_ish_shell_exit_code"
_ish_forward
_ish_mark_prompt
_ish_status "$_ish_shell_exit_code"
"""

    csh_init = r"""
if (! $?_ish_prompt_id) set _ish_prompt_id = 0
set _ish_before = "`printf '@START@'`"
set _ish_after = "`printf '@END@'`"
set _ish_cont_before = "`printf '@CONT_START@'`"
set _ish_cont_after = "`printf '@CONT_END@'`"
"""
    # BSD csh has no precmd. Its adapter explicitly sources this after a prompt.
    csh_refresh = r"""
source @CSH_HOOK@
"""
    csh_hook = r"""
if ("$prompt" !~ *@CSH_START_PATTERN@*) set prompt = "$_ish_before$prompt$_ish_after"
if ($?tcsh) then
    # ish edits the primary prompt. Preserve the user's editor setting for
    # command execution (including foreach's own secondary line editor).
    # Syntax/history errors and :p can skip postcmd. Do not overwrite the
    # saved setting with our temporary "unset edit" on the next prompt.
    if (! $_ish_editor_suspended) set _ish_edit_enabled = $?edit
    set _ish_editor_suspended = 1
    unset edit
endif
@ _ish_prompt_id ++
setenv PWD "$cwd"
printf '@SOH@@VERSION@@RS@exitcode@US@' > "$_ish_pipe"
printf '%s' "$_ish_shell_exit_code" | base64 -w0 >> "$_ish_pipe"
printf '@RS@alias@US@' >> "$_ish_pipe"
alias | base64 -w0 >> "$_ish_pipe"
printf '@RS@environ@US@' >> "$_ish_pipe"
env -0 | base64 -w0 >> "$_ish_pipe"
printf '@RS@prompt_id@US@' >> "$_ish_pipe"
printf '%s' "$_ish_prompt_id" | base64 -w0 >> "$_ish_pipe"
printf '@EOT@' >> "$_ish_pipe"
@CSH_FORWARD@ "$_tty_pipe"
printf '@PROMPT_ID@' "$_ish_prompt_id"
set status = $_ish_shell_exit_code
"""
    csh = (
        csh_init
        + r"""
set _ish_recover_path = @CSH_SELF@
alias ish_recover 'source "$_ish_recover_path"'
set _ish_shell_exit_code = 0
source @CSH_UPDATE@
"""
    )
    tcsh_bind = r"""
# postcmd is suspended by the caller; its current definition is snapshotted.
if ("`alias precmd`" != "_ish_precmd") alias _ish_original_precmd "`alias precmd`"
if ("`alias _ish_current_postcmd`" != "_ish_postcmd") alias _ish_original_postcmd "`alias _ish_current_postcmd`"
if ("`alias periodic`" != "_ish_periodic") then
    alias _ish_original_periodic "`alias periodic`"
    set _ish_periodic_last = "`date +%s`"
endif
if ($?tperiod) then
    if ("$tperiod" != "0") set _ish_user_tperiod = "$tperiod"
else
    set _ish_user_tperiod = 0
endif
alias precmd _ish_precmd
alias periodic _ish_periodic
set tperiod = 0
"""
    tcsh_watch = r"""
# periodic runs at a primary-prompt boundary, never while reading a loop body.
# Preserve the existing periodic callback's interval while checking hooks at
# every primary prompt. Only consult the clock when there is a user callback.
alias _ish_periodic_dispatch ''
if ("`alias _ish_original_periodic`" != "") then
    set _ish_periodic_now = "`date +%s`"
    @ _ish_periodic_elapsed = $_ish_periodic_now - $_ish_periodic_last
    @ _ish_periodic_interval = $_ish_user_tperiod * 60
    if ($_ish_periodic_elapsed >= $_ish_periodic_interval) then
        set _ish_periodic_last = "$_ish_periodic_now"
        alias _ish_periodic_dispatch 'alias postcmd "`alias _ish_current_postcmd`"; set status = $_ish_guard_status; _ish_original_periodic; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd'
    endif
endif
_ish_periodic_dispatch
source @CSH_BIND@
"""
    tcsh = (
        'set _ish_pipe = "$1"\nset _tty_pipe = "$2"\n'
        + csh_init
        + r"""
# Rebind installed wrappers without resetting saved editor/periodic state.
alias ish_recover 'alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_bind_path"; alias postcmd _ish_postcmd'
unset notify
set _ish_hook_path = @CSH_HOOK@
set _ish_bind_path = @CSH_BIND@
set _ish_watch_path = @CSH_WATCH@
if (! $?_ish_tcsh_installed) then
    set _ish_tcsh_installed = 1
    set _ish_editor_suspended = 0
    set _ish_edit_enabled = $?edit
    alias _ish_original_precmd "`alias precmd`"
    alias _ish_original_postcmd "`alias postcmd`"
    alias _ish_original_periodic "`alias periodic`"
    set _ish_user_tperiod = 0
    if ($?tperiod) then
        set _ish_user_tperiod = "$tperiod"
    endif
    set _ish_periodic_last = "`date +%s`"
    # A sourced file also invokes postcmd for each line. Suspend that hook
    # only around our bookkeeping so it neither restores edit too early nor
    # calls the user's postcmd for integration commands.
    alias _ish_precmd 'set _ish_shell_exit_code = $status; set status = $_ish_shell_exit_code; _ish_original_precmd; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_bind_path"; source "$_ish_hook_path"; alias postcmd _ish_postcmd'
    # postcmd runs after the user's input has been recorded in history and
    # before execution, even with edit unset. Keep setup out of the input line.
    alias _ish_postcmd 'set _ish_exec_status = $status; if ($_ish_editor_suspended && $_ish_edit_enabled) set edit; set _ish_editor_suspended = 0; set status = $_ish_exec_status; _ish_original_postcmd; set status = $_ish_exec_status'
    alias _ish_periodic 'set _ish_guard_status = $status; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_watch_path"; alias postcmd _ish_postcmd; set status = $_ish_guard_status'
    alias precmd _ish_precmd
    alias periodic _ish_periodic
    set tperiod = 0
    alias postcmd _ish_postcmd
endif
"""
    )
    return {
        name: render(body)
        for name, body in {
            BASH_INTEGRATION_SCRIPT: bash,
            ZSH_INTEGRATION_SCRIPT: zsh,
            CSH_INTEGRATION_SCRIPT: csh,
            TCSH_INTEGRATION_SCRIPT: tcsh,
            POSIX_INTEGRATION_SCRIPT: posix,
            POSIX_UPDATE_SCRIPT: posix_refresh,
            CSH_UPDATE_SCRIPT: csh_refresh,
            TCSH_PRECMD_SCRIPT: csh_hook,
            TCSH_BIND_HOOKS_SCRIPT: tcsh_bind,
            TCSH_WATCH_HOOKS_SCRIPT: tcsh_watch,
        }.items()
    }
