from __future__ import annotations

import asyncio
import os
import pkgutil
import subprocess
import sys
import termios
import traceback
from collections import Counter
from functools import partial
from typing import (
	TYPE_CHECKING,
	Any, Union, Optional, Iterable, Dict, List, Tuple,
	Set, Callable, Awaitable
)

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import ThreadedCompleter, merge_completers
from prompt_toolkit.filters import (
	Condition,
	has_arg,
	has_focus,
	is_done,
	is_true,
	renderer_height_is_known,
)
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
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
from prompt_toolkit.lexers import PygmentsLexer, DynamicLexer
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.shortcuts.prompt import _split_multiline_prompt, _RPrompt, CompleteStyle
from prompt_toolkit.widgets import Frame
from prompt_toolkit.widgets.toolbars import (
	SearchToolbar,
	SystemToolbar,
	ValidationToolbar,
)

from pygments.lexers import get_lexer_by_name

from ish.app.pytool import ProcessHandler
from ish.config import config
from ish.log import get_logger
from ish.lang import i18n
from ish.shell.constants import ALIAS, BUILTIN, ENVIRON, EXITCODE, PWD
from ish.shell.context import ShellContext
from ish.shell.signal import ShellExitRequest, ShellPassRequest
from ish.shell.base import InteractiveShell
from ish.shell.adapters import get_adapter
from ish.parser.shell import alias_parser, dict_parser, str_parser, simple_command

from .completer import PromptCompleter
from .ansi import ShellANSI

if TYPE_CHECKING:
	from argparse import Namespace
	from prompt_toolkit.completion import Completer
	from prompt_toolkit.key_binding.key_processor import KeyPressEvent
	from prompt_toolkit.key_binding.key_bindings import KeyHandlerCallable
	from prompt_toolkit.layout.containers import AnyContainer
	from ish.plugin.manager import PluginManager

__all__ = ['Prompt']


def get_builtins(shell: str, executable: Optional[str] = None) -> List[str]:
	adapter = get_adapter(shell, executable)
	if not adapter.builtins_command:
		return list(adapter.builtins)
	try:
		stdout, stderr = subprocess.Popen(
			adapter.builtins_command,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			shell=True,
			text=True,
			executable=executable or shell,
		).communicate()
		return stdout.split()
	except Exception:
		return []


class Prompt(PromptSession):
	def __init__(
		self,
		*args: Any,
		shell: Optional[str] = None,
		encoder: Optional[int] = sys.stdout.encoding or 'utf-8',
		input_fd: Optional[int] = None,
		output_fd: Optional[int] = None,
		plugin_manager: Optional[PluginManager] = None,
		option: Optional[Namespace] = None,
		**kwargs
	) -> None:
		self.logger = get_logger()

		self.shell: Optional[str] = kwargs.pop('shell', shell)
		self.encoder: Optional[str] = kwargs.pop('encoder', encoder)
		if input_fd is not None:
			input_fd = kwargs.pop('input_fd', input_fd)
			from prompt_toolkit.input import create_input
			kwargs.update(
				{'input': create_input(os.fdopen(input_fd, mode='r', encoding=self.encoder, errors='replace', closefd=False))}
			)

		if output_fd is not None:
			output_fd = kwargs.pop('output_fd', output_fd)
			from prompt_toolkit.output import create_output
			kwargs.update(
				{'output': create_output(os.fdopen(output_fd, mode='w', encoding=self.encoder, errors='replace', closefd=False))}
			)

		self.context = ShellContext()
		self.context.register_handler(
			ENVIRON,
			lambda data: dict_parser(data, encoder=self.encoder, x_sep="=",
			y_sep="\0" if b"\0" in data else "\n"),
			lambda data: (asyncio.create_task(self._update_context(ENVIRON, data)))
		)
		self.context.register_handler(
			ALIAS,
			lambda data: alias_parser(data, encoder=self.encoder, shell=self.interactive_shell.shell),
			lambda data: (asyncio.create_task(self._update_context(ALIAS, data)))
		)
		self.context.register_handler(
			EXITCODE,
			lambda data: str_parser(data, encoder=self.encoder)
		)

		self.interactive_shell = InteractiveShell(
			self.shell or 'bash', prompt=self, encoder=self.encoder,
			stdin=sys.stdin.fileno() if input_fd is None else input_fd,
			stdout=sys.stdout.fileno() if output_fd is None else output_fd,
		)
		self.shell = self.interactive_shell.shell

		self.process_handler: ProcessHandler = ProcessHandler(
			encoder=self.encoder, stdin=input_fd, stdout=output_fd)

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
		self.default_completer = PromptCompleter(
			words=lambda: self._completion_words,
			cwd=lambda: [self._cwd],
			ignore_case=True,
			match_middle=False,
			quote=self.interactive_shell.adapter.syntax.quote,
		)
		self.exit_command: str = "ish_exit"
		self.internal_commands: Dict[str, Tuple[Callable[..., Awaitable[Any]], Tuple, Dict]] = {
		'ish_reload': (self.load_rc_async, (), {}),
		}

		self.plugin_manager = kwargs.pop('plugin_manager', plugin_manager)
		self.option = kwargs.pop('option', option)
		self.internal_tools: Dict[str, Callable[..., Any]] = {}

		super().__init__(*args, **kwargs)
		self.multiline = True
		self.color_depth = ColorDepth.TRUE_COLOR
		self.key_bindings = self._create_key_binding()
		self.auto_suggest = AutoSuggestFromHistory()
		self.lexer = PygmentsLexer(type(get_lexer_by_name(self.interactive_shell.adapter.syntax.lexer)))
		self.completer = ThreadedCompleter(self.default_completer)
		self.parser = Vt100Parser(feed_key_callback=self.app.key_processor.feed)

	@property
	def key_list(self) -> List[Dict[str, Any]]:
		bindings_info = []

		bindings = getattr(self, 'key_bindings', None)
		if bindings is None:
			return bindings_info

		for binding in bindings.bindings:
			keys_str = " ".join(str(k) for k in binding.keys)
			handler_name = getattr(binding.hander, '__name__', str(binding.handler))
			bindings_info.append({
				'keys': keys_str,
				'keys_tuple': binding.keys,
				'handler': handler_name,
				'handler_func': binding.handler,
				'filter': binding.filter,
				'eager': binding.eager,
				'is_global': binding.is_global,
			})
		return bindings_info

	@property
	def commands(self) -> Set[str]:
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
			] + self.floats,
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

	def _create_key_binding(self) -> KeyBindings:
		def is_balanced(string, quotation: str) -> bool:
			return string.count(quotation) % 2 == 0

		def get_indentation(line) -> int:
			return len(line) - len(line.lstrip(' '))

		def input_pair(event, key: str, insert: str) -> None:
			try:
				buffer = event.current_buffer
				cursor_position = buffer.document.cursor_position
				if buffer.text and buffer.text[cursor_position] == key:
					buffer.delete(count=1)
					buffer.insert_text(key, move_cursor=True)
				else:
					event.current_buffer.insert_text(insert, move_cursor=True)
					event.current_buffer.cursor_left()
			except:
				if is_balanced(event.current_buffer.text, key):
					event.current_buffer.insert_text(insert, move_cursor=True)
					event.current_buffer.cursor_left()
				else:
					event.current_buffer.insert_text(key, move_cursor=True)

		def input_set(event, key: str, set_key: str, insert: str) -> None:
			try:
				buffer = event.current_buffer
				cursor_position = buffer.document.cursor_position
				after_cursor = buffer.text[cursor_position]
				if after_cursor != set_key:
					event.current_buffer.insert_text(insert, move_cursor=True)
					event.current_buffer.cursor_left()
				else:
					event.current_buffer.insert_text(insert, move_cursor=True)
			except:
				event.current_buffer.insert_text(insert, move_cursor=True)
				event.current_buffer.cursor_left()

		def get_common_prefix(completions):
			if not completions:
				return ""
			texts = [str(c.display.__pt_formatted_text__()[0][1]) for c in completions]
			return os.path.commonprefix(texts)

		kb = KeyBindings()

		@kb.add(Keys.Enter)
		def _(event: KeyPressEvent):
			buffer = event.current_buffer
			text = buffer.text
			lines = text.split('\n')
			cursor_position = buffer.document.cursor_position
			if text.strip().endswith('\\'):
				if cursor_position > 0:
					left_char = buffer.document.text[cursor_position - 1]
					if left_char == ":":
						indentation = get_indentation(lines[-1])
						buffer.insert_text('\n' + ' ' * (indentation + 4))
					else:
						if lines:
							indentation = get_indentation(lines[-1])
							buffer.insert_text('\n' + ' ' * indentation)
						else:
							event.current_buffer.insert_text('\n')

				else:
					event.current_buffer.insert_text('\n')

			else:
				event.app.current_buffer.validate_and_handle()

		@kb.add(Keys.Backspace)
		def _(event: KeyPressEvent):
			buffer = event.current_buffer
			cursor_position = buffer.document.cursor_position
			char_before_cursor = buffer.document.char_before_cursor
			try:
				if char_before_cursor == '(' and buffer.text[cursor_position] == ')':
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				elif char_before_cursor == '{' and buffer.text[cursor_position] == '}':
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				elif char_before_cursor == '[' and buffer.text[cursor_position] == ']':
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				elif char_before_cursor == '"' and buffer.text[cursor_position] == '"':
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				elif char_before_cursor == "'" and buffer.text[cursor_position] == "'":
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				elif char_before_cursor == '`' and buffer.text[cursor_position] == '`':
					event.current_buffer.cursor_right()
					event.current_buffer.delete_before_cursor(2)
				else:
					event.current_buffer.delete_before_cursor(1)

			except:
				event.current_buffer.delete_before_cursor(1)

		@kb.add(Keys.Tab)
		def _(event: KeyPressEvent):
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
						common_prefix = get_common_prefix(completions)
						current_word = buffer.document.get_word_before_cursor(WORD=True)
						current_text = current_word[current_word.rfind(os.sep) + 1:]
						extra_text = common_prefix[len(current_text):]
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
			buffer = event.current_buffer
			code = buffer.text
			lines = code.split('\n')
			cursor_position = buffer.document.cursor_position
			if cursor_position > 0:
				left_char = buffer.document.text[cursor_position - 1]
				if left_char == ":":
					indentation = get_indentation(lines[-1])
					buffer.insert_text('\n' + ' ' * (indentation + 4))
				else:
					if lines:
						indentation = get_indentation(lines[-1])
						buffer.insert_text('\n' + ' ' * indentation)
					else:
						event.current_buffer.insert_text('\n')
			else:
				event.current_buffer.insert_text('\n')

		@kb.add('(')
		def _(event: KeyPressEvent):
			input_set(event, '(', ')', '()')

		@kb.add('"')
		def _(event: KeyPressEvent):
			input_pair(event, '"', '""')

		return kb

	@staticmethod
	def _scan_path(directories: Iterable) -> Set[str]:
		all_cmd: Set[str] = set()
		for directory in directories:
			if not directory: continue
			try:
				with os.scandir(directory) as entries:
					for entry in entries:
						if entry.is_file() and os.access(entry.path, os.X_OK):
							all_cmd.add(entry.name)
			except (FileNotFoundError, PermissionError):
				continue
		return all_cmd

	def _update_layout(self) -> None:
		if hasattr(self, 'app') and self.app is not None:
			self.app.layout = self._create_layout()
			self.app.invalidate()

	def _rebuild_completer(self) -> None:
		combined = set()
		for values in self._completions.values():
			combined.update(values)
		sorted_list = sorted(
			list(combined),
			key=lambda x: (-self._usage_counter[x], len(x), x)
		)
		self._completion_words[:] = sorted_list

	async def _update_context(self, category: str, data: Any) -> None:
		if ENVIRON == category:
			os_path: Set[str] = set(os.get_exec_path(data))
			execs = await asyncio.to_thread(self._scan_path, os_path)
			self._completions[category][:] = execs
			self._cwd = data.get(PWD)
			await asyncio.to_thread(self._rebuild_completer)

		elif ALIAS == category:
			self._completions[category][:] = list(data.keys())
			await asyncio.to_thread(self._rebuild_completer)

	def cmd(self, cmd: str) -> None:
		self.app.exit(result=cmd)

	async def post_exec(self) -> None:
		if self.post_hook is None:
			return

		try:
			await self.process_handler.run(self.post_hook)
		except Exception:
			self.logger.exception('post hook failed')
			return

	async def pre_exec(self) -> None:
		if self.pre_hook is None:
			return

		try:
			await self.process_handler.run(self.pre_hook)
		except Exception:
			self.logger.exception('pre hook failed')
			return

	async def fallback(self) -> None:
		if not self.interactive_shell.last_command.strip() or self.fallback_hook is None:
			return

		try:
			status = getattr(self.context, EXITCODE, None)
			if status is not None and str(status).strip() != "0":
				await self.process_handler.run(self.fallback_hook)
		except Exception:
			self.logger.exception('fallback hook failed')
			return

	def add_tool(
		self,
		cmd: str,
		plugin_name: Optional[str] = None,
		function_name: Optional[str] = None,
		function: Optional[Callable[..., Any]] = None
	) -> None:
		if plugin_name and function_name:
			plugin = self.plugin_manager.get(plugin_name)
			if plugin and hasattr(plugin, function_name):
				self.internal_tools[cmd] = getattr(plugin, function_name)
		elif function:
			self.internal_tools[cmd] = function

	def delete_tool(self, cmd: str) -> None:
		if cmd in self.internal_tools:
			del self.internal_tools[cmd]

	def add_key(self, *keys: Union[Keys, str], handler: Callable[[KeyPressEvent], None], **kwargs) -> None:
		self.key_bindings.add(*keys, **kwargs)(handler)

	def delete_key(self, *args: Union[Keys, str, KeyHandlerCallable]) -> None:
		self.key_bindings.remove(*args)

	def add_float(self, content: AnyContainer, **kwargs) -> None:
		new_float = Float(content=content, **kwargs)
		self.floats.append(new_float)
		self._update_layout()

	def delete_float(self, target_float: Optional[Float] = None) -> None:
		if target_float is None:
			if self.floats:
				self.floats.clear()
				self._update_layout()
		else:
			if target_float in self.floats:
				self.floats.remove(target_float)
				self._update_layout()

	def set_completer(self, completer) -> None:
		comps = merge_completers([self.default_completer] + list(completer), deduplicate=True)
		self.completer = ThreadedCompleter(comps)

	def set_prompt(self, prompt: bytes, encoder: Optional[str] = None) -> None:
		cleaned_prompt = prompt.replace(b'\r\n', b'\n')
		self.message = ShellANSI(cleaned_prompt.decode(encoder or self.encoder, errors='replace'))

	def update_context(self, category: str, data: Any) -> None:
		self.context.update(category, data)

	def load_rc(self) -> None:
		try:
			import ish
			for loader, module_name, is_pkg in pkgutil.walk_packages(ish.__path__, ish.__name__ + "."):
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
				str(config.RC_FILE),
				run_name="__main__",
				init_globals=init_globals
			)

		except SystemExit:
			pass

		except Exception:
			self.logger.error(
				i18n.get('error', error=traceback.format_exc())
			)
			sys.exit(1)

	async def load_rc_async(self) -> None:
		await asyncio.to_thread(self.load_rc)

	async def get(self, **kwargs) -> Optional[str]:
		try:
			command = await self.prompt_async(**kwargs)
			if command is None:
				return ''
		except (termios.error, OSError):
			return ''

		key = command.strip()
		if key == self.exit_command:
			raise ShellExitRequest

		if key in self.internal_commands:
			handler, args, kwargs = self.internal_commands[key]
			raise ShellPassRequest(handler, command, *args, **kwargs)

		argv = simple_command(command)
		if argv and argv[0] in self.internal_tools:
			await self.process_handler.run(self.internal_tools[argv[0]], *argv[1:])
			return ''

		return command.rstrip()

	def run(self) -> None:
		self.interactive_shell.run()
