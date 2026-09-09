"""Integration policies must follow configuration, not hard-coded shell names."""
import asyncio
from dataclasses import replace
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

from ish.parser.shell import alias_parser
from ish.shell.adapters import ADAPTERS, SYNTAXES, ShellAdapter, ShellSyntax, CSH_BEHAVIOR, get_adapter
from ish.shell.base import InteractiveShell
from ish.shell.constants import (
    BEFORE_PROMPT, AFTER_PROMPT, BEFORE_CONTINUATION, AFTER_CONTINUATION,
    PROMPT_ID_PREFIX, OSC_TERMINATOR, CARET_BEFORE_PROMPT, CARET_AFTER_PROMPT,
    BASH_INTEGRATION_SCRIPT, CSH_UPDATE_SCRIPT, POSIX_UPDATE_SCRIPT,
    TCSH_PRECMD_SCRIPT, bytes_to_shell_escape,
)
from ish.shell.scripts import make_scripts
from ish.shell.sequencer import Sequencer
from ish.ui.prompt import get_builtins


class AdapterTests(unittest.TestCase):
    def test_source_quotes_paths_and_passes_positional_arguments(self):
        values = ("/tmp/ish user's/init", '/tmp/a $x;!', '/tmp/b pipe')
        for name in ('bash', 'zsh'):
            with self.subTest(shell=name):
                self.assertEqual(shlex.split(get_adapter(name).source(*values)), ['source', *values])
        self.assertEqual(get_adapter('tcsh').source(*values),
                         "source /tmp/ish\\ user\\'s/init /tmp/a\\ \\$x\\;\\! /tmp/b\\ pipe\n")

    def test_source_binds_variables_for_shells_without_source_arguments(self):
        values = ('/tmp/init file', '/tmp/in pipe', '/tmp/out pipe')
        self.assertEqual(get_adapter('sh').source(*values),
                         "_ish_pipe='/tmp/in pipe'; _tty_pipe='/tmp/out pipe'; . '/tmp/init file'\n")
        self.assertEqual(get_adapter('csh').source(*values),
                         'set _ish_pipe = /tmp/in\\ pipe; set _tty_pipe = /tmp/out\\ pipe; source /tmp/init\\ file\n')
        for name in ('sh', 'csh'):
            with self.subTest(shell=name), self.assertRaises(ValueError):
                get_adapter(name).source('/tmp/init', 'only one pipe')

    def test_refresh_preserves_status_before_source_only_when_required(self):
        directory = Path('/tmp/ish scripts')
        self.assertEqual(get_adapter('csh').refresh_command(directory),
                         f'set _ish_shell_exit_code = $status; source /tmp/ish\\ scripts/{CSH_UPDATE_SCRIPT}\n')
        self.assertEqual(get_adapter('sh').refresh_command(directory),
                         f". '/tmp/ish scripts/{POSIX_UPDATE_SCRIPT}'\n")
        for name in ('bash', 'zsh', 'tcsh'):
            self.assertEqual(get_adapter(name).refresh_command(directory), '')

    def test_executable_aliases_and_csh_symlink(self):
        self.assertEqual(get_adapter('/bin/dash').name, 'sh')
        self.assertEqual(get_adapter('/usr/bin/bsd-csh').name, 'csh')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'csh'
            path.symlink_to(Path(directory) / 'tcsh')
            self.assertEqual(get_adapter('csh', str(path)).name, 'tcsh')
        with self.assertRaisesRegex(ValueError, 'Unsupported shell'):
            get_adapter('unknown-shell')

    def test_new_shell_reuses_family_quoting_aliases_and_builtins(self):
        adapter = replace(ADAPTERS['csh'], name='new-shell')
        with patch.dict(ADAPTERS, {'new-shell': adapter}):
            self.assertEqual(alias_parser(b'll\tls -l\n', 'utf-8', 'new-shell'), {'ll': 'ls -l'})
            self.assertEqual(get_builtins('new-shell'), get_builtins('csh'))
            self.assertEqual(get_adapter('new-shell').source('/a b'), 'source /a\\ b\n')

    def test_new_syntax_supplies_source_assignment_and_status_expression(self):
        syntax = ShellSyntax(
            quote=lambda value: '<' + value + '>', assignment='let {name} {value}',
            exit_status='$result', source_command='load',
            parse_aliases=lambda text: {'custom': text}, lexer='text',
        )
        adapter = ShellAdapter(
            'example', 'example', ('-i',), 'init.example',
            source_args=('input', 'output'), refresh_script='refresh.example',
            capture_refresh_status=True,
        )
        with patch.dict(SYNTAXES, {'example': syntax}), patch.dict(ADAPTERS, {'example': adapter}):
            self.assertEqual(adapter.source('/init', '/in pipe', '/out pipe'),
                             'let input </in pipe>; let output </out pipe>; load </init>\n')
            self.assertEqual(adapter.refresh_command(Path('/tmp')),
                             'let _ish_shell_exit_code $result; load </tmp/refresh.example>\n')
            self.assertEqual(alias_parser(b'value', 'utf-8', 'example'), {'custom': 'value'})

    def test_native_input_policy_can_be_reused_and_overridden(self):
        command = 'foreach x (a b)\necho $x\nend'
        for native in (True, False):
            adapter = replace(ADAPTERS['tcsh'], name='new-shell', behavior=replace(
                CSH_BEHAVIOR, native_continuation=native, batch_input=native))
            with self.subTest(native=native), patch.dict(ADAPTERS, {'new-shell': adapter}):
                session = Mock()
                engine = InteractiveShell('/not-installed/new-shell', prompt=session)
                engine.continuation_active = asyncio.Event()
                engine.prompt_event = asyncio.Event()
                output = engine._set_continuation(b'foreach? ')
                self.assertTrue(engine.continuation_active.is_set())
                if native:
                    self.assertEqual(output, b'foreach? ')
                    session.set_prompt.assert_not_called()
                    self.assertFalse(engine.prompt_event.is_set())
                    self.assertEqual(adapter.behavior.split_commands(command), [command])
                else:
                    self.assertIsNone(output)
                    session.set_prompt.assert_called_once_with(b'foreach? ', engine.encoder)
                    self.assertTrue(engine.prompt_event.is_set())
                    self.assertEqual(adapter.behavior.split_commands(command), command.splitlines())


class AdapterSignalTests(unittest.TestCase):
    def test_configured_signals_at_every_chunk_boundary(self):
        # A renamed adapter must keep the extra caret prompt registration.
        adapters = [*ADAPTERS.values(), replace(ADAPTERS['tcsh'], name='new-shell')]
        stream = (b'first' + PROMPT_ID_PREFIX + b'42' + OSC_TERMINATOR
                  + BEFORE_PROMPT + b'main> ' + AFTER_PROMPT
                  + BEFORE_CONTINUATION + b'next> ' + AFTER_CONTINUATION
                  + CARET_BEFORE_PROMPT + b'recovered> ' + CARET_AFTER_PROMPT + b'last')
        for adapter in adapters:
            for split in range(len(stream) + 1):
                with self.subTest(shell=adapter.name, split=split):
                    sequencer = Sequencer()
                    ids, prompts, continuations, recovered = [], [], [], []
                    adapter.configure_sequencer(
                        sequencer, prompt_id=ids.append, prompt=prompts.append,
                        continuation=continuations.append, unhooked_prompt=recovered.append)
                    output = sequencer.interpret(stream[:split]) + sequencer.interpret(stream[split:])
                    self.assertEqual(ids, [b'42'])
                    self.assertEqual(prompts, [b'main> '])
                    self.assertEqual(continuations, [b'next> '])
                    if adapter.unhooked_prompt:
                        self.assertEqual(recovered, [b'recovered> '])
                        self.assertEqual(output, b'firstlast')
                    else:
                        self.assertEqual(recovered, [])
                        self.assertEqual(output, b'first' + CARET_BEFORE_PROMPT
                                         + b'recovered> ' + CARET_AFTER_PROMPT + b'last')

    def test_script_markers_use_shared_constants(self):
        # Change the protocol tokens to detect stale literals in templates,
        # including prompt wrapping guards and the C shell glob check.
        start, prefix = b'\x1b]900;X\a', b'\x1b]900;I;'
        with patch('ish.shell.scripts.BEFORE_PROMPT', start), patch('ish.shell.scripts.PROMPT_ID_PREFIX', prefix):
            scripts = make_scripts(Path('/tmp/ish'))
        bash = scripts[BASH_INTEGRATION_SCRIPT]
        hook = scripts[TCSH_PRECMD_SCRIPT]
        self.assertIn("*$'" + bytes_to_shell_escape(start) + "'*", bash)
        self.assertIn("printf '" + bytes_to_shell_escape(start) + "'", bash)
        self.assertIn('*900\\;X*', hook)
        for body in (bash, hook):
            self.assertIn(bytes_to_shell_escape(prefix + b'%s' + OSC_TERMINATOR), body)
            self.assertNotIn('633;P;', body)


if __name__ == '__main__':
    unittest.main()
