"""Decode literal tool arguments without evaluating shell expressions or state.

Only the verified literal subset is handled here. Unsupported quoting, history,
expansion, and compound syntax return None so the original input stays native.
"""


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
        char = text[i]
        if char == "\r" or ord(char) < 32 and char not in "\t\n":
            return None
        if char == "!" and history and (csh or quote != "'"):
            return None
        if quote == "'":
            if char == "'":
                if zsh and text[i + 1 : i + 2] == "'":
                    return None
                quote = None
            elif csh and char in "\\\n":
                return None
            else:
                value.append(char)
        elif char == "\\":
            if i + 1 == len(text) or csh and quote:
                return None
            nxt = text[i + 1]
            if zsh and quote == '"' and nxt == "!":
                # BANG_HIST changes whether this backslash survives in interactive zsh.
                return None
            if nxt == "\r" or ord(nxt) < 32 and nxt not in "\t\n":
                return None
            if nxt == "\n":
                if csh:
                    return None
            else:
                if quote == '"' and nxt not in '$`"\\':
                    value.append("\\")
                value.append(nxt)
                active = True
            i += 2
            continue
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in "$`" or csh and char == "\n":
                return None
            else:
                value.append(char)
        elif char in "'\"":
            quote = char
            active = True
        elif char in " \t":
            if active:
                argv.append("".join(value))
                value, active = [], False
        elif char in ";&|()<>\n$`*?[]{}~":
            return None
        elif char == "#" and (csh or zsh or not active):
            return None
        elif zsh and (char == "^" or char == "=" and not active):
            return None
        elif not argv and char == "=":
            # A leading assignment belongs to the shell, not a tool named NAME=value.
            return None
        elif not argv and not active and char == "^" and history:
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
    return literal_argv(text, history=True)


def zsh_literal_argv(text: str) -> list[str] | None:
    """Avoid zsh history, extended glob syntax, and option-dependent quoting."""
    return literal_argv(text, history=True, zsh=True)


def csh_literal_argv(text: str) -> list[str] | None:
    """Decode C shell literals without applying POSIX quoted escapes."""
    return literal_argv(text, csh=True, history=True)
