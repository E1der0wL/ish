"""Small, non-evaluating shell lexer for the word at the cursor.

It tracks quotes, escapes, operators and nested command substitutions. Dynamic
words are left to the shell; completion never evaluates a substitution.
"""

from dataclasses import dataclass

_POSIX_COMMAND_PREFIXES = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "command", "exec"}
)
_CSH_COMMAND_PREFIXES = frozenset({"exec", "time", "nohup"})


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
            assignment, separator, _ = text[start:i].partition("=")
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
        c = text[i]
        if c == "\r" or ord(c) < 32 and c not in "\t\n":
            return CompletionWord(start, "", False, True)
        if c == "!" and history and (csh or quote != "'"):
            return CompletionWord(start, "", False, True)
        if c == "^" and history and quote is None and not value and need_command:
            return CompletionWord(start, "", False, True)
        if not active and c not in " \t\r\n;&|()<>":
            start, active = i, True
        if c == "\\" and quote != "'":
            quoted_word = True
            if csh and quote:
                return CompletionWord(start, "", False, True)
            if i + 1 < len(text):
                nxt = text[i + 1]
                if zsh and quote == '"' and nxt == "!":
                    return CompletionWord(start, "", False, True)
                if nxt == "\r" or ord(nxt) < 32 and nxt not in "\t\n":
                    return CompletionWord(start, "", False, True)
                if csh and nxt == "\n":
                    return CompletionWord(start, "", False, True)
                if quote == '"' and nxt not in '$`"\\\n':
                    value.append("\\")
                if nxt != "\n":
                    value.append(nxt)
                i += 2
                continue
            dynamic = True  # A trailing escape is not yet a complete prefix.
        elif quote == "'":
            if c == "'":
                if zsh and text[i + 1 : i + 2] == "'":
                    return CompletionWord(start, "", False, True)
                quote = None
            elif csh and c in "\\\n":
                return CompletionWord(start, "", False, True)
            else:
                value.append(c)
        elif not csh and text.startswith("$((", i):
            # Arithmetic is an expression, not a command position.
            end = text.find("))", i + 3)
            if end < 0:
                return CompletionWord(start, "", False, True)
            dynamic = True
            i = end + 2
            continue
        elif (not csh and text.startswith("$(", i)) or (
            c == "`" and (not stack or stack[-1][-1] != "`")
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
                    ")" if c == "$" else "`",
                )
            )
            i += 2 if c == "$" else 1
            start, value, active, quote, dynamic = i, [], False, None, False
            need_command, redirect = True, False
            quoted_word = False
            continue
        elif stack and c == stack[-1][-1] and quote is None:
            start, value, active, quote, need_command, redirect, quoted_word, _ = (
                stack.pop()
            )
            dynamic = True
        elif quote == '"':
            if c == '"':
                quote = None
            elif csh and c == "\n":
                return CompletionWord(start, "", False, True)
            else:
                value.append(c)
                dynamic |= c == "$"
        elif c in "'\"":
            quote = c
            quoted_word = True
        elif c in " \t\r":
            end_word()
            start = i + 1
        elif c in "()":
            if csh or active or not need_command:
                return CompletionWord(start, "", False, True)
            end_word()
            need_command, redirect, start = True, False, i + 1
        elif c in ";|&\n":
            end_word()
            need_command, redirect, start = True, False, i + 1
        elif c in "<>":
            if text.startswith("<<", i):
                return CompletionWord(start, "", False, True)
            end_word()
            redirect, start = True, i + 1
        elif c == "#" and (not value or csh or zsh):
            return CompletionWord(len(text), "", False, True)
        else:
            value.append(c)
            dynamic |= c in "$`*?[]{}"
            dynamic |= zsh and (
                c == "^" or c == "~" and len(value) > 1 or c == "=" and len(value) == 1
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
    if cursor < len(text) and (word.quote or text[cursor] not in " \t\n;&|"):
        return None
    return word


def bash_completion(text: str, cursor: int) -> CompletionWord | None:
    """Keep Bash history references out of literal completion contexts."""
    return completion_context(text, cursor, history=True)


def zsh_completion(text: str, cursor: int) -> CompletionWord | None:
    """Respect zsh expansion and uncertain quote-option boundaries."""
    return completion_context(text, cursor, history=True, zsh=True)


def csh_completion(text: str, cursor: int) -> CompletionWord | None:
    """Avoid POSIX assignments, substitutions, and quoted escapes in C shells."""
    return completion_context(text, cursor, csh=True, history=True)
