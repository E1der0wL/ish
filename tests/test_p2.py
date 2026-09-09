import asyncio
import gc
import json
import os
from pathlib import Path
import pty
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from ish.config import config
from ish.app.pytool import ProcessHandler
from ish.fdio import FDWriter
from ish.lang.i18n import I18N
from ish.parser.shell import alias_parser, simple_command
from ish.plugin.manager import PluginInfo, PluginManager
from ish.shell.adapters import get_adapter
from ish.shell.base import InteractiveShell, ScrollBack
from ish.shell.integration import BEFORE_PROMPT, AFTER_PROMPT
from ish.shell.sequencer import Sequencer
from ish.shell.signal import ShellExitRequest
from ish.ui.completer import PromptCompleter
from ish.ui.prompt import Prompt


class ParsingTests(unittest.TestCase):
    def test_alias_names_quotes_newlines_and_equals(self):
        aliases = {'ll': 'ls -l', 'quote': "echo 'hello'", 'lines': 'one\ntwo', 'eq': 'x=y z'}
        data = '\n'.join('alias ' + k + '=' + shlex.quote(v) for k, v in aliases.items())
        self.assertEqual(alias_parser(data.encode(), 'utf-8'), aliases)
        self.assertEqual(alias_parser(b"ll='ls -l'\nx='echo hi'", 'utf-8', 'zsh'), {'ll': 'ls -l', 'x': 'echo hi'})
        self.assertEqual(alias_parser(b'll\tls -l\nx\techo hi\n', 'utf-8', 'csh'), {'ll': 'ls -l', 'x': 'echo hi'})

    def test_literal_argv_and_shell_syntax(self):
        self.assertEqual(simple_command('tool "two words" \'\' a\\ b \'x;y\''), ['tool', 'two words', '', 'a b', 'x;y'])
        for text in ['tool x | cat', 'tool && true', 'tool >file', 'tool "$HOME"', 'tool $(echo x)', 'tool "bad']:
            self.assertIsNone(simple_command(text), text)

    def test_completion_preserves_one_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            names = ['hello world', "hello'quote", 'hello$dollar', 'hello;semi']
            for name in names:
                Path(directory, name).touch()
            Path(directory, 'hello dir').mkdir()
            completer = PromptCompleter([], cwd=lambda: [directory])
            for prefix in ['cat hel', 'cat "hel', "cat 'hel", 'cat hello\\ ']:
                doc = Document(prefix)
                results = list(completer.get_completions(doc, CompleteEvent()))
                self.assertTrue(results)
                for completion in results:
                    completed = prefix[:len(prefix) + completion.start_position] + completion.text
                    args = shlex.split(completed)
                    self.assertEqual(len(args), 2, completed)
                    self.assertIn(args[1].rstrip('/'), names + ['hello dir'])

    def test_completion_operators_case_and_substitution(self):
        completer = PromptCompleter(['ls', 'less'], cwd=lambda: [], ignore_case=True)
        for prefix in ['L', 'true && L', 'false || L', 'echo $(L', 'echo $(echo $(L', 'echo `L']:
            self.assertEqual([c.text for c in completer.get_completions(Document(prefix), CompleteEvent())], ['ls ', 'less '], prefix)
        for prefix in ['echo "a;L', "echo 'a|L", 'echo $(pwd)L', 'echo $HO', 'echo x > L', 'echo $((L']:
            self.assertEqual(list(completer.get_completions(Document(prefix), CompleteEvent())), [], prefix)


class MemoryTests(unittest.TestCase):
    def test_pending_write_queue_is_bounded_and_signals_backpressure(self):
        loop, flow = Mock(), Mock()
        writer = FDWriter(loop, 99, max_pending_bytes=128, on_flow=flow)
        with patch('ish.fdio.os.write', side_effect=BlockingIOError):
            writer.write(b'x' * 80)
            flow.assert_called_once_with(True)
            with self.assertRaises(BufferError):
                writer.write(b'x' * 80)
            self.assertEqual(writer.pending_bytes, 80)
        with patch('ish.fdio.os.write', return_value=80):
            writer._flush()
        self.assertEqual(writer.pending_bytes, 0)
        self.assertEqual(flow.call_args.args, (False,))
        writer.close()

    def test_history_counts_and_total_budget(self):
        history = ScrollBack(max_lines=2, max_bytes=64)
        for _ in range(100):
            history.append(b'a\nb\nc\n')
            history.append_ld(b'x' * 100)
            self.assertLessEqual(history.retained_bytes, 64)
            self.assertEqual(history._current_bytes, sum(map(len, history._buffer)))
            self.assertLessEqual(len(history), 2)
        history.append(b'z' * 100000)
        self.assertLessEqual(history.retained_bytes, 64)
        self.assertTrue(history.history_truncated)
        self.assertTrue(history.last_output_truncated)
        history.clear()
        self.assertEqual(history.retained_bytes, 0)
        self.assertFalse(history.last_output_truncated)

    def test_unterminated_prompt_and_controls_are_bounded_and_recover(self):
        sequencer = Sequencer(max_sequence_bytes=128)
        prompts = []
        sequencer.between_sequence(BEFORE_PROMPT, AFTER_PROMPT, prompts.append)
        for byte in BEFORE_PROMPT + b'x' * 10000:
            sequencer.interpret(bytes([byte]))
            self.assertLessEqual(len(sequencer.between_buffer), 128)
        self.assertEqual(sequencer.interpret(AFTER_PROMPT + b'OK'), b'OK')
        self.assertIn(b'[prompt truncated]', prompts[0])
        for prefix, suffix in [(b'\x1b]0;', b'\x07'), (b'\x1b[', b'm')]:
            output = sequencer.interpret(prefix + b'1' * 10000)
            self.assertLessEqual(len(sequencer.buffer), 128)
            output += sequencer.interpret(suffix + b'OK')
            self.assertEqual(output, prefix + b'1' * 10000 + suffix + b'OK')

    def test_unicode_fragmentation_and_between_passthrough(self):
        data = '가나다🙂'.encode()
        sequencer = Sequencer()
        self.assertEqual(b''.join(sequencer.interpret(bytes([v])) for v in data), data)
        seen = []
        sequencer.between_sequence(BEFORE_PROMPT, AFTER_PROMPT, seen.append, remove_seq=False)
        data = BEFORE_PROMPT + data + AFTER_PROMPT
        self.assertEqual(sequencer.interpret(data), data)
        self.assertEqual(seen, ['가나다🙂'.encode()])


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_slow_output_keeps_tail_with_bounded_queue(self):
        input_r, input_w = os.pipe()
        output_r, output_w = os.pipe()
        os.close(input_w)
        os.set_blocking(output_r, False)
        received = bytearray()
        writers = []
        loop = asyncio.get_running_loop()
        def writer(*args, **kwargs):
            instance = FDWriter(*args, **kwargs, max_pending_bytes=256 * 1024)
            writers.append(instance)
            return instance
        def read():
            received.extend(os.read(output_r, 65536))
        task = None
        try:
            with patch('ish.app.pytool.FDWriter', side_effect=writer):
                task = asyncio.create_task(ProcessHandler(stdin=input_r, stdout=output_w).run(print, 'x' * 200000 + 'END'))
                await asyncio.sleep(1.5)
                self.assertFalse(task.done())
                self.assertTrue(writers[1]._paused)
                self.assertLessEqual(writers[1].pending_bytes, 256 * 1024)
                loop.add_reader(output_r, read)
                self.assertEqual(await asyncio.wait_for(task, 5), 0)
                await asyncio.sleep(.02)
            self.assertEqual(received.count(b'x'), 200000)
            self.assertTrue(received.endswith(b'END\r\n'))
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            loop.remove_reader(output_r)
            for fd in (input_r, output_r, output_w):
                os.close(fd)

    async def test_hooks_pass_callable_and_fallback_requires_error(self):
        prompt = SimpleNamespace(pre_hook=print, post_hook=len, fallback_hook=repr,
            process_handler=SimpleNamespace(run=AsyncMock()), logger=Mock(),
            interactive_shell=SimpleNamespace(last_command='false'), context=SimpleNamespace(exitcode='1'))
        await Prompt.pre_exec(prompt)
        await Prompt.post_exec(prompt)
        await Prompt.fallback(prompt)
        self.assertEqual([c.args for c in prompt.process_handler.run.await_args_list], [(print,), (len,), (repr,)])
        prompt.context.exitcode = '0'
        await Prompt.fallback(prompt)
        self.assertEqual(prompt.process_handler.run.await_count, 3)

    async def test_internal_tool_with_quoted_arguments(self):
        prompt = SimpleNamespace(prompt_async=AsyncMock(return_value='tool "two words" \'\' a\\ b'),
            exit_command='ish_exit', internal_commands={}, internal_tools={'tool': print},
            process_handler=SimpleNamespace(run=AsyncMock()))
        self.assertEqual(await Prompt.get(prompt), '')
        prompt.process_handler.run.assert_awaited_once_with(print, 'two words', '', 'a b')
        prompt.prompt_async.return_value = 'tool x | cat'
        self.assertEqual(await Prompt.get(prompt), 'tool x | cat')

    async def test_custom_fds_reach_both_engines_and_remain_owned_by_caller(self):
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryFile(mode='w+') as out:
                with patch('ish.ui.prompt.get_builtins', return_value=[]):
                    prompt = Prompt(shell='bash', input_fd=slave, output_fd=out.fileno())
                self.assertEqual(prompt.interactive_shell.stdin_fd, slave)
                self.assertEqual(prompt.interactive_shell.stdout_fd, out.fileno())
                self.assertEqual(prompt.process_handler.stdout_fd, out.fileno())
                prompt.input.close()
                del prompt
                gc.collect()
                os.fstat(slave)
                os.fstat(out.fileno())
        finally:
            os.close(master)
            os.close(slave)

    async def test_context_wait_handles_prompt_arriving_before_fifo(self):
        shell = InteractiveShell('bash', stdin=0, stdout=1, prompt=Mock())
        shell._prompt_id = 2
        shell._context_id = 1
        task = asyncio.create_task(shell._wait_context())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        shell._context_id = 2
        shell._context_event.set()
        await asyncio.wait_for(task, 1)


class DependencyTests(unittest.TestCase):
    def test_pep440_ranges_and_invalid_specs(self):
        manager = PluginManager()
        for version, spec, expected in [('2.0', '~=1.2', False), ('1.9', '~=1.2', True),
                ('1.3', '~=1.2.3', False), ('1.8', '>=1.2,<2,!=1.8', False),
                ('1.4', '==1.*', True), ('1.0rc1', '>=1.0rc1,<2', True), ('1.0', 'garbage', False)]:
            with patch.object(manager, 'logger'):
                self.assertEqual(manager._ensure_version(version, spec), expected)

    def test_install_uses_argv_without_shell(self):
        manager = PluginManager()
        manager.python_exe = '/tmp/python with spaces'
        with patch('ish.plugin.manager.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(manager._load_library('demo>=1.2,<2|demo_import'))
        self.assertEqual(run.call_args.args[0][-1], 'demo>=1.2,<2')
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_distribution_version_uses_distribution_name(self):
        manager = PluginManager()
        with patch('ish.plugin.manager.importlib.util.find_spec', return_value=object()), \
             patch('ish.plugin.manager.importlib.metadata.version', return_value='1.5') as version:
            self.assertTrue(manager._ensure_library('distribution>=1.2|import_name'))
            version.assert_called_once_with('distribution')


class LanguageTests(unittest.TestCase):
    def test_partial_translation_fallback_and_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            lang = I18N()
            lang.base_path = Path(directory)
            Path(directory, 'en.json').write_text(json.dumps({'custom': 'English'}))
            Path(directory, 'ko.json').write_text(json.dumps({'custom': '한국어'}))
            lang.load_messages('ko')
            self.assertEqual(lang.get('custom'), '한국어')
            self.assertNotEqual(lang.get('error'), 'error')
            lang.load_messages('fr')
            self.assertEqual(lang.get('custom'), 'English')

    def test_invalid_language_logs_and_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            lang = I18N()
            lang.base_path = Path(directory)
            Path(directory, 'ko.json').write_text('{broken')
            with self.assertLogs('global', level='WARNING'):
                lang.load_messages('ko')
            self.assertNotEqual(lang.get('error'), 'error')


if __name__ == '__main__':
    unittest.main()
