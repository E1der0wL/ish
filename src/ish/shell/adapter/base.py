"""Shell syntax, runtime policies and integration setup in one registry."""

import os
import shlex
import signal
import termios
from dataclasses import dataclass
from typing import Callable

from ..constants import (
    AFTER_CONTINUATION,
    AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT,
    BEFORE_BUFFERED_CONTINUATION,
    BEFORE_CONTINUATION,
    BEFORE_PROMPT,
    BUFFERED_CONTINUATION_HINT,
    CARET_AFTER_PROMPT,
    CARET_BEFORE_PROMPT,
    CSH_INTEGRATION_SCRIPT,
    CSH_UPDATE_SCRIPT,
    NATIVE_CONTINUATION,
    POSIX_INTEGRATION_SCRIPT,
    PROMPT_ID_PREFIX,
    TCSH_INTEGRATION_SCRIPT,
    ZSH_INTEGRATION_SCRIPT,
    SessionSignals,
)
from ..guard import (
    GUARD_ACTIVE,
    GUARD_CHECKED,
    SHELL_GUARD_SOURCE,
    NativeFeature,
    NativeLibrary,
    PreloadPolicy,
)
from ..input import InputHandoffMode, LongInputMode
from ..signals import SignalPolicy, TerminalSignal
from .handlers import ZshInterrupt
from .parsing import (
    BASH_PARSING,
    CSH_PARSING,
    POSIX_PARSING,
    ZSH_PARSING,
    ParsingPolicy,
)

# Build and activation choices live here. Native implementations stay in guard.py.
NATIVE_LIBRARIES = {
    "shell": NativeLibrary("ish_shell.so", "ish_shell.c", SHELL_GUARD_SOURCE),
}

POSIX_PRELOAD_CHECK = """if [ "${{{checked}-}}" != {token} ]; then
    if [ "${{{active}-}}" != {token} ]; then
        printf '%s\\n' {message} >&2
        exit 1
    fi
    unset {active}
    {checked}={token}
fi
"""
CSH_PRELOAD_CHECK = """if (! $?{checked}) set {checked} = ""
if ("${checked}" != {token}) then
    if (! $?{active}) then
        echo {message}
        exit 1
    endif
    if ("${active}" != {token}) then
        echo {message}
        exit 1
    endif
    unsetenv {active}
    set {checked} = {token}
endif
"""


def posix_quote(value: str) -> str:
    """Quote a literal POSIX-style word, including zsh's leading equals expansion."""
    quoted = shlex.quote(value)
    return f"'{value}'" if value.startswith("=") and quoted == value else quoted


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
    preload_check: str = ""

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
        posix_quote,
        "{name}={value}",
        "$?",
        ".",
        posix_aliases,
        "bash",
        preload_check=POSIX_PRELOAD_CHECK,
    ),
    "csh": ShellSyntax(
        csh_quote,
        "set {name} = {value}",
        "$status",
        "source",
        csh_aliases,
        "tcsh",
        restore_status="set status = {value}",
        preload_check=CSH_PRELOAD_CHECK,
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
    parsing: ParsingPolicy
    behavior: ShellBehavior = POSIX_BEHAVIOR
    source_command: str | None = None
    # None passes positional arguments; a tuple binds variables before source.
    source_args: tuple[str, ...] | None = None
    refresh_script: str | None = None
    capture_refresh_status: bool = False
    unhooked_prompt: tuple[bytes, bytes] | None = None
    native_continuation_signal: bytes | None = None
    preload: PreloadPolicy | None = None
    buffered_continuation: tuple[bytes, bytes] | None = None
    buffered_continuation_suffix: bytes | None = None
    builtins_command: str = ""
    builtins: tuple[str, ...] = ()
    long_input: LongInputMode = LongInputMode.REJECT
    signal_policy: SignalPolicy = SignalPolicy()
    builtins_args: tuple[str, ...] = ("-c",)
    input_handoff: InputHandoffMode = InputHandoffMode.PROMPT_ACK
    # Trusted literals in the corresponding template's shell quoting context.
    template_tokens: tuple[tuple[str, str], ...] = ()

    @property
    def syntax(self) -> ShellSyntax:
        """Return the shared syntax operations for this syntax family."""
        return SYNTAXES[self.family]

    @property
    def refresh(self) -> bool:
        """Report whether a separate state-refresh script is configured."""
        return self.refresh_script is not None

    def preload_check(self) -> str:
        """Render startup confirmation for this adapter's selected native policy."""
        if self.preload is None:
            return ""
        if not self.syntax.preload_check:
            raise ValueError(
                f"Native startup confirmation is not defined for {self.family}"
            )
        message = (
            f"ish: {self.name} requires {self.preload.library.binary_name} "
            "with its configured policy (a dynamically linked Linux shell)."
        )
        return self.syntax.preload_check.format(
            active=GUARD_ACTIVE,
            checked=GUARD_CHECKED,
            token=self.syntax.quote(self.preload.activation_token),
            message=self.syntax.quote(message),
        )

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
        continuation_callback = continuation
        if self.buffered_continuation_suffix:
            suffix = scope(self.buffered_continuation_suffix)

            def continuation_callback(payload):
                """Apply a trailing display hint only to its own complete capture."""
                if payload.endswith(suffix):
                    return continuation(payload[: -len(suffix)], buffered=True)
                return continuation(payload)

        sequencer.on_prefix(scope(PROMPT_ID_PREFIX), prompt_id, restart_capture=True)
        sequencer.between_sequence(scope(BEFORE_PROMPT), scope(AFTER_PROMPT), prompt)
        sequencer.between_sequence(
            scope(BEFORE_CONTINUATION), scope(AFTER_CONTINUATION), continuation_callback
        )
        if self.buffered_continuation:
            sequencer.between_sequence(
                *(scope(marker) for marker in self.buffered_continuation),
                lambda prompt: continuation(prompt, buffered=True),
            )
        if self.unhooked_prompt:
            sequencer.between_sequence(
                *(scope(marker) for marker in self.unhooked_prompt), unhooked_prompt
            )
        if self.native_continuation_signal:
            sequencer.on_sequence(
                scope(self.native_continuation_signal), lambda: continuation(b"")
            )


ADAPTERS = {
    "bash": ShellAdapter(
        "bash",
        "posix",
        ("--noediting", "-i"),
        BASH_INTEGRATION_SCRIPT,
        parsing=BASH_PARSING,
        source_command="source",
        buffered_continuation=(BEFORE_BUFFERED_CONTINUATION, AFTER_CONTINUATION),
        builtins_command="compgen -b",
        builtins_args=("--noprofile", "--norc", "-c"),
        template_tokens=(("@BASH_DEFAULT_CONTINUATION@", "> "),),
        long_input=LongInputMode.STAGED_FIRST_LINE,
    ),
    "zsh": ShellAdapter(
        "zsh",
        "posix",
        ("-i",),
        ZSH_INTEGRATION_SCRIPT,
        parsing=ZSH_PARSING,
        source_command="source",
        buffered_continuation_suffix=BUFFERED_CONTINUATION_HINT,
        builtins_command='printf "%s\\n" ${(k)builtins}',
        # -f skips user rc files; zsh always reads its system zshenv.
        builtins_args=("-f", "-c"),
        template_tokens=(("@ZSH_DEFAULT_CONTINUATION@", "%_> "),),
        # Releasing a cancelled no-ZLE line requires a matching SIGINT response.
        long_input=LongInputMode.STAGED_FIRST_LINE,
        signal_policy=SignalPolicy(
            terminal=(TerminalSignal(signal.SIGINT, termios.VINTR, ZshInterrupt),)
        ),
    ),
    "tcsh": ShellAdapter(
        "tcsh",
        "csh",
        ("-i",),
        TCSH_INTEGRATION_SCRIPT,
        parsing=CSH_PARSING,
        behavior=CSH_BEHAVIOR,
        unhooked_prompt=(CARET_BEFORE_PROMPT, CARET_AFTER_PROMPT),
        native_continuation_signal=NATIVE_CONTINUATION,
        template_tokens=(("@TCSH_DEFAULT_CONTINUATION@", "%R? "),),
        preload=PreloadPolicy(
            NATIVE_LIBRARIES["shell"], NativeFeature.SUPPRESS_TTY_READAHEAD
        ),
        builtins_command="builtins",
        builtins_args=("-f", "-c"),
        long_input=LongInputMode.STAGED_FIRST_LINE,
    ),
    "csh": ShellAdapter(
        "csh",
        "csh",
        ("-i",),
        CSH_INTEGRATION_SCRIPT,
        parsing=CSH_PARSING,
        behavior=CSH_BEHAVIOR,
        source_args=INTEGRATION_ARGUMENTS,
        refresh_script=CSH_UPDATE_SCRIPT,
        capture_refresh_status=True,
        input_handoff=InputHandoffMode.NATIVE,
        long_input=LongInputMode.STAGED_FIRST_LINE,
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
        parsing=POSIX_PARSING,
        source_args=INTEGRATION_ARGUMENTS,
        # dash can execute a suffix after rejecting an earlier long fragment.
        long_input=LongInputMode.REJECT,
        builtins=tuple(
            "alias bg break cd command continue eval exec exit export false fc fg getopts hash jobs "
            "kill printf pwd read readonly return set shift test times trap true type ulimit umask "
            "unalias unset wait".split()
        ),
    ),
}


def preload_libraries() -> tuple[NativeLibrary, ...]:
    """Collect only libraries used by adapters, once per distinct artifact name."""
    libraries = {}
    for adapter in ADAPTERS.values():
        if adapter.preload is not None:
            library = adapter.preload.library
            previous = libraries.setdefault(library.binary_name, library)
            if previous != library:
                raise ValueError(f"Conflicting native library: {library.binary_name}")
    return tuple(libraries.values())


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
