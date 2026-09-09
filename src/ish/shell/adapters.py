"""Shell syntax, runtime policies and integration setup in one registry."""
from dataclasses import dataclass
import os
import shlex
from typing import Callable

from .constants import (
    BEFORE_PROMPT, AFTER_PROMPT, BEFORE_CONTINUATION, AFTER_CONTINUATION,
    PROMPT_ID_PREFIX, CARET_BEFORE_PROMPT, CARET_AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT, ZSH_INTEGRATION_SCRIPT, CSH_INTEGRATION_SCRIPT,
    TCSH_INTEGRATION_SCRIPT, POSIX_INTEGRATION_SCRIPT,
    CSH_UPDATE_SCRIPT, POSIX_UPDATE_SCRIPT,
)


def csh_quote(value: str) -> str:
    if '\n' in value or '\r' in value:
        raise ValueError('C shell integration paths cannot contain newlines')
    return ''.join('\\' + c if c.isspace() or c in "'\"\\$`!;&|()<>*?[]{}~#" else c for c in value) or "''"


def posix_aliases(text: str) -> dict[str, str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    return dict(token.split('=', 1) for token in lexer if '=' in token)


def csh_aliases(text: str) -> dict[str, str]:
    return dict(line.split('\t', 1) for line in text.splitlines() if '\t' in line)


@dataclass(frozen=True)
class ShellSyntax:
    quote: Callable[[str], str]
    assignment: str
    exit_status: str
    source_command: str
    parse_aliases: Callable[[str], dict[str, str]]
    lexer: str

    def assign(self, name: str, expression: str) -> str:
        """Assign an already quoted value or a shell expression."""
        return self.assignment.format(name=name, value=expression)


# These are the syntax features ish uses, not strict standards compliance
# claims (notably, zsh shares this POSIX-style quoting/assignment profile).
SYNTAXES = {
    'posix': ShellSyntax(shlex.quote, '{name}={value}', '$?', '.', posix_aliases, 'bash'),
    'csh': ShellSyntax(csh_quote, 'set {name} = {value}', '$status', 'source', csh_aliases, 'tcsh'),
}


@dataclass(frozen=True)
class ShellBehavior:
    native_continuation: bool = False
    preserve_output_line: bool = False
    batch_input: bool = False
    idle_recovery: bool = True

    def split_commands(self, command: str) -> list[str]:
        if self.batch_input:
            return [command]
        return command.splitlines() if command.strip() else ['']


POSIX_BEHAVIOR = ShellBehavior()
# Sleeping csh/tcsh may be reading foreach or $<. Recovery commands must not
# be injected into that input; keep secondary editing and typeahead native.
CSH_BEHAVIOR = ShellBehavior(
    native_continuation=True, preserve_output_line=True,
    batch_input=True, idle_recovery=False,
)
INTEGRATION_ARGUMENTS = ('_ish_pipe', '_tty_pipe')


@dataclass(frozen=True)
class ShellAdapter:
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
    builtins_command: str = ''
    builtins: tuple[str, ...] = ()

    @property
    def syntax(self) -> ShellSyntax:
        return SYNTAXES[self.family]

    @property
    def refresh(self) -> bool:
        return self.refresh_script is not None

    def source(self, path, *args) -> str:
        syntax = self.syntax
        command = self.source_command or syntax.source_command
        if self.source_args is not None and args:
            # dash ignores dot arguments; BSD csh source accepts no arguments.
            if len(args) != len(self.source_args):
                raise ValueError(f'{self.name} integration needs {len(self.source_args)} arguments')
            assignments = [syntax.assign(name, syntax.quote(str(value)))
                           for name, value in zip(self.source_args, args)]
            return '; '.join(assignments) + '; ' + command + ' ' + syntax.quote(str(path)) + '\n'
        return command + ' ' + ' '.join(syntax.quote(str(v)) for v in (path, *args)) + '\n'

    def refresh_command(self, directory) -> str:
        if not self.refresh_script:
            return ''
        command = self.source(directory / self.refresh_script)
        if self.capture_refresh_status:
            command = self.syntax.assign('_ish_shell_exit_code', self.syntax.exit_status) + '; ' + command
        return command

    def configure_sequencer(self, sequencer, *, prompt_id, prompt, continuation, unhooked_prompt):
        sequencer.on_prefix(PROMPT_ID_PREFIX, prompt_id)
        sequencer.between_sequence(BEFORE_PROMPT, AFTER_PROMPT, prompt)
        sequencer.between_sequence(BEFORE_CONTINUATION, AFTER_CONTINUATION, continuation)
        if self.unhooked_prompt:
            sequencer.between_sequence(*self.unhooked_prompt, unhooked_prompt)


ADAPTERS = {
    'bash': ShellAdapter(
        'bash', 'posix', ('--noediting', '-i'), BASH_INTEGRATION_SCRIPT,
        source_command='source', builtins_command='compgen -b',
    ),
    'zsh': ShellAdapter(
        'zsh', 'posix', ('-d', '-i'), ZSH_INTEGRATION_SCRIPT,
        source_command='source', builtins_command='printf "%s\\n" ${(k)builtins}',
    ),
    'tcsh': ShellAdapter(
        'tcsh', 'csh', ('-i',), TCSH_INTEGRATION_SCRIPT, behavior=CSH_BEHAVIOR,
        unhooked_prompt=(CARET_BEFORE_PROMPT, CARET_AFTER_PROMPT), builtins_command='builtins',
    ),
    'csh': ShellAdapter(
        'csh', 'csh', ('-i',), CSH_INTEGRATION_SCRIPT, behavior=CSH_BEHAVIOR,
        source_args=INTEGRATION_ARGUMENTS,
        refresh_script=CSH_UPDATE_SCRIPT, capture_refresh_status=True,
        builtins=tuple(
            'alias bg break breaksw case cd chdir continue default dirs echo else end endif endsw '
            'eval exec exit fg foreach glob goto hashstat history if jobs kill limit login logout '
            'nice nohup notify onintr popd pushd rehash repeat set setenv shift source stop suspend '
            'switch time umask unalias unhash unlimit unset unsetenv wait while'.split()),
    ),
    'sh': ShellAdapter(
        'sh', 'posix', ('-i',), POSIX_INTEGRATION_SCRIPT,
        source_args=INTEGRATION_ARGUMENTS, refresh_script=POSIX_UPDATE_SCRIPT,
        builtins=tuple(
            'alias bg break cd command continue eval exec exit export false fc fg getopts hash jobs '
            'kill printf pwd read readonly return set shift test times trap true type ulimit umask '
            'unalias unset wait'.split()),
    ),
}

# Membership is declared only above; this view is useful for discovery/docs.
SHELL_CATEGORIES = {
    family: tuple(name for name, adapter in ADAPTERS.items() if adapter.family == family)
    for family in SYNTAXES
}
SHELL_ALIASES = {'bsd-csh': 'csh', 'dash': 'sh'}
# /bin/csh may be a tcsh symlink, which needs tcsh's integration hooks.
RESOLVED_SHELL_ALIASES = {('csh', 'tcsh'): 'tcsh'}


def get_adapter(shell: str, path: str | None = None) -> ShellAdapter:
    name = os.path.basename(shell)
    name = SHELL_ALIASES.get(name, name)
    if path:
        name = RESOLVED_SHELL_ALIASES.get((name, os.path.basename(os.path.realpath(path))), name)
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(f'Unsupported shell {shell!r}. Choose {", ".join(ADAPTERS)}.') from None
