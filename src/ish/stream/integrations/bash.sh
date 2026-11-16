# ish-stream-api: 1
# Native Readline owns every command line. F2 is reserved in this Bash session;
# outside primary editing it is consumed, while program stdin remains native.
_ish_stream_min_major=5
_ish_stream_min_minor=1
_ish_stream_emacs_maps=(emacs-standard)
_ish_stream_vi_maps=(vi-insert vi-command)
_ish_stream_keys=('\eOQ' '\e[12~')
_ish_stream_direct=' self-insert accept-line operate-and-get-next backward-delete-char delete-char backward-char forward-char beginning-of-line end-of-line backward-word forward-word kill-line backward-kill-line kill-whole-line backward-kill-word kill-word unix-word-rubout unix-line-discard transpose-chars transpose-words upcase-word downcase-word capitalize-word previous-history next-history beginning-of-history end-of-history history-search-backward history-search-forward revert-line undo yank yank-pop vi-movement-mode vi-insertion-mode vi-insert-beg vi-append-mode vi-append-eol '
if [[ -r @RC@ ]]; then builtin source @RC@; fi
_ish_stream_status=$?
if (( BASH_VERSINFO[0] < _ish_stream_min_major ||
      (BASH_VERSINFO[0] == _ish_stream_min_major && BASH_VERSINFO[1] < _ish_stream_min_minor) )); then
    builtin printf '%s\n' @VERSION_NOTICE@ >&2
    builtin return "$_ish_stream_status"
fi
_ish_stream_nonce=@NONCE@
_ish_stream_python=@PYTHON@
_ish_stream_helper=@HELPER@
_ish_stream_socket=@SOCKET@
_ish_stream_serial=0
_ish_stream_primary=0
_ish_stream_ready=0
_ish_stream_bound=0
_ish_stream_pc_array=0
_ish_stream_pc_supported=1
_ish_stream_marker=
_ish_stream_notice=
_ish_stream_suffix='${_ish_stream_marker:0:(_ish_stream_primary=_ish_stream_ready)*${#_ish_stream_marker}}'
_ish_stream_hide_prefix='\e]777;ish;'$_ish_stream_nonce';$((_ish_stream_primary=0,_ish_stream_ready=0,++_ish_stream_serial));H\a'
_ish_stream_more_prefix='\['$_ish_stream_hide_prefix'\]'
_ish_stream_private_prefix='\e[ish-'$_ish_stream_nonce'-'
declare -a _ish_stream_maps=()
declare -a _ish_stream_native_maps=() _ish_stream_native_bindings=()
declare -a _ish_stream_function_maps=() _ish_stream_function_bindings=()
declare -a _ish_stream_private_maps=() _ish_stream_private_keys=()
declare -A _ish_stream_original=() _ish_stream_restored=()

_ish_stream_key() {
    local _ish_record=$1 _ish_char
    local -n _ish_key_result=$2 _ish_end_result=$3
    [[ $_ish_record == \"* ]] || return 1
    for ((_ish_end_result=1; _ish_end_result<${#_ish_record}; _ish_end_result++)); do
        _ish_char=${_ish_record:_ish_end_result:1}
        if [[ $_ish_char == \\ ]]; then
            _ish_end_result=$((_ish_end_result + 1))
        elif [[ $_ish_char == \" ]]; then
            _ish_key_result=${_ish_record:1:_ish_end_result-1}
            return 0
        fi
    done
    return 1
}

_ish_stream_decode() {
    # Bash 5.1/5.2 bind -X uses Readline's macro encoding, but bind -x does
    # not decode it on import. Decode only that byte format, never shell syntax.
    local LC_ALL=C _ish_encoded=$1 _ish_i=0 _ish_char _ish_code _ish_octet
    local _ish_control _ish_meta
    local -n _ish_decoded=$2
    _ish_decoded=
    while (( _ish_i < ${#_ish_encoded} )); do
        _ish_char=${_ish_encoded:_ish_i:1}
        _ish_i=$((_ish_i + 1))
        if [[ $_ish_char != \\ ]]; then
            _ish_decoded+=$_ish_char
            continue
        fi
        _ish_control=0 _ish_meta=0
        while [[ ${_ish_encoded:_ish_i:2} == 'C-' || ${_ish_encoded:_ish_i:2} == 'M-' ]]; do
            if [[ ${_ish_encoded:_ish_i:1} == C ]]; then _ish_control=1; else _ish_meta=128; fi
            _ish_i=$((_ish_i + 2))
        done
        _ish_char=${_ish_encoded:_ish_i:1}
        _ish_i=$((_ish_i + 1))
        if (( !_ish_control && !_ish_meta )) || [[ $_ish_char == \\ ]]; then
            if [[ $_ish_char == \\ ]] && (( _ish_control || _ish_meta )); then
                _ish_char=${_ish_encoded:_ish_i:1}
                _ish_i=$((_ish_i + 1))
            fi
            case $_ish_char in
                e) _ish_code=27 ;;
                \\) _ish_code=92 ;;
                \") _ish_code=34 ;;
                *) return 1 ;;
            esac
        else
            [[ $_ish_char ]] || return 1
            builtin printf -v _ish_code '%d' "'$_ish_char"
        fi
        if (( _ish_control )); then
            if (( _ish_code == 63 )); then _ish_code=127; else _ish_code=$((_ish_code & 31)); fi
        fi
        _ish_code=$((_ish_code | _ish_meta))
        (( _ish_code )) || return 1
        builtin printf -v _ish_octet '\\%03o' "$_ish_code"
        builtin printf -v _ish_char '%b' "$_ish_octet"
        _ish_decoded+=$_ish_char
    done
}

_ish_stream_event() {
    _ish_stream_serial=$((_ish_stream_serial + 1))
    builtin printf '\033]777;ish;%s;%s;%s\007' \
        "$_ish_stream_nonce" "$_ish_stream_serial" "$1" > /dev/tty
}

_ish_stream_restore() {
    local _ish_index
    (( _ish_stream_bound )) || return 0
    for ((_ish_index=0; _ish_index<${#_ish_stream_native_bindings[@]}; _ish_index++)); do
        builtin bind -m "${_ish_stream_native_maps[_ish_index]}" \
            -x "${_ish_stream_native_bindings[_ish_index]}"
    done
    for ((_ish_index=0; _ish_index<${#_ish_stream_function_bindings[@]}; _ish_index++)); do
        builtin bind -m "${_ish_stream_function_maps[_ish_index]}" \
            "${_ish_stream_function_bindings[_ish_index]}"
    done
    _ish_stream_bound=0
}

_ish_stream_bind() {
    local _ish_map _ish_key _ish_line _ish_index=0 _ish_offset _ish_start _ish_separator _ish_id
    local _ish_position _ish_slashes
    local _ish_function _ish_private _ish_hide=${_ish_stream_private_prefix}hide~
    local _ish_body _ish_decoded_body _ish_quoted _ish_quote="'" _ish_escaped="'\\\\''"
    local -a _ish_wrappers=()
    [[ $_ish_stream_socket ]] || return 0
    # Reclaim private macro keys at a prompt boundary, never while the native
    # operation following the hide macro can still be executing.
    for ((_ish_index=0; _ish_index<${#_ish_stream_private_keys[@]}; _ish_index++)); do
        builtin bind -m "${_ish_stream_private_maps[_ish_index]}" \
            -r "${_ish_stream_private_keys[_ish_index]}"
    done
    _ish_stream_private_maps=()
    _ish_stream_private_keys=()
    _ish_stream_native_maps=()
    _ish_stream_native_bindings=()
    _ish_stream_function_maps=()
    _ish_stream_function_bindings=()
    if [[ -o vi ]]; then
        _ish_stream_maps=("${_ish_stream_vi_maps[@]}")
    else
        _ish_stream_maps=("${_ish_stream_emacs_maps[@]}")
    fi
    for _ish_map in "${_ish_stream_maps[@]}"; do
        while IFS= builtin read -r _ish_line; do
            # Preserve Readline's serialization; only decode its key boundary.
            _ish_stream_key "$_ish_line" _ish_key _ish_offset || return 1
            # Bash 5.1/5.2 serialize a colon; 5.3 uses whitespace for the
            # decoding form. Keep that native choice when wrapping the body.
            case ${_ish_line:_ish_offset} in
                '": "'*) _ish_start=$((_ish_offset + 4)); _ish_separator=': ' ;;
                '" "'*) _ish_start=$((_ish_offset + 3)); _ish_separator=' ' ;;
                *) return 1 ;;
            esac
            # Readline 8.2 prints a prefix map's ANYOTHERKEY (256) as \000.
            # Actual NUL (0) is \C-@. Remove only an unescaped trailing shadow
            # token, retaining a user's literal backslash followed by 000.
            if [[ $_ish_separator == ': ' && $_ish_key == *'\000' ]]; then
                _ish_slashes=0
                for ((_ish_position=${#_ish_key}-5; _ish_position>=0; _ish_position--)); do
                    [[ ${_ish_key:_ish_position:1} == \\ ]] || break
                    _ish_slashes=$((_ish_slashes + 1))
                done
                if (( _ish_slashes % 2 == 0 )); then
                    _ish_key=${_ish_key:0:${#_ish_key}-4}
                fi
            fi
            [[ $_ish_key == '\eOQ' || $_ish_key == '\e[12~' ]] && continue
            [[ ${_ish_line: -1} == \" ]] || return 1
            _ish_body=${_ish_line:_ish_start:${#_ish_line}-_ish_start-1}
            _ish_stream_native_maps+=("$_ish_map")
            if [[ $_ish_separator == ': ' ]]; then
                _ish_stream_decode "$_ish_body" _ish_decoded_body || return 1
                _ish_id="$_ish_map|$_ish_key"
                if [[ $_ish_decoded_body == "${_ish_stream_restored[$_ish_id]-}" ]]; then
                    _ish_decoded_body=${_ish_stream_original[$_ish_id]}
                fi
                _ish_stream_original[$_ish_id]=$_ish_decoded_body
                builtin printf -v _ish_quoted '%q' "$_ish_decoded_body"
                _ish_stream_restored[$_ish_id]="builtin eval $_ish_quoted"
                _ish_stream_native_bindings+=("\"$_ish_key\":${_ish_stream_restored[$_ish_id]}")
                _ish_wrappers+=("\"$_ish_key\":_ish_stream_dispatch $_ish_quoted \"\$@\"")
            else
                _ish_stream_native_bindings+=("$_ish_line")
                _ish_quoted=${_ish_body//"$_ish_quote"/"$_ish_escaped"}
                _ish_wrappers+=("\"$_ish_key\" \"_ish_stream_dispatch '$_ish_quoted' \\\"\$@\\\"\"")
            fi
        done < <(builtin bind -m "$_ish_map" -X)
    done
    for ((_ish_index=0; _ish_index<${#_ish_wrappers[@]}; _ish_index++)); do
        builtin bind -m "${_ish_stream_native_maps[_ish_index]}" -x "${_ish_wrappers[_ish_index]}"
    done
    for _ish_map in "${_ish_stream_maps[@]}"; do
        builtin bind -m "$_ish_map" -x "\"$_ish_hide\":_ish_stream_hide"
        _ish_stream_private_maps+=("$_ish_map")
        _ish_stream_private_keys+=("$_ish_hide")
        while IFS= builtin read -r _ish_line; do
            [[ $_ish_line == \"* ]] || continue
            _ish_function=${_ish_line##*: }
            [[ $_ish_stream_direct == *" $_ish_function "* ]] && continue
            _ish_stream_key "$_ish_line" _ish_key _ish_offset || continue
            [[ $_ish_key == '\eOQ' || $_ish_key == '\e[12~' ]] && continue
            [[ ${_ish_line:_ish_offset:3} == '": ' ]] || continue
            _ish_private=${_ish_stream_private_prefix}${#_ish_stream_function_bindings[@]}~
            _ish_stream_function_maps+=("$_ish_map")
            _ish_stream_function_bindings+=("$_ish_line")
            builtin bind -m "$_ish_map" "\"$_ish_private\": $_ish_function"
            builtin bind -m "$_ish_map" "\"$_ish_key\": \"$_ish_hide$_ish_private\""
            _ish_stream_private_maps+=("$_ish_map")
            _ish_stream_private_keys+=("$_ish_private")
        done < <(builtin bind -m "$_ish_map" -p)
        for _ish_key in "${_ish_stream_keys[@]}"; do
            builtin bind -m "$_ish_map" -x "\"$_ish_key\":_ish_stream_popup"
        done
    done
    _ish_stream_bound=1
}

_ish_stream_hide() {
    local _ish_saved=$? _ish_was_primary=$_ish_stream_primary
    _ish_stream_primary=0
    _ish_stream_ready=0
    _ish_stream_restore
    (( _ish_was_primary )) && _ish_stream_event H
    return "$_ish_saved"
}

_ish_stream_return() { return "$1"; }

_ish_stream_dispatch() {
    local _ish_saved=$? _ish_was_primary=$_ish_stream_primary _ish_result _ish_body=$1
    shift
    _ish_stream_hide
    if _ish_stream_return "$_ish_saved"; then
        builtin eval "$_ish_body"
    else
        builtin eval "$_ish_body"
    fi
    _ish_result=$?
    if (( _ish_was_primary )); then
        _ish_stream_prepare
        if (( _ish_stream_ready )); then
            _ish_stream_primary=1
            builtin printf '%s' "$_ish_stream_notice" > /dev/tty
        fi
    fi
    return "$_ish_result"
}

_ish_stream_guard() {
    if (( _ish_stream_pc_array )); then
        [[ ${PROMPT_COMMAND[0]-} == _ish_stream_precmd &&
           ${PROMPT_COMMAND[-1]-} == _ish_stream_prepare ]]
    else
        [[ ${PROMPT_COMMAND-} == _ish_stream_precmd$'\n'*$'\n'_ish_stream_prepare ]]
    fi
}

_ish_stream_prepare() {
    local _ish_saved=$?
    _ish_stream_ready=0
    _ish_stream_restore
    if builtin shopt -q promptvars && [[ -o emacs || -o vi ]] &&
       (( _ish_stream_pc_supported )) && _ish_stream_guard &&
       [[ $PS1 == *"$_ish_stream_suffix" &&
          $PS2 == "$_ish_stream_more_prefix"* && $PS0 == "$_ish_stream_hide_prefix"* ]] &&
       _ish_stream_bind; then
        _ish_stream_serial=$((_ish_stream_serial + 1))
        builtin printf -v _ish_stream_notice '\033]777;ish;%s;%s;P\007' \
            "$_ish_stream_nonce" "$_ish_stream_serial"
        _ish_stream_marker=$'\001'$_ish_stream_notice$'\002'
        _ish_stream_ready=1
    fi
    return "$_ish_saved"
}

_ish_stream_popup() {
    local _ish_saved=$?
    if (( _ish_stream_primary )) && [[ -o emacs || -o vi ]] &&
       _ish_stream_guard && [[ $PS1 == *"$_ish_stream_suffix" &&
       $PS2 == "$_ish_stream_more_prefix"* && $PS0 == "$_ish_stream_hide_prefix"* ]]; then
        _ish_stream_event B
        "$_ish_stream_python" "$_ish_stream_helper" --socket "$_ish_stream_socket" \
            --nonce "$_ish_stream_nonce" --serial "$_ish_stream_serial"
        _ish_stream_event P
    else
        _ish_stream_hide
    fi
    return "$_ish_saved"
}

# Keep native accept-line unchanged: bind-x on Enter redraws and may erase
# preceding output without a newline. This hook hides before an empty prompt's
# user reader; PS0 and PS2 handle the other boundaries through expansion only.
_ish_stream_precmd() { _ish_stream_hide; }
_ish_stream_pc_decl=$(builtin declare -p PROMPT_COMMAND 2>/dev/null) || _ish_stream_pc_decl=
_ish_stream_pc_attributes=${_ish_stream_pc_decl#declare }
_ish_stream_pc_attributes=${_ish_stream_pc_attributes%% *}
case $_ish_stream_pc_attributes in
    *r*|*n*|*A*) _ish_stream_pc_supported=0 ;;
    *a*)
        _ish_stream_pc_array=1
        PROMPT_COMMAND=(_ish_stream_precmd "${PROMPT_COMMAND[@]}" _ish_stream_prepare)
        ;;
    *) PROMPT_COMMAND=$'_ish_stream_precmd\n'${PROMPT_COMMAND-}$'\n_ish_stream_prepare' ;;
esac

if builtin shopt -q promptvars; then
    PS1=${PS1-}$_ish_stream_suffix
    PS2=$_ish_stream_more_prefix${PS2-}
    PS0=$_ish_stream_hide_prefix${PS0-}
fi
builtin return "$_ish_stream_status"
