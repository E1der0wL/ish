"""Small, non-evaluating shell lexer for the word at the cursor.

It tracks quotes, escapes, operators and nested command substitutions. Dynamic
words are left to the shell; completion never evaluates a substitution.
"""

from dataclasses import dataclass

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

_POSIX_COMMAND_PREFIXES = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "command", "exec"}
)
_CSH_COMMAND_PREFIXES = frozenset({"exec", "time", "nohup"})
_WORD_BOUNDARIES = BLANKS + COMMAND_SEPARATORS + PARENTHESES + REDIRECTIONS
_CURSOR_BOUNDARIES = BLANKS + COMMAND_SEPARATORS
_ARITHMETIC_START, _ARITHMETIC_END = "$((", "))"
_COMMAND_SUBSTITUTION, _SUBSTITUTION_END = "$(", ")"
_BACKTICK = "`"
_HEREDOC_START = "<<"


@dataclass
class CompletionWord:
    """Describe a prefix; dynamic marks expansion or syntax needing native interpretation."""

    start: int
    value: str
    command: bool
    dynamic: bool = False
    quote: str | None = None


def completion_word(
    text: str, *, csh: bool = False, history: bool = False, zsh: bool = False
) -> CompletionWord:
    """Extract the literal word to replace and its command position before the cursor.

    Track quoting, escapes, redirections, and nested command substitutions without
    executing expansions.
    """
    start = 0
    value = []
    active = False
    quote = None
    dynamic = False
    need_command = True
    redirect = False
    stack = []
    quoted_word = False
    keywords = _CSH_COMMAND_PREFIXES if csh else _POSIX_COMMAND_PREFIXES
    i = 0

    def end_word():
        """Use the completed word to update command and redirection context for the next
        word.
        """
        nonlocal active, value, dynamic, need_command, redirect, quoted_word
        if active:
            word = "".join(value)
            assignment, separator, _ = text[start:i].partition(ASSIGNMENT)
            prefix = (
                not csh
                and bool(separator)
                and assignment.isascii()
                and assignment.isidentifier()
            )
            prefix |= not quoted_word and need_command and word in keywords
            if not redirect and not prefix:
                need_command = False
            redirect = False
        active, value, dynamic, quoted_word = False, [], False, False

    while i < len(text):
        ordinary = ORDINARY_RUN.match(text, i)
        if ordinary:
            if not active:
                start, active = i, True
            value.append(ordinary[0])
            i = ordinary.end()
            continue
        c = text[i]
        if c in REJECTED_CONTROLS:
            return CompletionWord(start, "", False, True)
        if c == HISTORY and history and history_expands(quote, csh):
            return CompletionWord(start, "", False, True)
        if (
            c == QUICK_SUBSTITUTION
            and history
            and quote is None
            and not value
            and need_command
        ):
            return CompletionWord(start, "", False, True)
        if not active and c not in _WORD_BOUNDARIES:
            start, active = i, True
        if c == ESCAPE and quote != SINGLE_QUOTE:
            quoted_word = True
            if i + 1 < len(text):
                escaped = decode_escape(text[i + 1], quote, csh, zsh)
                if escaped is None:
                    return CompletionWord(start, "", False, True)
                if escaped:
                    value.append(escaped)
                i += 2
                continue
            if csh and quote:
                return CompletionWord(start, "", False, True)
            dynamic = True  # A trailing escape is not yet a complete prefix.
        elif quote == SINGLE_QUOTE:
            if c == SINGLE_QUOTE:
                if zsh and text.startswith(SINGLE_QUOTE, i + 1):
                    return CompletionWord(start, "", False, True)
                quote = None
            elif csh and c in CSH_QUOTED_NATIVE:
                return CompletionWord(start, "", False, True)
            else:
                value.append(c)
        elif not csh and text.startswith(_ARITHMETIC_START, i):
            # Arithmetic is an expression, not a command position.
            end = text.find(_ARITHMETIC_END, i + len(_ARITHMETIC_START))
            if end < 0:
                return CompletionWord(start, "", False, True)
            dynamic = True
            i = end + len(_ARITHMETIC_END)
            continue
        elif (not csh and text.startswith(_COMMAND_SUBSTITUTION, i)) or (
            c == _BACKTICK and (not stack or stack[-1][-1] != _BACKTICK)
        ):
            stack.append(
                (
                    start,
                    value,
                    active,
                    quote,
                    need_command,
                    redirect,
                    quoted_word,
                    _SUBSTITUTION_END if c != _BACKTICK else _BACKTICK,
                )
            )
            i += len(_COMMAND_SUBSTITUTION) if c != _BACKTICK else 1
            start, value, active, quote, dynamic = i, [], False, None, False
            need_command, redirect = True, False
            quoted_word = False
            continue
        elif stack and c == stack[-1][-1] and quote is None:
            start, value, active, quote, need_command, redirect, quoted_word, _ = (
                stack.pop()
            )
            dynamic = True
        elif quote == DOUBLE_QUOTE:
            if c == DOUBLE_QUOTE:
                quote = None
            elif csh and c == "\n":
                return CompletionWord(start, "", False, True)
            else:
                value.append(c)
                dynamic |= c == "$"
        elif c in QUOTES:
            quote = c
            quoted_word = True
        elif c in BLANKS:
            end_word()
            start = i + 1
        elif c in PARENTHESES:
            if csh or active or not need_command:
                return CompletionWord(start, "", False, True)
            end_word()
            need_command, redirect, start = True, False, i + 1
        elif c in COMMAND_SEPARATORS:
            end_word()
            need_command, redirect, start = True, False, i + 1
        elif c in REDIRECTIONS:
            if text.startswith(_HEREDOC_START, i):
                return CompletionWord(start, "", False, True)
            end_word()
            redirect, start = True, i + 1
        elif c == COMMENT and (not value or csh or zsh):
            return CompletionWord(len(text), "", False, True)
        else:
            value.append(c)
            dynamic |= c in UNQUOTED_EXPANSIONS
            dynamic |= zsh and (
                c == QUICK_SUBSTITUTION
                or c == TILDE
                and len(value) > 1
                or c == ASSIGNMENT
                and len(value) == 1
            )
        i += 1
    return CompletionWord(
        start, "".join(value), need_command and not redirect, dynamic, quote
    )


def completion_context(
    text: str,
    cursor: int,
    *,
    csh: bool = False,
    history: bool = False,
    zsh: bool = False,
) -> CompletionWord | None:
    """Analyze a replaceable prefix without erasing or reinterpreting the cursor suffix."""
    if not 0 <= cursor <= len(text):
        return None
    word = completion_word(text[:cursor], csh=csh, history=history, zsh=zsh)
    if cursor < len(text) and (word.quote or text[cursor] not in _CURSOR_BOUNDARIES):
        return None
    return word


def bash_completion(text: str, cursor: int) -> CompletionWord | None:
    """Keep Bash history references out of literal completion contexts."""
    return completion_context(text, cursor, **BASH_OPTIONS)


def zsh_completion(text: str, cursor: int) -> CompletionWord | None:
    """Respect zsh expansion and uncertain quote-option boundaries."""
    return completion_context(text, cursor, **ZSH_OPTIONS)


def csh_completion(text: str, cursor: int) -> CompletionWord | None:
    """Avoid POSIX assignments, substitutions, and quoted escapes in C shells."""
    return completion_context(text, cursor, **CSH_OPTIONS)
