"""Shell syntax, runtime policies and integration setup in one registry."""

import os
import shlex
from dataclasses import dataclass
from typing import Callable

from .constants import (
    AFTER_CONTINUATION,
    AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT,
    BEFORE_CONTINUATION,
    BEFORE_PROMPT,
    CARET_AFTER_PROMPT,
    CARET_BEFORE_PROMPT,
    CSH_INTEGRATION_SCRIPT,
    CSH_UPDATE_SCRIPT,
    POSIX_INTEGRATION_SCRIPT,
    POSIX_UPDATE_SCRIPT,
    PROMPT_ID_PREFIX,
    TCSH_INTEGRATION_SCRIPT,
    ZSH_INTEGRATION_SCRIPT,
    SessionSignals,
)


def csh_quote(value: str) -> str:
    """Escape a single C shell argument and reject paths containing line breaks."""
    if "\n" in value or "\r" in value:
        raise ValueError("C shell integration paths cannot contain newlines")
    return (
        "".join(
            "\\" + c if c.isspace() or c in "'\"\\$`!;&|()<>*?[]{}~#" else c
            for c in value
        )
        or "''"
    )


def posix_aliases(text: str) -> dict[str, str]:
    """Parse quoted POSIX-style alias output into names and bodies without executing it."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return dict(token.split("=", 1) for token in lexer if "=" in token)


def csh_aliases(text: str) -> dict[str, str]:
    """Extract names and bodies from tab-separated C shell alias output."""
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


@dataclass(frozen=True)
class ShellSyntax:
    """Immutable syntax operations for quoting, assignment, and status restoration."""

    quote: Callable[[str], str]
    assignment: str
    exit_status: str
    source_command: str
    parse_aliases: Callable[[str], dict[str, str]]
    lexer: str
    restore_status: str = "_ish_status {value}"

    def assign(self, name: str, expression: str) -> str:
        """Assign an already quoted value or a shell expression."""
        return self.assignment.format(name=name, value=expression)

    def preserve_status(self, command: str) -> str:
        """Wrap a command with syntax that saves and restores the previous exit status."""
        saved = "_ish_recovery_exit_code"
        return (
            self.assign(saved, self.exit_status)
            + "; "
            + command.rstrip("\n")
            + "; "
            + self.restore_status.format(value='"$' + saved + '"')
            + "\n"
        )


# These are the syntax features ish uses, not strict standards compliance
# claims (notably, zsh shares this POSIX-style quoting/assignment profile).
SYNTAXES = {
    "posix": ShellSyntax(
        shlex.quote, "{name}={value}", "$?", ".", posix_aliases, "bash"
    ),
    "csh": ShellSyntax(
        csh_quote,
        "set {name} = {value}",
        "$status",
        "source",
        csh_aliases,
        "tcsh",
        restore_status="set status = {value}",
    ),
}


@dataclass(frozen=True)
class ShellBehavior:
    """Syntax-independent policies for submission, continuation prompts, and output
    prefixes.
    """

    native_continuation: bool = False
    preserve_output_line: bool = False
    batch_input: bool = False

    def split_commands(self, command: str) -> list[str]:
        """Keep the input block intact for batch submission, or split it into executable
        lines.
        """
        if self.batch_input:
            return [command]
        return command.splitlines() if command.strip() else [""]


POSIX_BEHAVIOR = ShellBehavior(native_continuation=True, batch_input=True)
# Sleeping csh/tcsh may be reading foreach or $<. Recovery commands must not
# be injected into that input; keep secondary editing and typeahead native.
CSH_BEHAVIOR = ShellBehavior(
    native_continuation=True,
    preserve_output_line=True,
    batch_input=True,
)
INTEGRATION_ARGUMENTS = ("_ish_pipe", "_tty_pipe")


@dataclass(frozen=True)
class ShellAdapter:
    """Immutable configuration linking shell arguments, syntax, behavior, and integration
    signals.
    """

    name: str
    family: str
    args: tuple[str, ...]
    script: str
    behavior: ShellBehavior = POSIX_BEHAVIOR
    source_command: str | None = None
    # None passes positional arguments; a tuple binds variables before source.
    source_args: tuple[str, ...] | None = None
    refresh_script: str | None = None
    capture_refresh_status: bool = False
    unhooked_prompt: tuple[bytes, bytes] | None = None
    builtins_command: str = ""
    builtins: tuple[str, ...] = ()

    @property
    def syntax(self) -> ShellSyntax:
        """Return the shared syntax operations for this syntax family."""
        return SYNTAXES[self.family]

    @property
    def refresh(self) -> bool:
        """Report whether a separate state refresh script is required after the prompt."""
        return self.refresh_script is not None

    def source(self, path, *args) -> str:
        """Build a source command with paths and arguments quoted for the shell syntax.

        Assign variables first when source arguments are unsupported, and reject
        mismatched variable and argument counts.
        """
        syntax = self.syntax
        command = self.source_command or syntax.source_command
        if self.source_args is not None and args:
            # dash ignores dot arguments; BSD csh source accepts no arguments.
            if len(args) != len(self.source_args):
                raise ValueError(
                    f"{self.name} integration needs {len(self.source_args)} arguments"
                )
            assignments = [
                syntax.assign(name, syntax.quote(str(value)))
                for name, value in zip(self.source_args, args, strict=True)
            ]
            return (
                "; ".join(assignments)
                + "; "
                + command
                + " "
                + syntax.quote(str(path))
                + "\n"
            )
        return (
            command + " " + " ".join(syntax.quote(str(v)) for v in (path, *args)) + "\n"
        )

    def refresh_command(self, directory) -> str:
        """Build explicit state-refresh and status-saving commands only for shells that
        need them.
        """
        if not self.refresh_script:
            return ""
        command = self.source(directory / self.refresh_script)
        if self.capture_refresh_status:
            command = (
                self.syntax.assign("_ish_shell_exit_code", self.syntax.exit_status)
                + "; "
                + command
            )
        return command

    def configure_sequencer(
        self,
        sequencer,
        *,
        prompt_id,
        prompt,
        continuation,
        unhooked_prompt,
        signals=SessionSignals(),
    ):
        """Register session-specific primary, continuation, and status-ID callbacks with
        the sequencer.
        """
        scope = signals.scope
        sequencer.on_prefix(scope(PROMPT_ID_PREFIX), prompt_id)
        sequencer.between_sequence(scope(BEFORE_PROMPT), scope(AFTER_PROMPT), prompt)
        sequencer.between_sequence(
            scope(BEFORE_CONTINUATION), scope(AFTER_CONTINUATION), continuation
        )
        if self.unhooked_prompt:
            sequencer.between_sequence(
                *(scope(marker) for marker in self.unhooked_prompt), unhooked_prompt
            )


ADAPTERS = {
    "bash": ShellAdapter(
        "bash",
        "posix",
        ("--noediting", "-i"),
        BASH_INTEGRATION_SCRIPT,
        source_command="source",
        builtins_command="compgen -b",
    ),
    "zsh": ShellAdapter(
        "zsh",
        "posix",
        ("-i",),
        ZSH_INTEGRATION_SCRIPT,
        source_command="source",
        builtins_command='printf "%s\\n" ${(k)builtins}',
    ),
    "tcsh": ShellAdapter(
        "tcsh",
        "csh",
        ("-i",),
        TCSH_INTEGRATION_SCRIPT,
        behavior=CSH_BEHAVIOR,
        unhooked_prompt=(CARET_BEFORE_PROMPT, CARET_AFTER_PROMPT),
        builtins_command="builtins",
    ),
    "csh": ShellAdapter(
        "csh",
        "csh",
        ("-i",),
        CSH_INTEGRATION_SCRIPT,
        behavior=CSH_BEHAVIOR,
        source_args=INTEGRATION_ARGUMENTS,
        refresh_script=CSH_UPDATE_SCRIPT,
        capture_refresh_status=True,
        builtins=tuple(
            "alias bg break breaksw case cd chdir continue default dirs echo else end endif endsw "
            "eval exec exit fg foreach glob goto hashstat history if jobs kill limit login logout "
            "nice nohup notify onintr popd pushd rehash repeat set setenv shift source stop suspend "
            "switch time umask unalias unhash unlimit unset unsetenv wait while".split()
        ),
    ),
    "sh": ShellAdapter(
        "sh",
        "posix",
        ("-i",),
        POSIX_INTEGRATION_SCRIPT,
        source_args=INTEGRATION_ARGUMENTS,
        refresh_script=POSIX_UPDATE_SCRIPT,
        builtins=tuple(
            "alias bg break cd command continue eval exec exit export false fc fg getopts hash jobs "
            "kill printf pwd read readonly return set shift test times trap true type ulimit umask "
            "unalias unset wait".split()
        ),
    ),
}

# Membership is declared only above; this view is useful for discovery/docs.
SHELL_CATEGORIES = {
    family: tuple(
        name for name, adapter in ADAPTERS.items() if adapter.family == family
    )
    for family in SYNTAXES
}
SHELL_ALIASES = {"bsd-csh": "csh", "dash": "sh"}
# /bin/csh may be a tcsh symlink, which needs tcsh's integration hooks.
RESOLVED_SHELL_ALIASES = {("csh", "tcsh"): "tcsh"}


def get_adapter(shell: str, path: str | None = None) -> ShellAdapter:
    """Normalize executable aliases and csh symlinks and find a supported adapter."""
    name = os.path.basename(shell)
    name = SHELL_ALIASES.get(name, name)
    if path:
        name = RESOLVED_SHELL_ALIASES.get(
            (name, os.path.basename(os.path.realpath(path))), name
        )
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"Unsupported shell {shell!r}. Choose {', '.join(ADAPTERS)}."
        ) from None
