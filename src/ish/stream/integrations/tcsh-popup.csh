# ish-stream-api: 1
set _ish_stream_python = @PYTHON@
set _ish_stream_helper = @HELPER@
set _ish_stream_socket = @SOCKET@
set _ish_stream_popup_allowed = 0
alias _ish_stream_popup 'set _ish_stream_popup_status = $status ; if ( $_ish_stream_popup_allowed ) $_ish_stream_python:q $_ish_stream_helper:q --socket $_ish_stream_socket:q --nonce @NONCE@ --serial $_ish_stream_serial ; if ( $_ish_stream_popup_allowed ) @ _ish_stream_serial ++ ; if ( $_ish_stream_popup_allowed ) set _ish_stream_active = 1 ; if ( $_ish_stream_popup_allowed ) /usr/bin/printf "\033]777;ish;@NONCE@;%s;P\007" "$_ish_stream_serial" > ! /dev/tty ; set status = $_ish_stream_popup_status'
set _ish_stream_popup_body = "`alias _ish_stream_popup`"
