"""Build shell templates separately from session tokens and final rendering."""

import shlex
import shutil

from ..constants import (
    AFTER_CONTINUATION,
    AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT,
    BEFORE_BUFFERED_CONTINUATION,
    BEFORE_CONTINUATION,
    BEFORE_PROMPT,
    BUFFERED_CONTINUATION_HINT,
    CSH_INTEGRATION_SCRIPT,
    CSH_UPDATE_SCRIPT,
    FORWARD_BINARY,
    LINE_INTERRUPT_ACK_PREFIX,
    LINE_READER_READY_PREFIX,
    NATIVE_CONTINUATION,
    OSC_TERMINATOR,
    POSIX_INTEGRATION_SCRIPT,
    POSIX_UPDATE_SCRIPT,
    PROMPT_ID_PREFIX,
    TCSH_BIND_HOOKS_SCRIPT,
    TCSH_INTEGRATION_SCRIPT,
    TCSH_PRECMD_SCRIPT,
    TCSH_WATCH_HOOKS_SCRIPT,
    ZSH_INTEGRATION_SCRIPT,
    ZSH_TRAP_SNAPSHOT,
    SessionSignals,
    bytes_to_shell_escape,
)
from ..protocol import EOT, RS, SOH, US, VERSION
from .base import ADAPTERS, csh_quote

_POSIX_INIT = r"""
_ish_pipe=$1
_tty_pipe=$2
_ish_prompt_id=${_ish_prompt_id:-0}
@PRELOAD_CHECK@
"""


_CSH_INIT = r"""
if (! $?_ish_prompt_id) set _ish_prompt_id = 0
set _ish_before = "`@CSH_PRINTF@ '@START@'`"
set _ish_after = "`@CSH_PRINTF@ '@END@'`"
set _ish_cont_before = "`@CSH_PRINTF@ '@CONT_START@'`"
set _ish_cont_after = "`@CSH_PRINTF@ '@CONT_END@'`"
@PRELOAD_CHECK@
"""


def _make_tokens(directory, signals, forward_path, input_ack_path=None):
    """Quote paths and encode protocol markers for one integration installation."""
    scope = signals.scope
    forward_path = forward_path or directory / FORWARD_BINARY
    # Resolve the few remaining external utilities once, before user commands
    # can change PATH. The context helper handles encoding and environments.
    printf_path = shutil.which("printf", path="/usr/bin:/bin") or "/usr/bin/printf"
    return {
        "@INPUT_ACK@": shlex.quote(str(input_ack_path or "")),
        "@CSH_INPUT_ACK@": csh_quote(str(input_ack_path or "")),
        "@VERSION@": VERSION.decode("ascii"),
        "@FORWARD@": shlex.quote(str(forward_path)),
        "@CSH_FORWARD@": csh_quote(str(forward_path)),
        "@CSH_PRINTF@": csh_quote(printf_path),
        "@CSH_UPDATE@": csh_quote(str(directory / CSH_UPDATE_SCRIPT)),
        "@CSH_HOOK@": csh_quote(str(directory / TCSH_PRECMD_SCRIPT)),
        "@CSH_BIND@": csh_quote(str(directory / TCSH_BIND_HOOKS_SCRIPT)),
        "@CSH_WATCH@": csh_quote(str(directory / TCSH_WATCH_HOOKS_SCRIPT)),
        "@POSIX_UPDATE@": shlex.quote(str(directory / POSIX_UPDATE_SCRIPT)),
        "@BASH_SELF@": shlex.quote(str(directory / BASH_INTEGRATION_SCRIPT)),
        "@ZSH_SELF@": shlex.quote(str(directory / ZSH_INTEGRATION_SCRIPT)),
        "@ZSH_TRAPS@": shlex.quote(str(directory / ZSH_TRAP_SNAPSHOT)),
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
        "@BUFFERED_CONT_HINT@": bytes_to_shell_escape(
            scope(BUFFERED_CONTINUATION_HINT)
        ),
        "@PROMPT_ID@": bytes_to_shell_escape(
            scope(PROMPT_ID_PREFIX) + b"%s" + OSC_TERMINATOR
        ),
        "@LINE_READY@": bytes_to_shell_escape(
            scope(LINE_READER_READY_PREFIX) + b"%s;%s" + OSC_TERMINATOR
        ),
        "@LINE_INTERRUPT@": bytes_to_shell_escape(
            scope(LINE_INTERRUPT_ACK_PREFIX) + b"%s;%s" + OSC_TERMINATOR
        ),
        # csh checks only the OSC body so both raw and caret prompts match.
        "@CSH_NATIVE_CONTINUATION@": bytes_to_shell_escape(scope(NATIVE_CONTINUATION)),
        "@CSH_START_PATTERN@": csh_quote(
            scope(BEFORE_PROMPT)[2 : -len(OSC_TERMINATOR)].decode("ascii")
        ),
    }


def _preload_checks(adapter):
    """Collect native startup checks, honoring an explicitly selected adapter."""
    checks = {entry.script: entry.preload_check() for entry in ADAPTERS.values()}
    if adapter is not None:
        checks[adapter.script] = adapter.preload_check()
    return checks


def _render_script(template, tokens, preload_check):
    """Render native checks before ordered tokens, preserving literal shell text."""
    template = template.replace("@PRELOAD_CHECK@", preload_check)
    for key, value in tokens.items():
        template = template.replace(key, value)
    return template.lstrip("\n")


def _posix_update(*, alias_command="alias", printf_command="command printf"):
    """Build shared state functions with explicit shell command spellings."""
    template = r"""
_ish_update() {
    _ish_prompt_id=$((_ish_prompt_id + 1))
    @UPDATE_ALIAS@ | @FORWARD@ --context "$_ish_pipe" "$1" "$_ish_prompt_id"
}
_ish_forward() { @FORWARD@ "$_tty_pipe" @INPUT_ACK@ "$_ish_prompt_id" "$_ish_pipe"; }
_ish_status() { return "$1"; }
_ish_mark_prompt() { @UPDATE_PRINTF@ '@PROMPT_ID@' "$_ish_prompt_id"; }
"""
    return template.replace("@UPDATE_ALIAS@", alias_command).replace(
        "@UPDATE_PRINTF@", printf_command
    )


def _bash_template():
    """Assemble Bash hooks without changing shell execution boundaries."""
    return (
        _POSIX_INIT
        + _posix_update()
        + r"""
ish_recover() {
    local _ish_recover_status=$?
    source @BASH_SELF@ "$_ish_pipe" "$_tty_pipe"
    return "$_ish_recover_status"
}
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
    # Rebuild from the default at every primary prompt. User PS2 substitutions
    # must not consume command input while the continuation prompt is expanded.
    if builtin shopt -q promptvars; then
        # Bash 5.3 runs this check in the current shell without a subshell per
        # continuation line.
        PS2='${ _ish_continuation_start; }@BASH_DEFAULT_CONTINUATION@'$'@CONT_END@'
    else
        PS2=$'@CONT_START@@BASH_DEFAULT_CONTINUATION@@CONT_END@'
    fi
    _ish_update "$_ish_shell_exit_code"
    _ish_forward
    _ish_mark_prompt
    return "$_ish_shell_exit_code"
}
# Supported Bash versions execute PROMPT_COMMAND arrays. A scalar becomes one
# array entry; arrays retain their original order and command boundaries.
if [[ "${PROMPT_COMMAND[0]:-}" != _ish_capture_status ]]; then
    PROMPT_COMMAND=(_ish_capture_status "${PROMPT_COMMAND[@]}" _ish_precmd)
fi
"""
    )


def _zsh_template():
    """Assemble Zsh hooks with builtin-only shared state functions."""
    return (
        _POSIX_INIT
        + _posix_update(alias_command="builtin alias", printf_command="builtin printf")
        + r"""
ish_recover() { source @ZSH_SELF@ "$_ish_pipe" "$_tty_pipe"; }
unsetopt zle notify promptcr promptsp
# Startup and explicit recovery restore prompt expansion. The next prompt also
# reloads zsh/zselect when available; ordinary prompt updates allow opt-outs.
builtin setopt promptsubst
_ish_continuation_ready() {
    emulate -L zsh
    local -a _ish_available
    local -i _ish_ready=0
    # zsh's read -t consumes input when ready; zselect only observes readiness.
    # This is a display hint, not an input-ownership or TTY-mode guarantee.
    if builtin zselect -t 0 -a _ish_available -r 0 2>/dev/null; then
        _ish_ready=1
    fi
    # A math function returns its last arithmetic value with a successful status.
    (( _ish_ready )); builtin true
}
functions -M ish_continuation_ready 0 0 _ish_continuation_ready
typeset -A _ish_continuation_suffixes=(
    0 $'@CONT_END@'
    1 $'@BUFFERED_CONT_HINT@@CONT_END@'
)
_ish_wrap_continuation() {
    # Keep zsh's parser-context prompt, excluding user command substitutions.
    # Rebuild it at every primary prompt, including after explicit recovery.
    if [[ -o promptsubst ]] && { builtin zmodload -e zsh/zselect || builtin zmodload zsh/zselect 2>/dev/null; }; then
        # Keep the probe in this shell: no subprocess or timer per continuation.
        PS2=$'@CONT_START@@ZSH_DEFAULT_CONTINUATION@''${_ish_continuation_suffixes[$((ish_continuation_ready()))]}'
    else
        PS2=$'@CONT_START@@ZSH_DEFAULT_CONTINUATION@@CONT_END@'
    fi
}
# Install only over the default SIGINT behavior, never over a user's trap.
# Capture in this shell: command substitution resets string traps. This private
# session file is overwritten only when sourcing and removed by session cleanup.
# Later readiness checks only compare the function body, without file I/O.
if (( ! ${+functions[TRAPINT]} )) && builtin trap >| @ZSH_TRAPS@; then
    _ish_int_traps=$(<@ZSH_TRAPS@)
    if [[ "$_ish_int_traps"$'\n' != *' INT'$'\n'* ]]; then
        TRAPINT() {
            if (( ZSH_SUBSHELL == 0 )); then
                _ish_interrupt_pending=1
                local _ish_reading=0
                if (( ${_ish_reading_command:-0} )) && [[ ${functions[preexec]-} == "${_ish_line_preexec_body-}" ]]; then
                    _ish_reading=1
                fi
                builtin printf '@LINE_INTERRUPT@' "$_ish_prompt_id" "$_ish_reading"
            fi
            return 130
        }
        _ish_line_interrupt_body=${functions[TRAPINT]}
    fi
    unset _ish_int_traps
fi
_ish_precmd() {
    _ish_interrupt_pending=0
    # Rewrap user edits at a confirmed prompt, preserving the latest hook.
    # Resolve autoloads under their original name before copying their body.
    if [[ -z ${_ish_line_preexec_body-} || ${functions[preexec]-} != "$_ish_line_preexec_body" ]]; then
        if [[ ${functions[preexec]-} != *'builtin autoload -X'* ]] || builtin autoload +X preexec; then
            functions[_ish_original_preexec]=${functions[preexec]-:}
            preexec() {
                local _ish_saved_status=$?
                # Revoke reader ownership before any user hook or command runs.
                _ish_reading_command=0
                builtin printf '@LINE_READY@' "$_ish_prompt_id" 0
                _ish_status "$_ish_saved_status"
                _ish_original_preexec "$@"
            }
            _ish_line_preexec_body=${functions[preexec]}
        fi
    fi
    if [[ "$PS1" != *$'@START@'* ]]; then
        PS1=$'@START@'"${PS1}"$'@END@'
    fi
    _ish_wrap_continuation
    _ish_update "$1"
    _ish_forward
    local _ish_line_ready=0
    if [[ -n ${_ish_line_interrupt_body-} && ${functions[TRAPINT]-} == "$_ish_line_interrupt_body" && -n ${_ish_line_preexec_body-} && ${functions[preexec]-} == "$_ish_line_preexec_body" ]]; then
        _ish_line_ready=1
    fi
    _ish_reading_command=1
    builtin printf '@LINE_READY@' "$_ish_prompt_id" "$_ish_line_ready"
    _ish_mark_prompt
}
# A nonzero TRAPINT return leaves zsh's return flag set when its non-ZLE
# line reader aborts. The next precmd function can be skipped while clearing
# that flag. Its hook array still runs; retry our wrapper exactly once there.
_ish_after_interrupt() {
    local _ish_saved_status=$?
    if (( ${_ish_interrupt_pending:-0} )); then
        _ish_status "$_ish_saved_status"
        precmd
    fi
    return "$_ish_saved_status"
}
if [[ ${functions[precmd]-} != *'_ish_precmd '* ]]; then
    functions[_ish_original_precmd]=${functions[precmd]-:}
    _ish_original_precmd_functions=("${precmd_functions[@]:#_ish_after_interrupt}")
    precmd_functions=(_ish_after_interrupt)
    precmd() {
        local _ish_saved_status=$?
        local _ish_hook
        # Include hooks registered after initialization; integration always runs.
        _ish_original_precmd_functions+=("${precmd_functions[@]:#_ish_after_interrupt}")
        precmd_functions=(_ish_after_interrupt)
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


def _posix_templates():
    """Build POSIX integration and its prompt-refresh companion."""
    posix = (
        "_ish_prompt_id=${_ish_prompt_id:-0}\n@PRELOAD_CHECK@\n"
        + _posix_update()
        + r"""
ish_recover() {
    _ish_recover_status=$?
    . @POSIX_SELF@
    return "$_ish_recover_status"
}
_ish_posix_before() {
    _ish_shell_exit_code=$?
    # PS1 expansion runs in a subshell, so a parent-shell counter cannot
    # advance here. A monotonic generation stays fresh across expansions.
    _ish_prompt_id=$(@FORWARD@ --clock)
    _ish_update "$_ish_shell_exit_code"
    _ish_forward
    _ish_mark_prompt
}
_ish_posix_prompt() {
    case "$PS1" in *'$(_ish_posix_before)'*) ;;
        *) PS1='$(_ish_posix_before)'"$(command printf '@START@')${PS1}$(command printf '@END@')";;
    esac
    case "$PS2" in *"$(command printf '@CONT_START@')"*) ;;
        *) PS2="$(command printf '@CONT_START@')${PS2}$(command printf '@CONT_END@')";;
    esac
}
. @POSIX_UPDATE@
"""
    )
    posix_refresh = r"""
_ish_posix_prompt
"""
    return {
        POSIX_INTEGRATION_SCRIPT: posix,
        POSIX_UPDATE_SCRIPT: posix_refresh,
    }


def _csh_templates():
    """Keep C shell aliases and sourced hook boundaries intact."""
    # BSD csh has no precmd. Only explicit ish_recover sources this update.
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
    # Rebuild the default prompt with its leading input-ownership marker.
    # Custom scalar/array prompts must not bypass continuation notification.
    set prompt2 = "%{$_ish_native_continuation%}@TCSH_DEFAULT_CONTINUATION@"
endif
@ _ish_prompt_id ++
setenv PWD "$cwd"
alias | @CSH_FORWARD@ --context "$_ish_pipe" "$_ish_shell_exit_code" "$_ish_prompt_id"
@CSH_FORWARD@ "$_tty_pipe" @CSH_INPUT_ACK@ "$_ish_prompt_id" "$_ish_pipe"
@CSH_PRINTF@ '@PROMPT_ID@' "$_ish_prompt_id"
set status = $_ish_shell_exit_code
"""
    csh = (
        _CSH_INIT
        + r"""
set _ish_recover_path = @CSH_SELF@
alias ish_recover 'set _ish_shell_exit_code = $status; source "$_ish_recover_path"'
if (! $?_ish_shell_exit_code) set _ish_shell_exit_code = 0
source @CSH_UPDATE@
"""
    )
    tcsh_bind = r"""
# postcmd is suspended by the caller; its current definition is snapshotted.
if ("`alias precmd`" != "_ish_precmd") alias _ish_original_precmd "`alias precmd`"
if ("`alias _ish_current_postcmd`" != "_ish_postcmd") alias _ish_original_postcmd "`alias _ish_current_postcmd`"
if ("`alias periodic`" != "_ish_periodic") then
    alias _ish_original_periodic "`alias periodic`"
    set _ish_periodic_last = "`@CSH_FORWARD@ --time`"
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
    set _ish_periodic_now = "`@CSH_FORWARD@ --time`"
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
        + _CSH_INIT
        + r"""
# Rebind installed wrappers without resetting saved editor/periodic state.
alias ish_recover 'if (1) glob; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_bind_path"; alias postcmd _ish_postcmd'
unset notify
set _ish_hook_path = @CSH_HOOK@
set _ish_bind_path = @CSH_BIND@
set _ish_watch_path = @CSH_WATCH@
if (! $?_ish_tcsh_installed) then
    set _ish_tcsh_installed = 1
    # Keep Enter bytes recognizable when unread input returns through the FIFO.
    setty -d -inlcr +icrnl
    # A trailing invisible marker can wait for the editor's next redraw. A
    # leading notification leaves all visible PS2 rendering with tcsh.
    set _ish_native_continuation = "`@CSH_PRINTF@ '@CSH_NATIVE_CONTINUATION@'`"
    set _ish_editor_suspended = 0
    set _ish_edit_enabled = $?edit
    alias _ish_original_precmd "`alias precmd`"
    alias _ish_original_postcmd "`alias postcmd`"
    alias _ish_original_periodic "`alias periodic`"
    set _ish_user_tperiod = 0
    if ($?tperiod) then
        set _ish_user_tperiod = "$tperiod"
    endif
    set _ish_periodic_last = "`@CSH_FORWARD@ --time`"
    # A sourced file also invokes postcmd for each line. Suspend that hook
    # only around our bookkeeping so it neither restores edit too early nor
    # calls the user's postcmd for integration commands.
    # Ctrl+C in the secondary editor can leave redraw bytes in tcsh's output
    # buffer. Backticks inherit that buffer and can capture it as alias text.
    # The argument-free glob builtin flushes it to the terminal without adding
    # output or forking. A one-line if bypasses aliases but still allows builtin
    # lookup. Save status first so flushing does not change user hook status.
    alias _ish_precmd 'set _ish_shell_exit_code = $status; if (1) glob; set status = $_ish_shell_exit_code; _ish_original_precmd; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_bind_path"; source "$_ish_hook_path"; alias postcmd _ish_postcmd'
    # postcmd runs after the user's input has been recorded in history and
    # before execution, even with edit unset. Keep setup out of the input line.
    alias _ish_postcmd 'set _ish_exec_status = $status; if (1) glob; if ($_ish_editor_suspended && $_ish_edit_enabled) set edit; set _ish_editor_suspended = 0; set status = $_ish_exec_status; _ish_original_postcmd; set status = $_ish_exec_status'
    alias _ish_periodic 'set _ish_guard_status = $status; if (1) glob; alias _ish_current_postcmd "`alias postcmd`"; unalias postcmd; source "$_ish_watch_path"; alias postcmd _ish_postcmd; set status = $_ish_guard_status'
    alias precmd _ish_precmd
    alias periodic _ish_periodic
    set tperiod = 0
    alias postcmd _ish_postcmd
endif
"""
    )
    return {
        CSH_INTEGRATION_SCRIPT: csh,
        TCSH_INTEGRATION_SCRIPT: tcsh,
        CSH_UPDATE_SCRIPT: csh_refresh,
        TCSH_PRECMD_SCRIPT: csh_hook,
        TCSH_BIND_HOOKS_SCRIPT: tcsh_bind,
        TCSH_WATCH_HOOKS_SCRIPT: tcsh_watch,
    }


# Register each integration entry point with the builder for its complete bundle.
# Shared builders run once per installation, even when several adapters use them.
SCRIPT_BUILDERS = {
    BASH_INTEGRATION_SCRIPT: lambda: {BASH_INTEGRATION_SCRIPT: _bash_template()},
    ZSH_INTEGRATION_SCRIPT: lambda: {ZSH_INTEGRATION_SCRIPT: _zsh_template()},
    CSH_INTEGRATION_SCRIPT: _csh_templates,
    TCSH_INTEGRATION_SCRIPT: _csh_templates,
    POSIX_INTEGRATION_SCRIPT: _posix_templates,
}


def make_scripts(
    directory,
    *,
    signals=SessionSignals(),
    forward_path=None,
    adapter=None,
    input_ack_path=None,
):
    """Return the existing script names and sources for one integration session.

    Builders only assemble shell text; tokens and native startup checks are
    resolved afresh for each call. Adapters select registered script bundles;
    adding a shell does not require another entry in this assembly function.
    """
    tokens = {}
    checks = _preload_checks(adapter)
    entries = {entry.script: entry for entry in ADAPTERS.values()}
    if adapter is not None:
        entries[adapter.script] = adapter
    templates = {}
    built = set()
    for entry in entries.values():
        tokens.update(entry.template_tokens)
        try:
            builder = SCRIPT_BUILDERS[entry.script]
        except KeyError:
            raise ValueError(
                f"No integration builder registered for {entry.script}"
            ) from None
        if builder not in built:
            for name, body in builder().items():
                if name in templates and templates[name] != body:
                    raise ValueError(f"Conflicting integration script: {name}")
                templates[name] = body
            built.add(builder)
        if entry.script not in templates:
            raise ValueError(f"Integration builder did not provide {entry.script}")
    # Resolve template policy before inserting paths, which may contain token text.
    tokens.update(_make_tokens(directory, signals, forward_path, input_ack_path))
    return {
        name: _render_script(body, tokens, checks.get(name, ""))
        for name, body in templates.items()
    }
