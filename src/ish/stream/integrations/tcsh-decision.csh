# ish-stream-api: 1
set _ish_stream_popup_allowed = 0; set _ish_stream_event = H; set _ish_stream_command = "\!#:q"; if ($_ish_stream_active && $_ish_stream_command:q == $_ish_stream_popup_body:q) set _ish_stream_popup_allowed = 1; if ($_ish_stream_popup_allowed) set _ish_stream_event = B;
