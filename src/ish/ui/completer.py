"""Path and command completion preserving shell quoting and cursor context."""

from __future__ import annotations

import os
import pwd
import shlex
from posixpath import sep as _PATH_SEPARATOR
from typing import Callable, Iterable, Mapping, Pattern, Sequence, Union

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import AnyFormattedText

from ish.parser.completion import CompletionWord, completion_context
from ish.parser.lexical import TILDE

__all__ = ["PathCompleter", "PromptCompleter"]

_ARGUMENT_END = " "


def _default_context(document: Document) -> CompletionWord | None:
    """Retain POSIX completion for independently constructed completers."""
    return completion_context(document.text, document.cursor_position)


class PathCompleter(Completer):
    """Complete filesystem paths as single quoted arguments without dynamic shell
    expansion.
    """

    def __init__(
        self,
        only_directories: bool = False,
        get_paths: Callable[[], list[str]] | None = None,
        file_filter: Callable[[str], bool] | None = None,
        expanduser: bool = False,
        quote: Callable[[str], str] = shlex.quote,
        context: Callable[[Document], CompletionWord | None] = _default_context,
        get_home: Callable[[], str | None] | None = None,
    ) -> None:
        """Configure the search-path provider, file filter, and shell quoting function."""
        self.only_directories = only_directories
        self.get_paths = get_paths or (lambda: [os.curdir])
        self.file_filter = file_filter or (lambda _: True)
        self.expanduser = expanduser
        self.quote = quote
        self.context = context
        self.get_home = get_home

    def _home_directory(self, tilde: str) -> str:
        """Use the shell's HOME for bare tilde and passwd data for named users."""
        if tilde != TILDE or self.get_home is None:
            return os.path.expanduser(tilde)
        home = self.get_home()
        if home is not None:
            return home
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            return tilde

    def get_completions(self, document, complete_event):
        """Generate directory and file candidates matching the literal path before the
        cursor.
        """
        word = self.context(document)
        if word is not None:
            yield from self.complete(document, word)

    def complete(self, document: Document, word: CompletionWord):
        """Complete paths using an already analyzed context shared by the parent."""
        if word.dynamic:
            return
        raw_word = document.text_before_cursor[word.start :]
        text = word.value
        home_prefix = ""
        if self.expanduser and raw_word.startswith(TILDE):
            tilde, separator, relative = word.value.partition(_PATH_SEPARATOR)
            # Quoted or escaped tildes/usernames remain literal filesystem names.
            if raw_word.partition(_PATH_SEPARATOR)[0] == tilde:
                home = self._home_directory(tilde)
                if home != tilde:
                    if not separator:
                        directory = home or _PATH_SEPARATOR
                        roots = [""] if os.path.isabs(directory) else self.get_paths()
                        for root in roots:
                            path = os.path.join(root, directory)
                            if os.path.isdir(path) and self.file_filter(path):
                                yield Completion(
                                    text=tilde + _PATH_SEPARATOR,
                                    start_position=word.start
                                    - document.cursor_position,
                                )
                                break
                        return
                    home_prefix = tilde + _PATH_SEPARATOR
                    text = home.rstrip(_PATH_SEPARATOR) + _PATH_SEPARATOR + relative
        dirname, prefix = os.path.split(text)
        input_dirname = os.path.dirname(word.value)
        try:
            matches = {}
            roots = [""] if os.path.isabs(dirname) else self.get_paths()
            for root in roots:
                directory = os.path.join(root or os.curdir, dirname)
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if not entry.name.startswith(prefix):
                                continue
                            try:
                                if not self.file_filter(entry.path):
                                    continue
                                is_dir = entry.is_dir()
                            except OSError:
                                continue
                            if is_dir or not self.only_directories:
                                matches[entry.name] = is_dir
                except OSError:
                    continue
            for name, is_dir in sorted(matches.items()):
                suffix = _PATH_SEPARATOR if is_dir else ""
                path = os.path.join(input_dirname, name) + suffix
                # Keep the tilde unquoted so the shell expands it when submitted.
                # Only the remaining path needs the shell's literal quoting.
                try:
                    quoted = (
                        home_prefix + self.quote(path[len(home_prefix) :])
                        if home_prefix
                        else self.quote(path)
                    )
                except ValueError:
                    # Some shell syntaxes cannot represent a filename containing LF.
                    continue
                yield Completion(
                    text=quoted + ("" if is_dir else _ARGUMENT_END),
                    start_position=word.start - document.cursor_position,
                    display=name + suffix,
                )
        except (TypeError, OSError):
            return


class PromptCompleter(Completer):
    """Combine PATH, alias, builtin, and file path candidates according to context."""

    def __init__(
        self,
        words: Union[Sequence[str], Callable[[], Sequence[str]]],
        cwd: Callable[[], list[str]] | None = None,
        ignore_case: bool = False,
        display_dict: Union[Mapping[str, AnyFormattedText], None] = None,
        meta_dict: Union[Mapping[str, AnyFormattedText], None] = None,
        match_middle: bool = False,
        pattern: Union[Pattern[str], None] = None,
        quote: Callable[[str], str] = shlex.quote,
        context: Callable[[Document], CompletionWord | None] = _default_context,
        get_home: Callable[[], str | None] | None = None,
    ) -> None:
        """Prepare the command provider, display metadata, case policy, and path completer."""
        self.words = words
        self.ignore_case = ignore_case
        self.display_dict = display_dict or {}
        self.meta_dict = meta_dict or {}
        self.match_middle = match_middle
        self.pattern = pattern
        self.quote = quote
        self.context = context
        self.path_completer = PathCompleter(
            expanduser=True,
            get_paths=cwd,
            quote=quote,
            context=context,
            get_home=get_home,
        )

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        """Provide path candidates and add command names only at command positions."""
        word = self.context(document)
        if word is None:
            return
        command_position = word.command and not word.dynamic
        words = self.words
        if command_position and callable(words):
            words = words()
        yield from self.path_completer.complete(document, word)
        if not command_position:
            return
        prefix = word.value.casefold() if self.ignore_case else word.value
        for command in words:
            candidate = command.casefold() if self.ignore_case else command
            matches = (
                prefix in candidate
                if self.match_middle
                else candidate.startswith(prefix)
            )
            if matches:
                try:
                    quoted = self.quote(command)
                except ValueError:
                    continue
                yield Completion(
                    text=quoted + _ARGUMENT_END,
                    start_position=word.start - document.cursor_position,
                    display=self.display_dict.get(command, command),
                    display_meta=self.meta_dict.get(command, ""),
                )
