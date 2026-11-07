"""Lexical rules shared by completion and literal Python-tool arguments."""

import re
from types import MappingProxyType

# Both public parser families apply the same preset for the selected shell.
BASH_OPTIONS = MappingProxyType({"history": True})
ZSH_OPTIONS = MappingProxyType({"history": True, "zsh": True})
CSH_OPTIONS = MappingProxyType({"history": True, "csh": True})

SINGLE_QUOTE = "'"
DOUBLE_QUOTE = '"'
QUOTES = SINGLE_QUOTE + DOUBLE_QUOTE
ESCAPE = "\\"
HISTORY = "!"
QUICK_SUBSTITUTION = "^"
EXPANSION = "$`"
COMMENT = "#"
ASSIGNMENT = "="
TILDE = "~"
BLANKS = " \t"
COMMAND_SEPARATORS = ";|&\n"
REDIRECTIONS = "<>"
PARENTHESES = "()"
UNQUOTED_EXPANSIONS = EXPANSION + "*?[]{}"
# CR and all the other C0 controls except tab and LF are rejected even when escaped.
REJECTED_CONTROLS = frozenset(chr(code) for code in range(32)) - frozenset("\t\n")
_DOUBLE_ESCAPES = EXPANSION + DOUBLE_QUOTE + ESCAPE
CSH_QUOTED_NATIVE = ESCAPE + "\n"

# All scanner transitions begin on one of these characters. The complementary
# run represents literal text in every supported quoting state and dialect.
_SYNTAX = (
    BLANKS
    + QUOTES
    + ESCAPE
    + HISTORY
    + QUICK_SUBSTITUTION
    + COMMENT
    + ASSIGNMENT
    + TILDE
    + COMMAND_SEPARATORS
    + REDIRECTIONS
    + PARENTHESES
    + UNQUOTED_EXPANSIONS
)
ORDINARY_RUN = re.compile(
    "[^" + re.escape(_SYNTAX + "".join(sorted(REJECTED_CONTROLS))) + "]+"
)


def history_expands(quote: str | None, csh: bool) -> bool:
    """Decide whether an enabled history expansion can run in this quote context."""
    return csh or quote != SINGLE_QUOTE


def decode_escape(char: str, quote: str | None, csh: bool, zsh: bool) -> str | None:
    """Return the represented text, or defer to the shell for unsupported escapes."""
    if char in REJECTED_CONTROLS or csh and (quote or char == "\n"):
        return None
    if char == "\n":
        return ""
    if quote == DOUBLE_QUOTE:
        if zsh and char == HISTORY:
            # BANG_HIST decides whether this backslash survives.
            return None
        if char not in _DOUBLE_ESCAPES:
            return ESCAPE + char
    return char
