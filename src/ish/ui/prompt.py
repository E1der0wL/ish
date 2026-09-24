"""Connect the prompt-toolkit editor to shell state, completion, and Python tools.

The editor owns input at the primary prompt. Suspend rendering while displaying shell
output, and prevent stale asynchronous PATH scans from overwriting newer state.
"""

from __future__ import annotations

import asyncio
import os
import pkgutil
import subprocess
import sys
import termios
import traceback
from collections import Counter
from contextlib import contextmanager
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from prompt_toolkit import PromptSession
from prompt_toolkit.application import in_terminal
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer, ValidationState
from prompt_toolkit.completion import (
    Completer,
    ThreadedCompleter,
    get_common_complete_suffix,
    merge_completers,
)
from prompt_toolkit.document import Document
from prompt_toolkit.filters import (
    Condition,
    has_arg,
    has_focus,
    is_done,
    is_true,
    renderer_height_is_known,
)
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import (
    BufferControl,
    FormattedTextControl,
    SearchBufferControl,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.layout.processors import (
    AfterInput,
    AppendAutoSuggestion,
    ConditionalProcessor,
    DisplayMultipleCursors,
    DynamicProcessor,
    HighlightIncrementalSearchProcessor,
    HighlightSelectionProcessor,
    PasswordProcessor,
    ReverseSearchProcessor,
    merge_processors,
)
from prompt_toolkit.lexers import DynamicLexer, PygmentsLexer
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.shortcuts.prompt import (
    CompleteStyle,
    _RPrompt,
    _split_multiline_prompt,
)
from prompt_toolkit.validation import ValidationError
from prompt_toolkit.widgets import Frame
from prompt_toolkit.widgets.toolbars import (
    SearchToolbar,
    SystemToolbar,
    ValidationToolbar,
)
from pygments.lexers import get_lexer_by_name

from ish.app.pytool import ProcessHandler
from ish.config import config
from ish.lang import i18n
from ish.log import get_logger
from ish.parser.completion import CompletionWord
from ish.parser.shell import alias_parser, dict_parser, str_parser
from ish.runtime.fdio import InputBytes
from ish.runtime.observer import InputObserver
from ish.shell.adapter import get_adapter
from ish.shell.base import InteractiveShell
from ish.shell.constants import ALIAS, BUILTIN, ENVIRON, EXITCODE, PWD
from ish.shell.context import ShellContext
from ish.shell.input import CANONICAL_LINE_BYTES, InputRejected
from ish.shell.request import ShellExitRequest, ShellPassRequest

from .ansi import ShellANSI
from .completer import PromptCompleter
from .input import (
    ObservedInput,
    feed_literal_text,
    feed_pending_keys,
    take_pending_keys,
)

if TYPE_CHECKING:
    from argparse import Namespace

    from prompt_toolkit.key_binding.key_bindings import KeyHandlerCallable
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.layout.containers import AnyContainer

    from ish.plugin.manager import PluginManager

__all__ = ["Prompt"]


def get_builtins(shell: str, executable: Optional[str] = None) -> List[str]:
    """Get builtins with startup files disabled where supported, or use a static list."""
    adapter = get_adapter(shell, executable)
    if not adapter.builtins_command:
        return list(adapter.builtins)
    try:
        env = os.environ.copy()
        # Bash reads BASH_ENV in noninteractive mode even with --norc.
        # Only the discovery child loses these hooks; the session keeps them.
        for name in ("BASH_ENV", "ENV"):
            env.pop(name, None)
        stdout, stderr = subprocess.Popen(
            [executable or shell, *adapter.builtins_args, adapter.builtins_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        ).communicate()
        return stdout.split()
    except Exception:
        return []


class Prompt(PromptSession):
    """Multiline editor and user extension interface connected to shell state.

    Prompt-toolkit handles primary-prompt editing and completion; InteractiveShell
    handles commands and continuation input. The caller retains ownership of supplied
    I/O descriptors.
    """

    def __init__(
        self,
        *args: Any,
        shell: Optional[str] = None,
        encoder: Optional[str] = sys.stdout.encoding or "utf-8",
        input_fd: Optional[int] = None,
        output_fd: Optional[int] = None,
        plugin_manager: Optional[PluginManager] = None,
        option: Optional[Namespace] = None,
        **kwargs,
    ) -> None:
        """Configure the shell runner, state parsers, Python worker, UI layout, and key
        bindings.
        """
        self.logger = get_logger()
        self.input_observer = InputObserver()

        self.shell: Optional[str] = kwargs.pop("shell", shell)
        self.encoder: Optional[str] = kwargs.pop("encoder", encoder)
        if input_fd is not None:
            input_fd = kwargs.pop("input_fd", input_fd)
            from prompt_toolkit.input import create_input

            kwargs.update(
                {
                    "input": create_input(
                        os.fdopen(
                            input_fd,
                            mode="r",
                            encoding=self.encoder,
                            errors="replace",
                            closefd=False,
                        )
                    )
                }
            )

        if output_fd is not None:
            output_fd = kwargs.pop("output_fd", output_fd)
            from prompt_toolkit.output import create_output

            kwargs.update(
                {
                    "output": create_output(
                        os.fdopen(
                            output_fd,
                            mode="w",
                            encoding=self.encoder,
                            errors="replace",
                            closefd=False,
                        )
                    )
                }
            )

        self.context = ShellContext()
        self.context.register_handler(
            ENVIRON,
            lambda data: dict_parser(
                data,
                encoder=self.encoder,
                x_sep="=",
                y_sep="\0" if b"\0" in data else "\n",
            ),
            lambda data: asyncio.create_task(self._update_context(ENVIRON, data)),
        )
        self.context.register_handler(
            ALIAS,
            lambda data: alias_parser(
                data, encoder=self.encoder, shell=self.interactive_shell.shell
            ),
            lambda data: asyncio.create_task(self._update_context(ALIAS, data)),
        )
        self.context.register_handler(
            EXITCODE, lambda data: str_parser(data, encoder=self.encoder)
        )
        self.last_exitcode: Optional[int] = None
        self.last_tool_exitcode: Optional[int] = None

        self.interactive_shell = InteractiveShell(
            self.shell or "bash",
            prompt=self,
            encoder=self.encoder,
            stdin=sys.stdin.fileno() if input_fd is None else input_fd,
            stdout=sys.stdout.fileno() if output_fd is None else output_fd,
            input_observer=self.input_observer,
        )
        self.shell = self.interactive_shell.shell

        self.process_handler: ProcessHandler = ProcessHandler(
            encoder=self.encoder,
            stdin=input_fd,
            stdout=output_fd,
            get_terminal_fd=lambda: self.interactive_shell.master_fd,
            input_observer=self.input_observer,
            take_input=self.take_typeahead,
            return_input=self.interactive_shell.return_typeahead,
            return_native_input=self.interactive_shell.return_native_input,
            observe_output=self.interactive_shell.terminal_state.feed,
        )
        self.interactive_shell.resize_callback = self.process_handler.resize

        self.pre_hook: Optional[Callable[..., Any]] = None
        self.post_hook: Optional[Callable[..., Any]] = None
        self.fallback_hook: Optional[Callable[..., Any]] = None

        self.floats: List[Float] = []

        self._usage_counter = Counter()
        self._completions = {
            BUILTIN: get_builtins(self.shell, self.interactive_shell.shell_path),
            ENVIRON: [],
            ALIAS: [],
        }
        self._completion_words = []
        self._cwd = ""
        self._path_scan_key = None
        self._path_scan_task = None
        self._path_generation = 0
        self.default_completer = PromptCompleter(
            words=lambda: self._completion_words,
            cwd=lambda: [self._cwd],
            ignore_case=True,
            match_middle=False,
            quote=self.quote_argument,
            context=self.completion_context,
        )
        self.exit_command: str = "ish_exit"
        self.internal_commands: Dict[
            str, Tuple[Callable[..., Awaitable[Any]], Tuple, Dict]
        ] = {
            "ish_reload": (self.load_rc_async, (), {}),
        }

        self.plugin_manager = kwargs.pop("plugin_manager", plugin_manager)
        self.option = kwargs.pop("option", option)
        self.internal_tools: Dict[str, Callable[..., Any]] = {}

        if "auto_suggest" not in kwargs:
            kwargs["auto_suggest"] = AutoSuggestFromHistory()
        super().__init__(*args, **kwargs)
        self.app.input = ObservedInput(self.app.input, self.input_observer)
        self.multiline = True
        self.color_depth = ColorDepth.TRUE_COLOR
        self.key_bindings = self._create_key_binding()
        self.lexer = PygmentsLexer(
            type(get_lexer_by_name(self.interactive_shell.adapter.syntax.lexer))
        )
        self.completer = ThreadedCompleter(self.default_completer)
        self.parser = Vt100Parser(feed_key_callback=self.app.key_processor.feed)

    @property
    def key_list(self) -> List[Dict[str, Any]]:
        """Build a user-facing list of registered keys, handlers, and filters."""
        bindings_info = []

        bindings = getattr(self, "key_bindings", None)
        if bindings is None:
            return bindings_info

        for binding in bindings.bindings:
            keys_str = " ".join(str(k) for k in binding.keys)
            handler_name = getattr(binding.handler, "__name__", str(binding.handler))
            bindings_info.append(
                {
                    "keys": keys_str,
                    "keys_tuple": binding.keys,
                    "handler": handler_name,
                    "handler_func": binding.handler,
                    "filter": binding.filter,
                    "eager": binding.eager,
                    "is_global": binding.is_global,
                }
            )
        return bindings_info

    @property
    def commands(self) -> Set[str]:
        """Return a set copy of the current command completion candidates."""
        return set(self._completion_words)

    def _create_layout(self) -> Layout:
        """
        Create `Layout` for this prompt.
        """
        dyncond = self._dyncond

        # Create functions that will dynamically split the prompt. (If we have
        # a multiline prompt.)
        (
            has_before_fragments,
            get_prompt_text_1,
            get_prompt_text_2,
        ) = _split_multiline_prompt(self._get_prompt)

        default_buffer = self.default_buffer
        search_buffer = self.search_buffer

        # Create processors list.
        @Condition
        def display_placeholder() -> bool:
            """Display the placeholder only when the input buffer is empty."""
            return self.placeholder is not None and self.default_buffer.text == ""

        all_input_processors = [
            HighlightIncrementalSearchProcessor(),
            HighlightSelectionProcessor(),
            ConditionalProcessor(
                AppendAutoSuggestion(), has_focus(default_buffer) & ~is_done
            ),
            ConditionalProcessor(PasswordProcessor(), dyncond("is_password")),
            DisplayMultipleCursors(),
            # Users can insert processors here.
            DynamicProcessor(lambda: merge_processors(self.input_processors or [])),
            ConditionalProcessor(
                AfterInput(lambda: self.placeholder),
                filter=display_placeholder,
            ),
        ]

        # Create bottom toolbars.
        bottom_toolbar = ConditionalContainer(
            Window(
                FormattedTextControl(
                    lambda: self.bottom_toolbar, style="class:bottom-toolbar.text"
                ),
                style="class:bottom-toolbar",
                dont_extend_height=True,
                height=Dimension(min=1),
            ),
            filter=Condition(lambda: self.bottom_toolbar is not None)
            & ~is_done
            & renderer_height_is_known,
        )

        search_toolbar = SearchToolbar(
            search_buffer, ignore_case=dyncond("search_ignore_case")
        )

        search_buffer_control = SearchBufferControl(
            buffer=search_buffer,
            input_processors=[ReverseSearchProcessor()],
            ignore_case=dyncond("search_ignore_case"),
        )

        system_toolbar = SystemToolbar(
            enable_global_bindings=dyncond("enable_system_prompt")
        )

        def get_search_buffer_control() -> SearchBufferControl:
            "Return the UIControl to be focused when searching start."
            if is_true(self.multiline):
                return search_toolbar.control
            else:
                return search_buffer_control

        default_buffer_control = BufferControl(
            buffer=default_buffer,
            search_buffer_control=get_search_buffer_control,
            input_processors=all_input_processors,
            include_default_input_processors=False,
            lexer=DynamicLexer(lambda: self.lexer),
            preview_search=True,
        )

        default_buffer_window = Window(
            default_buffer_control,
            height=self._get_default_buffer_control_height,
            get_line_prefix=partial(
                self._get_line_prefix, get_prompt_text_2=get_prompt_text_2
            ),
            wrap_lines=dyncond("wrap_lines"),
        )

        @Condition
        def multi_column_complete_style() -> bool:
            """Report whether the current completion menu uses multiple columns."""
            return self.complete_style == CompleteStyle.MULTI_COLUMN

        # Build the layout.

        # The main input, with completion menus floating on top of it.
        main_input_container = FloatContainer(
            HSplit(
                [
                    ConditionalContainer(
                        Window(
                            FormattedTextControl(get_prompt_text_1),
                            dont_extend_height=True,
                        ),
                        Condition(has_before_fragments),
                    ),
                    ConditionalContainer(
                        default_buffer_window,
                        Condition(
                            lambda: (
                                get_app().layout.current_control
                                != search_buffer_control
                            )
                        ),
                    ),
                    ConditionalContainer(
                        Window(search_buffer_control),
                        Condition(
                            lambda: (
                                get_app().layout.current_control
                                == search_buffer_control
                            )
                        ),
                    ),
                ]
            ),
            [
                # Completion menus.
                # NOTE: Especially the multi-column menu needs to be
                #       transparent, because the shape is not always
                #       rectangular due to the meta-text below the menu.
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=CompletionsMenu(
                        max_height=16,
                        scroll_offset=1,
                        extra_filter=has_focus(default_buffer)
                        & ~multi_column_complete_style,
                    ),
                ),
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=MultiColumnCompletionsMenu(
                        show_meta=True,
                        extra_filter=has_focus(default_buffer)
                        & multi_column_complete_style,
                    ),
                ),
                # The right prompt.
                Float(
                    right=0,
                    top=0,
                    hide_when_covering_content=True,
                    content=_RPrompt(lambda: self.rprompt),
                ),
            ]
            + self.floats,
        )

        layout = HSplit(
            [
                # Wrap the main input in a frame, if requested.
                ConditionalContainer(
                    Frame(main_input_container),
                    filter=dyncond("show_frame"),
                    alternative_content=main_input_container,
                ),
                ConditionalContainer(ValidationToolbar(), filter=~is_done),
                ConditionalContainer(
                    system_toolbar, dyncond("enable_system_prompt") & ~is_done
                ),
                # In multiline mode, we use two toolbars for 'arg' and 'search'.
                ConditionalContainer(
                    Window(FormattedTextControl(self._get_arg_text), height=1),
                    dyncond("multiline") & has_arg,
                ),
                ConditionalContainer(search_toolbar, dyncond("multiline") & ~is_done),
                bottom_toolbar,
            ]
        )

        return Layout(layout, default_buffer_window)

    def _create_default_buffer(self) -> Buffer:
        """Check transport admission on acceptance, including cached user validation.

        Keep the user's dynamic validator and validate-while-typing behavior.
        The transport check runs on explicit validate/accept calls only, so it
        performs no per-keystroke terminal queries and cannot be bypassed by a
        cached VALID state from the asynchronous user validator.
        """
        buffer = super()._create_default_buffer()
        original_validate = buffer.validate
        rejection = None

        def validate(set_cursor: bool = False) -> bool:
            """Leave rejected input, cursor, history, and the active editor untouched."""
            nonlocal rejection
            if rejection is not None and buffer.validation_error is rejection:
                buffer.validation_state = ValidationState.UNKNOWN
                buffer.validation_error = None
            try:
                self._validate_editor_submission(buffer.text)
            except InputRejected as exc:
                rejection = ValidationError(
                    message=str(exc), cursor_position=buffer.cursor_position
                )
                buffer.validation_error = rejection
                buffer.validation_state = ValidationState.INVALID
                self.input_observer.record("input_rejected", "EDITOR")
                return False
            return original_validate(set_cursor=set_cursor)

        buffer.validate = validate
        return buffer

    def _create_key_binding(self) -> KeyBindings:
        """Register editing keys for submission, indentation, paired characters, and
        completion.
        """

        def is_balanced(string, quotation: str) -> bool:
            """Use an editing heuristic to check whether paired characters occur an even
            number of times.
            """
            return string.count(quotation) % 2 == 0

        def get_indentation(line) -> int:
            """Return the number of leading spaces on a line."""
            return len(line) - len(line.lstrip(" "))

        def input_pair(event, key: str, insert: str) -> None:
            """Skip a matching quote or open a pair at buffer end; close unmatched quotes."""
            buffer = event.current_buffer
            document = buffer.document
            if document.current_char == key:
                buffer.cursor_right()
            elif document.is_cursor_at_the_end and is_balanced(buffer.text, key):
                buffer.insert_text(insert)
                buffer.cursor_left()
            else:
                buffer.insert_text(key)

        def input_set(event, key: str, insert: str) -> None:
            """Open a bracket pair at buffer end, or insert only the typed character."""
            buffer = event.current_buffer
            if buffer.document.is_cursor_at_the_end:
                buffer.insert_text(insert)
                buffer.cursor_left()
            else:
                buffer.insert_text(key)

        kb = KeyBindings()

        @kb.add(Keys.Enter)
        def _(event: KeyPressEvent):
            """On Enter, insert a newline for explicit line continuation or submit the
            input.
            """
            buffer = event.current_buffer
            text = buffer.text
            lines = text.split("\n")
            cursor_position = buffer.document.cursor_position
            if (len(text) - len(text.rstrip("\\"))) % 2:
                if cursor_position > 0:
                    left_char = buffer.document.text[cursor_position - 1]
                    if left_char == ":":
                        indentation = get_indentation(lines[-1])
                        buffer.insert_text("\n" + " " * (indentation + 4))
                    else:
                        if lines:
                            indentation = get_indentation(lines[-1])
                            buffer.insert_text("\n" + " " * indentation)
                        else:
                            event.current_buffer.insert_text("\n")

                else:
                    event.current_buffer.insert_text("\n")

            else:
                event.app.current_buffer.validate_and_handle()

        @kb.add(Keys.Backspace)
        def _(event: KeyPressEvent):
            """On Backspace, remove an empty character pair or the preceding character."""
            buffer = event.current_buffer
            cursor_position = buffer.document.cursor_position
            char_before_cursor = buffer.document.char_before_cursor
            try:
                if char_before_cursor == "(" and buffer.text[cursor_position] == ")":
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                elif char_before_cursor == "{" and buffer.text[cursor_position] == "}":
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                elif char_before_cursor == "[" and buffer.text[cursor_position] == "]":
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                elif char_before_cursor == '"' and buffer.text[cursor_position] == '"':
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                elif char_before_cursor == "'" and buffer.text[cursor_position] == "'":
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                elif char_before_cursor == "`" and buffer.text[cursor_position] == "`":
                    event.current_buffer.cursor_right()
                    event.current_buffer.delete_before_cursor(2)
                else:
                    event.current_buffer.delete_before_cursor(1)

            except Exception:
                event.current_buffer.delete_before_cursor(1)

        @kb.add(Keys.Tab)
        def _(event: KeyPressEvent):
            """Use Tab to start asynchronous completion or apply the current candidate."""
            buffer = event.app.current_buffer
            complete_state = buffer.complete_state
            if not complete_state:
                buffer.start_completion(select_first=False)
            if complete_state:
                completions = complete_state.completions
                completion = complete_state.current_completion
                if completions:
                    if completion:
                        completion = complete_state.current_completion
                        buffer.apply_completion(completion)
                        buffer.cancel_completion()
                        self._usage_counter[completion.text.strip()] += 1
                    else:
                        extra_text = get_common_complete_suffix(
                            complete_state.original_document, completions
                        )
                        if extra_text:
                            buffer.insert_text(extra_text, move_cursor=True)
                        else:
                            buffer.complete_next()
                            completion = buffer.complete_state.current_completion
                            buffer.apply_completion(completion)
                            buffer.cancel_completion()
                            self._usage_counter[completion.text.strip()] += 1

        @kb.add(Keys.ControlSpace)
        def _(event: KeyPressEvent):
            """On Control-Space, insert a newline using current indentation and colon
            context.
            """
            buffer = event.current_buffer
            code = buffer.text
            lines = code.split("\n")
            cursor_position = buffer.document.cursor_position
            if cursor_position > 0:
                left_char = buffer.document.text[cursor_position - 1]
                if left_char == ":":
                    indentation = get_indentation(lines[-1])
                    buffer.insert_text("\n" + " " * (indentation + 4))
                else:
                    if lines:
                        indentation = get_indentation(lines[-1])
                        buffer.insert_text("\n" + " " * indentation)
                    else:
                        event.current_buffer.insert_text("\n")
            else:
                event.current_buffer.insert_text("\n")

        @kb.add("(")
        def _(event: KeyPressEvent):
            """Insert an opening parenthesis, pairing it only at the end of the input."""
            input_set(event, "(", "()")

        @kb.add("{")
        def _(event: KeyPressEvent):
            """Insert an opening brace, pairing it only at the end of the input."""
            input_set(event, "{", "{}")

        @kb.add("[")
        def _(event: KeyPressEvent):
            """Insert an opening bracket, pairing it only at the end of the input."""
            input_set(event, "[", "[]")

        @kb.add('"')
        def _(event: KeyPressEvent):
            """Apply paired-quote editing when a double quote is typed."""
            input_pair(event, '"', '""')

        @kb.add("'")
        def _(event: KeyPressEvent):
            """Apply paired-quote editing when a single quote is typed."""
            input_pair(event, "'", "''")

        @kb.add("`")
        def _(event: KeyPressEvent):
            """Apply paired-quote editing when a backtick is typed."""
            input_pair(event, "`", "``")

        return kb

    @staticmethod
    def _scan_path(directories: Iterable) -> Set[str]:
        """Collect executable filenames from PATH directories, skipping access errors."""
        all_cmd: Set[str] = set()
        for directory in directories:
            if not directory:
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_file() and os.access(entry.path, os.X_OK):
                            all_cmd.add(entry.name)
            except (FileNotFoundError, PermissionError):
                continue
        return all_cmd

    def _tool_argv(self, command: str) -> list[str] | None:
        """Resolve a registered tool only through the active shell's literal policy."""
        if not self.internal_tools:
            return None
        argv = self.interactive_shell.adapter.parsing.tool_argv(command)
        return argv if argv and argv[0] in self.internal_tools else None

    def _validate_editor_submission(self, command: str) -> None:
        """Apply shell transport limits without restricting directly dispatched tools."""
        try:
            data = (command + "\n").encode(self.encoder)
        except UnicodeEncodeError as exc:
            raise InputRejected(
                "Not sent: input cannot be encoded for this shell."
            ) from exc
        if len(data) > CANONICAL_LINE_BYTES:
            key = command.strip()
            if key == self.exit_command or key in self.internal_commands:
                return
            if self._tool_argv(command) is not None:
                return
        self.interactive_shell.validate_submission(data)

    def _update_layout(self) -> None:
        """Rebuild the layout after float or tool changes and request a redraw."""
        if hasattr(self, "app") and self.app is not None:
            self.app.layout = self._create_layout()
            self.app.invalidate()

    def _rebuild_completer(self) -> None:
        """Sort builtin, PATH, and alias candidates by usage frequency and length."""
        combined = set()
        for values in self._completions.values():
            combined.update(values)
        sorted_list = sorted(
            combined, key=lambda x: (-self._usage_counter[x], len(x), x)
        )
        self._completion_words[:] = sorted_list

    async def _update_context(self, category: str, data: Any) -> None:
        """Update completion candidates asynchronously after shell environment or alias
        changes.

        Share identical PATH scans and apply completed results only when their
        generation is current.
        """
        if ENVIRON == category:
            self._cwd = data.get(PWD)
            self._path_generation += 1
            generation = self._path_generation
            paths = tuple(
                os.path.normpath(os.path.join(self._cwd or os.getcwd(), path))
                for path in os.get_exec_path(data)
            )
            if self._path_scan_key != paths or self._path_scan_task.done():
                self._path_scan_key = paths
                self._path_scan_task = asyncio.create_task(
                    asyncio.to_thread(self._scan_path, paths)
                )
            execs = await asyncio.shield(self._path_scan_task)
            if (
                generation == self._path_generation
                and set(self._completions[category]) != execs
            ):
                self._completions[category][:] = execs
                self._rebuild_completer()

        elif ALIAS == category:
            aliases = list(data.keys())
            if self._completions[category] != aliases:
                self._completions[category][:] = aliases
                self._rebuild_completer()

    def cmd(self, cmd: str) -> None:
        """Exit the current editor and return the specified command as its submission."""
        self.app.exit(result=cmd)

    async def post_exec(self) -> None:
        """Run the registered Python hook after command execution and log failures."""
        if self.post_hook is None:
            return

        try:
            await self.process_handler.run(self.post_hook)
        except Exception:
            self.logger.exception("post hook failed")
            return

    async def pre_exec(self) -> None:
        """Run the registered Python hook before sending a command to the shell."""
        if self.pre_hook is None:
            return

        try:
            await self.process_handler.run(self.pre_hook)
        except Exception:
            self.logger.exception("pre hook failed")
            return

    async def fallback(self) -> None:
        """Run the fallback hook only when the last shell exit status indicates failure."""
        if (
            not self.interactive_shell.last_command.strip()
            or self.fallback_hook is None
        ):
            return

        try:
            status = getattr(self.context, EXITCODE, None)
            if status is not None and str(status).strip() != "0":
                await self.process_handler.run(self.fallback_hook)
        except Exception:
            self.logger.exception("fallback hook failed")
            return

    def set_tool(
        self,
        cmd: str,
        plugin_name: Optional[str] = None,
        function_name: Optional[str] = None,
        function: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Set an internal tool, or remove it when no function source is supplied.

        Supply either function or both plugin_name and function_name. Setting an
        existing command replaces its callable. Invalid sources raise an error
        without replacing the current tool; removing an unknown command is a no-op.
        """
        if plugin_name is not None or function_name is not None:
            if not plugin_name or not function_name or function is not None:
                raise ValueError(
                    "Supply either function or both plugin_name and function_name"
                )
            if self.plugin_manager is None:
                raise ValueError("No plugin manager is configured")
            plugin = self.plugin_manager.get(plugin_name)
            if plugin is None:
                raise ValueError(f"Plugin is not loaded: {plugin_name}")
            function = getattr(plugin, function_name, None)
        elif function is None:
            self.internal_tools.pop(cmd, None)
            return

        if not callable(function):
            raise TypeError("The internal tool must be callable")
        self.internal_tools[cmd] = function

    def set_key(
        self,
        *keys: Union[Keys, str, KeyHandlerCallable],
        handler: Optional[KeyHandlerCallable] = None,
        **kwargs,
    ) -> None:
        """Replace bindings for an exact key sequence, or remove them with handler=None.

        Forward registration options to KeyBindings.add. Replacement removes all
        bindings for that sequence, including conditional variants. Pass a single
        callable positionally to remove every binding using that handler. Removing
        an absent binding is a no-op; invalid replacements preserve existing bindings.
        """
        if not keys:
            raise ValueError("Supply a key sequence or a handler to remove")
        if handler is None and kwargs:
            raise ValueError("Binding options require a handler")
        by_handler = callable(keys[0])
        if by_handler:
            if len(keys) != 1 or handler is not None:
                raise TypeError("A positional handler can only be used for removal")
        else:
            if not all(isinstance(key, (Keys, str)) for key in keys):
                raise TypeError("Keys must be strings or Keys values")
            # Normalize aliases through the public API, even for disabled bindings.
            candidate = KeyBindings()
            candidate.add(*keys)(lambda event: None)
            keys = candidate.bindings[0].keys
            # Validate replacement options before removing the current binding.
            if handler is not None:
                if not callable(handler):
                    raise TypeError("The key handler must be callable")
                candidate.add(*keys, **kwargs)(handler)

        # In 3.0.53, remove can skip adjacent matches and raises UnboundLocalError
        # for absent key sequences. Remove only known matches through its public API.
        while any(
            binding.handler == keys[0] if by_handler else binding.keys == keys
            for binding in self.key_bindings.bindings
        ):
            self.key_bindings.remove(*keys)
        if handler is not None:
            self.key_bindings.add(*keys, **kwargs)(handler)

    def set_float(
        self,
        content: Optional[AnyContainer] = None,
        *,
        target_float: Optional[Float] = None,
        **kwargs,
    ) -> Optional[Float]:
        """Create or replace a float, or remove floats when content is None.

        Return the new Float handle for later replacement or removal. Supplying
        target_float replaces its list position with a newly configured Float;
        omitted options use Float defaults. A missing replacement target raises
        ValueError. With content=None, remove the target (missing targets are no-ops),
        or clear all custom floats when no target is supplied. Removal returns None.
        """
        if content is None:
            if kwargs:
                raise ValueError("Float options require content")
            if target_float is None:
                if not self.floats:
                    return None
                self.floats.clear()
            elif target_float in self.floats:
                self.floats.remove(target_float)
            else:
                return None
            self._update_layout()
            return None

        index = self.floats.index(target_float) if target_float is not None else None
        new_float = Float(content=content, **kwargs)
        if index is None:
            self.floats.append(new_float)
        else:
            self.floats[index] = new_float
        self._update_layout()
        return new_float

    def completion_context(self, document: Document) -> CompletionWord | None:
        """Analyze a replaceable prefix for built-in or user completion providers.

        This pure policy runs in the completion worker without querying the shell.
        A dynamic context needs native interpretation; literal providers leave it alone.
        """
        return self.interactive_shell.adapter.parsing.completion(
            document.text, document.cursor_position
        )

    def quote_argument(self, value: str) -> str:
        """Encode a whole literal word; do not apply this inside existing quotes."""
        return self.interactive_shell.adapter.syntax.quote(value)

    def set_completer(
        self, completer: Optional[Union[Completer, Iterable[Completer]]] = None
    ) -> None:
        """Replace additional completion sources with one completer or an iterable.

        None or an empty iterable restores default completion. Validate all sources
        before replacing the current completer. Additional sources precede the
        default source, keeping their first occurrence of duplicate suggestions.
        Run completion in a worker thread.
        """
        if completer is None:
            additional = []
        elif isinstance(completer, Completer):
            additional = [completer]
        else:
            try:
                additional = list(completer)
            except TypeError as exc:
                raise TypeError(
                    "Supply a Completer instance, an iterable of Completer instances, or None"
                ) from exc
            if not all(isinstance(item, Completer) for item in additional):
                raise TypeError("Each completion source must be a Completer instance")

        comps = merge_completers(
            [*additional, self.default_completer], deduplicate=True
        )
        self.completer = ThreadedCompleter(comps)

    def set_prompt(self, prompt: bytes, encoder: Optional[str] = None) -> None:
        """Decode shell prompt bytes into an ANSI value without executing screen controls."""
        cleaned_prompt = prompt.replace(b"\r\n", b"\n")
        self.message = ShellANSI(
            cleaned_prompt.decode(encoder or self.encoder, errors="replace")
        )

    async def write_output(self, write) -> None:
        # Reader callbacks run outside prompt_async's context. Select this app
        # explicitly so the renderer suspends and redraws around background output.
        """Suspend the renderer in the current app context, write external output, and
        redraw.
        """
        with set_app(self.app):
            async with in_terminal():
                await write()

    def update_context(self, category: str, data: Any) -> None:
        """Pass received raw shell state to the category-specific parsers."""
        self.context.update(category, data)
        if category == EXITCODE:
            try:
                self.last_exitcode = int(self.context.exitcode)
            except (TypeError, ValueError):
                pass

    def load_rc(self) -> None:
        """Execute the user's .ishrc.py with prompt, config, and plugin objects.

        The user script runs in the current process; log loading failures and exit.
        """
        try:
            import ish

            for loader, module_name, _is_pkg in pkgutil.walk_packages(
                ish.__path__, ish.__name__ + "."
            ):
                try:
                    module = loader.find_module(module_name).load_module(module_name)
                    sys.modules[module_name] = module
                except Exception:
                    continue

            init_globals = {
                "__name__": "__main__",
                "__file__": str(config.RC_FILE),
                "prompt": self,
                "logger": self.logger,
                "config": config,
                "option": self.option,
                "plugin": self.plugin_manager,
            }
            import runpy

            runpy.run_path(
                str(config.RC_FILE), run_name="__main__", init_globals=init_globals
            )

        except SystemExit:
            pass

        except Exception:
            self.logger.error(i18n.get("error", error=traceback.format_exc()))
            sys.exit(1)

    async def load_rc_async(self) -> None:
        """Run the user initialization script in a separate thread."""
        await asyncio.to_thread(self.load_rc)

    async def get(self, **kwargs) -> Optional[str]:
        """Read edited input and dispatch exit requests, internal commands, or Python
        tools.

        Preserve trailing whitespace in shell commands and leave compound syntax
        requiring expansion to the shell.
        """
        try:
            command = await self.prompt_async(**kwargs)
            if command is None:
                return ""
        except (termios.error, OSError):
            return ""

        key = command.strip()
        if key == self.exit_command:
            raise ShellExitRequest

        if key in self.internal_commands:
            handler, args, kwargs = self.internal_commands[key]
            raise ShellPassRequest(handler, command, *args, **kwargs)

        argv = self._tool_argv(command)
        if argv is not None:
            self.last_tool_exitcode = None
            try:
                cwd, environ = self.interactive_shell.tool_context()
            except Exception:
                self.logger.exception("Python tool context unavailable: %s", argv[0])
                status = 1
            else:
                # Transport failures and application cancellation must still reach
                # session cleanup; they are not ordinary tool exit statuses.
                status = await self.process_handler.run_in_context(
                    self.internal_tools[argv[0]], cwd, environ, *argv[1:]
                )
            self.last_tool_exitcode = status
            # Retain the raw worker status as well as a shell-style display code.
            self.last_exitcode = 128 - status if status < 0 else status
            return None

        return command

    def take_typeahead(self) -> bytes:
        """Transfer unconsumed editor input to a native consumer exactly once.

        Prompt-toolkit stores keys after the accepted Enter for its next run.
        A tool, an ongoing submitted block, or a shell without prompt hooks must
        receive those keys instead.
        Cursor-position replies are terminal protocol, not user input.
        """
        data, native_prefix = self.interactive_shell.take_pending_input()
        data += take_pending_keys(self.input, self.encoder)
        self.input_observer.record("editor_input_transferred", bytes=len(data))
        return InputBytes(data, native_prefix)

    def feed_typeahead(self, data: bytes) -> None:
        """Feed returned bytes through the editor's existing decoder and VT100 parser.

        Sharing these objects keeps a UTF-8 character or escape sequence split
        across the handoff joined to its later bytes from the terminal.
        """
        feed_pending_keys(
            self.input, data, self.encoder, self.parser, self.app.key_processor.feed
        )

    def feed_literal_input(self, data: bytes) -> None:
        """Restore native text without applying editing bindings a second time.

        Share the normal decoder so a split UTF-8 character can be completed by
        future terminal input, with CPR replies still excluded from that prefix.
        """
        feed_literal_text(
            self.input, data, self.encoder, self.default_buffer.insert_text
        )

    @property
    def output_active(self) -> bool:
        """Report whether shell output must suspend the editor's rendering."""
        return self.app.is_running

    def process_typeahead(self, *, invalidate: bool = False) -> bool:
        """Apply queued keys and report acceptance before another line is replayed."""
        self.app.key_processor.process_keys()
        if invalidate:
            self.app.invalidate()
        return self.app.is_done

    @contextmanager
    def observe_resize(self, callback):
        """Notify the shell before the editor's resize callback and restore on exit."""
        previous = self.app._on_resize

        def on_resize():
            callback()
            previous()

        self.app._on_resize = on_resize
        try:
            yield
        finally:
            self.app._on_resize = previous

    def run(self) -> None:
        """Call the connected shell runner's synchronous entry point."""
        self.interactive_shell.run()
