from __future__ import annotations
from typing import Final

BUILTIN: Final = "builtin"
ENVIRON: Final = "environ"
ALIAS: Final = "alias"
EXITCODE: Final = "exitcode"
PROMPT_ID: Final = "prompt_id"
PWD: Final = "PWD"

# The shell templates and the Python receiver share these exact wire signals.
ISH_OSC_PREFIX: Final = b'\x1b]633;'
OSC_TERMINATOR: Final = b'\a'
BEFORE_PROMPT: Final = ISH_OSC_PREFIX + b'S' + OSC_TERMINATOR
AFTER_PROMPT: Final = ISH_OSC_PREFIX + b'E' + OSC_TERMINATOR
BEFORE_CONTINUATION: Final = ISH_OSC_PREFIX + b's' + OSC_TERMINATOR
AFTER_CONTINUATION: Final = ISH_OSC_PREFIX + b'e' + OSC_TERMINATOR
COMMAND_START: Final = ISH_OSC_PREFIX + b'C' + OSC_TERMINATOR
COMMAND_DONE: Final = ISH_OSC_PREFIX + b'D' + OSC_TERMINATOR
PROMPT_ID_PREFIX: Final = ISH_OSC_PREFIX + b'P;'

# tcsh's native editor can render control bytes in caret notation.
CARET_BEFORE_PROMPT: Final = BEFORE_PROMPT.replace(b'\x1b', b'^[').replace(b'\a', b'^G')
CARET_AFTER_PROMPT: Final = AFTER_PROMPT.replace(b'\x1b', b'^[').replace(b'\a', b'^G')

# Keep the existing on-disk names for compatibility.
BASH_INTEGRATION_SCRIPT: Final = 'shellIntegraion.sh'
ZSH_INTEGRATION_SCRIPT: Final = 'shellIntegraion.zsh'
CSH_INTEGRATION_SCRIPT: Final = 'shellIntegraion.csh'
TCSH_INTEGRATION_SCRIPT: Final = 'shellIntegraion.tcsh'
POSIX_INTEGRATION_SCRIPT: Final = 'shellIntegraion.posix'
POSIX_UPDATE_SCRIPT: Final = 'ish_update.posix'
CSH_UPDATE_SCRIPT: Final = 'ish_update.csh'
TCSH_PRECMD_SCRIPT: Final = 'ish_precmd.tcsh'
TCSH_BIND_HOOKS_SCRIPT: Final = 'ish_bind_hooks.tcsh'
TCSH_WATCH_HOOKS_SCRIPT: Final = 'ish_watch_hooks.tcsh'
FORWARD_BINARY: Final = 'ish_forward'
FORWARD_SOURCE: Final = 'ish_forward.c'
SHELL_FIFO: Final = 'shell.fifo'
TTY_FIFO: Final = 'tty.fifo'


def bytes_to_shell_escape(value: bytes) -> str:
    """Encode protocol bytes for printf and ANSI-C shell strings."""
    return ''.join(chr(byte) if 32 <= byte <= 126 and byte != 92
                   else f'\\{byte:03o}' for byte in value)
