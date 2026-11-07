"""Decode literal tool arguments without evaluating shell expressions or state.

Only the verified literal subset is handled here. Unsupported quoting, history,
expansion, and compound syntax return None so the original input stays native.
"""

from .lexical import (
    ASSIGNMENT,
    BASH_OPTIONS,
    BLANKS,
    COMMAND_SEPARATORS,
    COMMENT,
    CSH_OPTIONS,
    CSH_QUOTED_NATIVE,
    DOUBLE_QUOTE,
    ESCAPE,
    EXPANSION,
    HISTORY,
    ORDINARY_RUN,
    PARENTHESES,
    QUICK_SUBSTITUTION,
    QUOTES,
    REDIRECTIONS,
    REJECTED_CONTROLS,
    SINGLE_QUOTE,
    TILDE,
    UNQUOTED_EXPANSIONS,
    ZSH_OPTIONS,
    decode_escape,
    history_expands,
)

_NATIVE_OPERATORS = (
    COMMAND_SEPARATORS + PARENTHESES + REDIRECTIONS + UNQUOTED_EXPANSIONS + TILDE
)


def literal_argv(
    text: str, *, csh: bool = False, history: bool = False, zsh: bool = False
) -> list[str] | None:
    """Scan one literal argv using the selected shell's lexical restrictions.

    C shells do not use POSIX backslash rules inside quotes. Their quoted
    backslashes are left native because backslash_quote can alter the result.
    Zsh adjacent single quotes are left native because RC_QUOTES is not tracked.
    """
    argv = []
    value = []
    active = False
    quote = None
    i = 0
    while i < len(text):
        ordinary = ORDINARY_RUN.match(text, i)
        if ordinary:
            value.append(ordinary[0])
            active = True
            i = ordinary.end()
            continue
        char = text[i]
        if char in REJECTED_CONTROLS:
            return None
        if char == HISTORY and history and history_expands(quote, csh):
            return None
        if quote == SINGLE_QUOTE:
            if char == SINGLE_QUOTE:
                if zsh and text.startswith(SINGLE_QUOTE, i + 1):
                    return None
                quote = None
            elif csh and char in CSH_QUOTED_NATIVE:
                return None
            else:
                value.append(char)
        elif char == ESCAPE:
            if i + 1 == len(text):
                return None
            escaped = decode_escape(text[i + 1], quote, csh, zsh)
            if escaped is None:
                return None
            if escaped:
                value.append(escaped)
                active = True
            i += 2
            continue
        elif quote == DOUBLE_QUOTE:
            if char == DOUBLE_QUOTE:
                quote = None
            elif char in EXPANSION or csh and char == "\n":
                return None
            else:
                value.append(char)
        elif char in QUOTES:
            quote = char
            active = True
        elif char in BLANKS:
            if active:
                argv.append("".join(value))
                value, active = [], False
        elif char in _NATIVE_OPERATORS:
            return None
        elif char == COMMENT and (csh or zsh or not active):
            return None
        elif zsh and (char == QUICK_SUBSTITUTION or char == ASSIGNMENT and not active):
            return None
        elif not argv and char == ASSIGNMENT:
            # A leading assignment belongs to the shell, not a tool named NAME=value.
            return None
        elif not argv and not active and char == QUICK_SUBSTITUTION and history:
            return None
        else:
            value.append(char)
            active = True
        i += 1
    if quote:
        return None
    if active:
        argv.append("".join(value))
    return argv


def bash_literal_argv(text: str) -> list[str] | None:
    """Decode Bash literals while leaving unquoted history references native."""
    return literal_argv(text, **BASH_OPTIONS)


def zsh_literal_argv(text: str) -> list[str] | None:
    """Avoid zsh history, extended glob syntax, and option-dependent quoting."""
    return literal_argv(text, **ZSH_OPTIONS)


def csh_literal_argv(text: str) -> list[str] | None:
    """Decode C shell literals without applying POSIX quoted escapes."""
    return literal_argv(text, **CSH_OPTIONS)
