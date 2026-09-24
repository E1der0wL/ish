"""Select non-evaluating completion and literal argument parsers for each shell."""

from collections.abc import Callable
from dataclasses import dataclass

from ish.parser.command import (
    bash_literal_argv,
    csh_literal_argv,
    literal_argv,
    zsh_literal_argv,
)
from ish.parser.completion import (
    CompletionWord,
    bash_completion,
    completion_context,
    csh_completion,
    zsh_completion,
)


@dataclass(frozen=True)
class ParsingPolicy:
    """Bind pure parsers; None declines a context or direct tool dispatch."""

    completion: Callable[[str, int], CompletionWord | None]
    tool_argv: Callable[[str], list[str] | None]


POSIX_PARSING = ParsingPolicy(completion_context, literal_argv)
BASH_PARSING = ParsingPolicy(bash_completion, bash_literal_argv)
ZSH_PARSING = ParsingPolicy(zsh_completion, zsh_literal_argv)
CSH_PARSING = ParsingPolicy(csh_completion, csh_literal_argv)
