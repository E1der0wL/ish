"""Shared terminal bytes, integration script names, and session-specific OSC signals."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final

# Seven-bit terminal spellings shared by the byte-level reader and writer.
ESC: Final = b"\x1b"
BEL: Final = b"\x07"
CSI: Final = ESC + b"["
OSC: Final = ESC + b"]"
ST: Final = ESC + b"\\"
CANCEL_BYTES: Final = (0x18, 0x1A)  # CAN and SUB cancel a control sequence.
SPACE_BYTE: Final = ord(" ")
DEL_BYTE: Final = 0x7F
LF_BYTE: Final = ord("\n")
# xterm private modes selecting the alternate text buffer.
ALT_BUFFER: Final = 47
ALT_CLEAR_ON_LEAVE: Final = 1047
ALT_SAVE_CURSOR: Final = 1049
ALTERNATE_SCREENS: Final = (ALT_BUFFER, ALT_CLEAR_ON_LEAVE, ALT_SAVE_CURSOR)
_BACKSLASH_BYTE = ord("\\")
_CARET_ESCAPE = b"^["
_CARET_BELL = b"^G"
_TOKEN_BYTES = 16

BUILTIN: Final = "builtin"
ENVIRON: Final = "environ"
ALIAS: Final = "alias"
EXITCODE: Final = "exitcode"
PROMPT_ID: Final = "prompt_id"
INPUT_DONE: Final = "input_done"
INPUT_ERROR: Final = "input_error"
PROMPT_ID_LIMIT: Final = 2**63
PROMPT_ID_MAX_DIGITS: Final = len(str(PROMPT_ID_LIMIT - 1))
PWD: Final = "PWD"

# The shell templates and the Python receiver share these exact wire signals.
ISH_OSC_PREFIX: Final = OSC + b"633;"
OSC_TERMINATOR: Final = BEL
BEFORE_PROMPT: Final = ISH_OSC_PREFIX + b"S" + OSC_TERMINATOR
AFTER_PROMPT: Final = ISH_OSC_PREFIX + b"E" + OSC_TERMINATOR
BEFORE_CONTINUATION: Final = ISH_OSC_PREFIX + b"s" + OSC_TERMINATOR
BEFORE_BUFFERED_CONTINUATION: Final = ISH_OSC_PREFIX + b"sb" + OSC_TERMINATOR
BUFFERED_CONTINUATION_HINT: Final = ISH_OSC_PREFIX + b"CB" + OSC_TERMINATOR
AFTER_CONTINUATION: Final = ISH_OSC_PREFIX + b"e" + OSC_TERMINATOR
NATIVE_CONTINUATION: Final = ISH_OSC_PREFIX + b"N" + OSC_TERMINATOR
COMMAND_START: Final = ISH_OSC_PREFIX + b"C" + OSC_TERMINATOR
COMMAND_DONE: Final = ISH_OSC_PREFIX + b"D" + OSC_TERMINATOR
PROMPT_ID_PREFIX: Final = ISH_OSC_PREFIX + b"P;"
LINE_READER_READY_PREFIX: Final = ISH_OSC_PREFIX + b"LR;"
LINE_INTERRUPT_ACK_PREFIX: Final = ISH_OSC_PREFIX + b"LI;"

# tcsh's native editor can render control bytes in caret notation.
_CARET_OSC_PREFIX = ISH_OSC_PREFIX.replace(ESC, _CARET_ESCAPE)
CARET_BEFORE_PROMPT: Final = BEFORE_PROMPT.replace(ESC, _CARET_ESCAPE).replace(
    BEL, _CARET_BELL
)
CARET_AFTER_PROMPT: Final = AFTER_PROMPT.replace(ESC, _CARET_ESCAPE).replace(
    BEL, _CARET_BELL
)

# Keep the existing on-disk names for compatibility.
BASH_INTEGRATION_SCRIPT: Final = "shellIntegraion.sh"
ZSH_INTEGRATION_SCRIPT: Final = "shellIntegraion.zsh"
ZSH_TRAP_SNAPSHOT: Final = "zsh-traps"
CSH_INTEGRATION_SCRIPT: Final = "shellIntegraion.csh"
TCSH_INTEGRATION_SCRIPT: Final = "shellIntegraion.tcsh"
POSIX_INTEGRATION_SCRIPT: Final = "shellIntegraion.posix"
POSIX_UPDATE_SCRIPT: Final = "ish_update.posix"
CSH_UPDATE_SCRIPT: Final = "ish_update.csh"
TCSH_PRECMD_SCRIPT: Final = "ish_precmd.tcsh"
TCSH_BIND_HOOKS_SCRIPT: Final = "ish_bind_hooks.tcsh"
TCSH_WATCH_HOOKS_SCRIPT: Final = "ish_watch_hooks.tcsh"
FORWARD_BINARY: Final = "ish_forward"
NATIVE_COMPILER: Final = "gcc"
BUNDLED_FORWARD_DIRECTORY: Final = "libexec"
FORWARD_SOURCE: Final = "ish_forward.c"
SHELL_FIFO: Final = "shell.fifo"
TTY_FIFO: Final = "tty.fifo"
INPUT_ACK_FIFO: Final = "input-ack.fifo"
# Linux since 4.13; Python's termios module does not expose this on every build.
TIOCGPTPEER: Final = 0x5441


@dataclass(frozen=True)
class SessionSignals:
    """Scope integration markers to one PTY; empty tokens support old fixtures."""

    token: str = ""

    @classmethod
    def create(cls):
        """Create a random signal identifier distinguishable from other sessions' ordinary
        output.
        """
        return cls(secrets.token_hex(_TOKEN_BYTES))

    def scope(self, sequence: bytes) -> bytes:
        """Insert the session identifier into an OSC or caret-form ish marker."""
        if not self.token:
            return sequence
        prefix = ISH_OSC_PREFIX + self.token.encode("ascii") + b";"
        return sequence.replace(ISH_OSC_PREFIX, prefix, 1).replace(
            _CARET_OSC_PREFIX, prefix.replace(ESC, _CARET_ESCAPE), 1
        )


def bytes_to_shell_escape(value: bytes) -> str:
    """Encode protocol bytes for printf and ANSI-C shell strings."""
    return "".join(
        chr(byte)
        if SPACE_BYTE <= byte < DEL_BYTE and byte != _BACKSLASH_BYTE
        else f"\\{byte:03o}"
        for byte in value
    )
