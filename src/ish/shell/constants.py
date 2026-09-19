"""Shared integration script names, state keys, and session-specific OSC signals."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final

BUILTIN: Final = "builtin"
ENVIRON: Final = "environ"
ALIAS: Final = "alias"
EXITCODE: Final = "exitcode"
PROMPT_ID: Final = "prompt_id"
PROMPT_ID_LIMIT: Final = 2**63
PROMPT_ID_MAX_DIGITS: Final = len(str(PROMPT_ID_LIMIT - 1))
PWD: Final = "PWD"

# The shell templates and the Python receiver share these exact wire signals.
ISH_OSC_PREFIX: Final = b"\x1b]633;"
OSC_TERMINATOR: Final = b"\a"
BEFORE_PROMPT: Final = ISH_OSC_PREFIX + b"S" + OSC_TERMINATOR
AFTER_PROMPT: Final = ISH_OSC_PREFIX + b"E" + OSC_TERMINATOR
BEFORE_CONTINUATION: Final = ISH_OSC_PREFIX + b"s" + OSC_TERMINATOR
BEFORE_BUFFERED_CONTINUATION: Final = ISH_OSC_PREFIX + b"sb" + OSC_TERMINATOR
BUFFERED_CONTINUATION_HINT: Final = ISH_OSC_PREFIX + b"CB" + OSC_TERMINATOR
AFTER_CONTINUATION: Final = ISH_OSC_PREFIX + b"e" + OSC_TERMINATOR
COMMAND_START: Final = ISH_OSC_PREFIX + b"C" + OSC_TERMINATOR
COMMAND_DONE: Final = ISH_OSC_PREFIX + b"D" + OSC_TERMINATOR
PROMPT_ID_PREFIX: Final = ISH_OSC_PREFIX + b"P;"
LINE_READER_READY_PREFIX: Final = ISH_OSC_PREFIX + b"LR;"
LINE_INTERRUPT_ACK_PREFIX: Final = ISH_OSC_PREFIX + b"LI;"

# tcsh's native editor can render control bytes in caret notation.
CARET_BEFORE_PROMPT: Final = BEFORE_PROMPT.replace(b"\x1b", b"^[").replace(b"\a", b"^G")
CARET_AFTER_PROMPT: Final = AFTER_PROMPT.replace(b"\x1b", b"^[").replace(b"\a", b"^G")

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
BUNDLED_FORWARD_DIRECTORY: Final = "libexec"
FORWARD_SOURCE: Final = "ish_forward.c"
SHELL_FIFO: Final = "shell.fifo"
TTY_FIFO: Final = "tty.fifo"
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
        return cls(secrets.token_hex(16))

    def scope(self, sequence: bytes) -> bytes:
        """Insert the session identifier into an OSC or caret-form ish marker."""
        if not self.token:
            return sequence
        prefix = ISH_OSC_PREFIX + self.token.encode("ascii") + b";"
        return sequence.replace(ISH_OSC_PREFIX, prefix, 1).replace(
            ISH_OSC_PREFIX.replace(b"\x1b", b"^["), prefix.replace(b"\x1b", b"^["), 1
        )


def bytes_to_shell_escape(value: bytes) -> str:
    """Encode protocol bytes for printf and ANSI-C shell strings."""
    return "".join(
        chr(byte) if 32 <= byte <= 126 and byte != 92 else f"\\{byte:03o}"
        for byte in value
    )
