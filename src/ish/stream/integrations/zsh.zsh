# ish-stream-api: 1
typeset -ga _ish_stream_keymaps=(emacs viins vicmd)
typeset -ga _ish_stream_popup_keys=('\eOQ' '\e[12~')
# These widgets read additional keys or can invoke user completion code.
# Ordinary cursor movement and self-insert remain native.
typeset -ga _ish_stream_reading_widgets=(
    quoted-insert
    vi-quoted-insert
    execute-named-cmd
    execute-last-named-cmd
    read-command
    describe-key-briefly
    where-is
    what-cursor-position
    vi-find-next-char
    vi-find-prev-char
    vi-find-next-char-skip
    vi-find-prev-char-skip
    vi-replace-chars
    vi-set-buffer
    vi-set-mark
    vi-goto-mark
    vi-goto-mark-line
    vi-delete
    vi-change
    vi-yank
    vi-indent
    vi-unindent
    vi-repeat-find
    vi-rev-repeat-find
    history-incremental-search-backward
    history-incremental-search-forward
    history-incremental-pattern-search-backward
    history-incremental-pattern-search-forward
    vi-history-search-backward
    vi-history-search-forward
    expand-or-complete
    expand-or-complete-prefix
    complete-word
    menu-complete
    menu-expand-or-complete
    reverse-menu-complete
    list-choices
    delete-char-or-list
    accept-and-menu-complete
    menu-select
    list-expand
    expand-word
)

builtin zmodload zsh/zleparameter || return 1
typeset -g _ish_stream_nonce=@NONCE@
typeset -g _ish_stream_python=@PYTHON@
typeset -g _ish_stream_helper=@HELPER@
typeset -g _ish_stream_socket=@SOCKET@
typeset -gi _ish_stream_serial=0 _ish_stream_primary=0 _ish_stream_bound=0
typeset -gi _ish_stream_depth=0 _ish_stream_widget_id=0
typeset -gA _ish_stream_bindings=() _ish_stream_owned=()

_ish_stream_event() {
    local _ish_saved=$?
    (( ++_ish_stream_serial ))
    builtin printf '\033]777;ish;%s;%s;%s\007' \
        "$_ish_stream_nonce" "$_ish_stream_serial" "$1" > /dev/tty
    return "$_ish_saved"
}

_ish_stream_hooks() {
    [[ ${widgets[zle-line-init]-} == user:_ish_stream_start &&
       ${widgets[zle-line-finish]-} == user:_ish_stream_finish ]]
}

_ish_stream_call() {
    local _ish_status=$1 _ish_widget=$2
    shift 2
    # Only status restoration is conditional. The original widget executes
    # normally, retaining ERR_RETURN and every other user option.
    if _ish_stream_return "$_ish_status"; then
        builtin zle "$_ish_widget" -f nolast -- "$@"
    else
        builtin zle "$_ish_widget" -f nolast -- "$@"
    fi
}

_ish_stream_restore() {
    local _ish_map _ish_key _ish_id _ish_current
    (( _ish_stream_bound )) || return 0
    for _ish_map in "${_ish_stream_keymaps[@]}"; do
        for _ish_key in "${_ish_stream_popup_keys[@]}"; do
            _ish_id="${_ish_map}|${_ish_key}"
            _ish_current=$(builtin bindkey -M "$_ish_map" -L "$_ish_key")
            # A widget may deliberately replace its own binding while running.
            # Restore only entries which are still the ones installed by ish.
            if [[ $_ish_current == "${_ish_stream_owned[$_ish_id]-}" ]]; then
                builtin eval -- "builtin ${_ish_stream_bindings[$_ish_id]}"
            fi
        done
    done
    _ish_stream_bound=0
}

_ish_stream_bind() {
    local _ish_map _ish_key _ish_id
    [[ -n $_ish_stream_socket ]] || return 0
    (( _ish_stream_bound )) && return 0
    for _ish_map in "${_ish_stream_keymaps[@]}"; do
        for _ish_key in "${_ish_stream_popup_keys[@]}"; do
            _ish_id="${_ish_map}|${_ish_key}"
            # Use zsh's own serialized binding, including native string macros.
            _ish_stream_bindings[$_ish_id]=$(builtin bindkey -M "$_ish_map" -L "$_ish_key")
            builtin bindkey -M "$_ish_map" "$_ish_key" _ish_stream_popup
            _ish_stream_owned[$_ish_id]=$(builtin bindkey -M "$_ish_map" -L "$_ish_key")
        done
    done
    _ish_stream_bound=1
}

_ish_stream_wrap_one() {
    local _ish_name=$1 _ish_saved _ish_wrapper
    [[ ${widgets[$_ish_name]-} == user:_ish_stream_* ]] && return 0
    (( ++_ish_stream_widget_id ))
    _ish_saved="_ish_stream_saved_$_ish_stream_widget_id"
    _ish_wrapper="_ish_stream_wrapper_$_ish_stream_widget_id"
    builtin zle -A "$_ish_name" "$_ish_saved"
    # Capture the target per widget: nested zle calls preserve the outer WIDGET.
    functions[$_ish_wrapper]='_ish_stream_dispatch '"$_ish_saved"' "$@"'
    builtin zle -N "$_ish_name" "$_ish_wrapper"
}

_ish_stream_wrap() {
    local _ish_name
    for _ish_name in "${(@k)widgets}"; do
        case $_ish_name in _ish_stream_*|zle-*|.*) continue ;; esac
        case ${widgets[$_ish_name]} in
            user:_ish_stream_*) ;;
            user:*|completion:*) _ish_stream_wrap_one "$_ish_name" ;;
        esac
    done
    for _ish_name in "${_ish_stream_reading_widgets[@]}"; do
        if (( ${+widgets[$_ish_name]} )); then
            _ish_stream_wrap_one "$_ish_name"
        fi
    done
}

_ish_stream_dispatch() {
    local _ish_old=$? _ish_target=$1 _ish_result=0
    shift
    if [[ $CONTEXT != start ]] || (( !_ish_stream_primary )); then
        _ish_stream_call "$_ish_old" "$_ish_target" "$@"
        return $?
    fi
    (( ++_ish_stream_depth ))
    if (( _ish_stream_depth == 1 )); then
        _ish_stream_event H
        _ish_stream_restore
    fi
    {
        _ish_stream_call "$_ish_old" "$_ish_target" "$@"
        _ish_result=$?
    } always {
        (( --_ish_stream_depth, 1 ))
        if (( !_ish_stream_depth && !TRY_BLOCK_INTERRUPT && !TRY_BLOCK_ERROR )) &&
           [[ $CONTEXT == start ]] && _ish_stream_hooks; then
            _ish_stream_wrap
            _ish_stream_bind
            builtin zle -R
            _ish_stream_event P
        fi
    }
    return "$_ish_result"
}

_ish_stream_start() {
    local _ish_old=$? _ish_result=0
    if (( _ish_stream_primary )); then _ish_stream_event H; fi
    _ish_stream_primary=0
    _ish_stream_restore
    (( ++_ish_stream_depth ))
    {
        if (( ${+widgets[_ish_stream_user_start]} )); then
            _ish_stream_call "$_ish_old" _ish_stream_user_start "$@"
            _ish_result=$?
        fi
    } always {
        (( --_ish_stream_depth, 1 ))
    }
    if [[ $CONTEXT == start ]] && _ish_stream_hooks; then
        _ish_stream_wrap
        _ish_stream_bind
        _ish_stream_primary=1
        _ish_stream_event P
    else
        _ish_stream_event H
    fi
    return "$_ish_result"
}

_ish_stream_finish() {
    local _ish_old=$? _ish_result=0
    _ish_stream_primary=0
    _ish_stream_event H
    _ish_stream_restore
    if (( ${+widgets[_ish_stream_user_finish]} )); then
        _ish_stream_call "$_ish_old" _ish_stream_user_finish "$@"
        _ish_result=$?
    fi
    return "$_ish_result"
}

_ish_stream_popup() {
    local _ish_old=$?
    if [[ $CONTEXT != start ]] || (( !_ish_stream_primary || _ish_stream_depth )); then
        builtin zle .undefined-key
        return "$_ish_old"
    fi
    _ish_stream_event B
    "$_ish_stream_python" "$_ish_stream_helper" --socket "$_ish_stream_socket" \
        --nonce "$_ish_stream_nonce" --serial "$_ish_stream_serial"
    _ish_stream_event P
    return "$_ish_old"
}

if (( ${+widgets[zle-line-init]} )); then
    builtin zle -A zle-line-init _ish_stream_user_start
fi
if (( ${+widgets[zle-line-finish]} )); then
    builtin zle -A zle-line-finish _ish_stream_user_finish
fi
builtin zle -N zle-line-init _ish_stream_start
builtin zle -N zle-line-finish _ish_stream_finish
builtin zle -N _ish_stream_popup
