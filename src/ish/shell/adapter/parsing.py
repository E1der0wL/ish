"""Select non-evaluating completion parsers for each shell."""

from collections.abc import Callable
from dataclasses import dataclass

from ish.parser.completion import (
    CompletionWord,
    bash_completion,
    completion_context,
    csh_completion,
    zsh_completion,
)


@dataclass(frozen=True)
class ParsingPolicy:
    """Bind a pure completion parser; None declines an unsupported context."""

    completion: Callable[[str, int], CompletionWord | None]


POSIX_PARSING = ParsingPolicy(completion_context)
BASH_PARSING = ParsingPolicy(bash_completion)
ZSH_PARSING = ParsingPolicy(zsh_completion)
CSH_PARSING = ParsingPolicy(csh_completion)
