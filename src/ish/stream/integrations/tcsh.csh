# ish-stream-api: 1
setenv HOME @HOME@
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
