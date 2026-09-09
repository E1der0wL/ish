"""Real interactive shells. Override ISH_TEST_<SHELL> with a test binary path."""
import asyncio
import os
from pathlib import Path
import pty
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ish.config import config
from ish.parser.shell import alias_parser, dict_parser
from ish.shell.base import InteractiveShell
from ish.shell.signal import ShellExitRequest


class Session:
    def __init__(self, commands):
        self.commands = iter(commands)
        self.app = SimpleNamespace(_on_resize=lambda: None)
        self.context = {}
        self.prompts = []
        self.events = []

    def set_prompt(self, data, encoder):
        self.prompts.append(data)

    def update_context(self, category, body):
        self.context[category] = body

    async def get(self, **kwargs):
        try:
            return next(self.commands)
        except StopIteration:
            raise ShellExitRequest

    async def pre_exec(self):
        self.events.append(('pre', None))

    async def post_exec(self):
        self.events.append(('post', self.context.get('exitcode')))

    async def fallback(self):
        self.events.append(('fallback', self.context.get('exitcode')))


class NativeShellTests(unittest.IsolatedAsyncioTestCase):
    async def run_shell(self, name, rc=''):
        path = os.environ.get('ISH_TEST_' + name.upper()) or shutil.which(name)
        if not path:
            self.skipTest(name + ' not installed; set ISH_TEST_' + name.upper())
        with tempfile.TemporaryDirectory(prefix="ish p2 'quotes! ") as directory:
            root = Path(directory)
            runtime = root / "runtime 'space!"
            if name == 'zsh':
                # An extracted Debian package has its modules beside its binary.
                modules = list(Path(path).parent.parent.glob('lib/*/zsh/*/zsh/parameter.so'))
                if modules:
                    rc = 'module_path=("' + str(modules[0].parent.parent) + '" $module_path)\n' + rc
            rc_name = {'bash': '.bashrc', 'zsh': '.zshrc', 'tcsh': '.tcshrc', 'csh': '.cshrc', 'sh': '.shrc'}[name]
            (root / rc_name).write_text(rc)
            input_master, input_slave = pty.openpty()
            session = Session(['false', 'true', 'pwd'])
            try:
                with tempfile.TemporaryFile() as output, \
                     patch('ish.shell.integration.config', SimpleNamespace(XDG_DATA_HOME=runtime)), \
                     patch('ish.shell.integration.ISH_FORWARD', str(runtime / 'ish_forward')), \
                     patch.dict(os.environ, {'HOME': directory, 'ZDOTDIR': directory,
                         'ENV': str(root / '.shrc'), 'TERM': 'xterm', 'PATH': '/usr/bin:/bin'}, clear=True):
                    shell = InteractiveShell(path, stdin=input_slave, stdout=output.fileno(), prompt=session)
                    try:
                        await asyncio.wait_for(shell.main(), 8)
                    except asyncio.TimeoutError:
                        output.seek(0)
                        self.fail(f'{name} timed out; init={bytes(shell._init_output)!r}; output={output.read()!r}; events={session.events!r}')
                    output.seek(0)
                    received = output.read()
                    self.assertIn(os.getcwd().encode(), received)
                    self.assertEqual(session.events, [
                        ('pre', None), ('post', b'1'), ('fallback', b'1'),
                        ('pre', None), ('post', b'0'), ('fallback', b'0'),
                        ('pre', None), ('post', b'0'), ('fallback', b'0')])
                    self.assertEqual(dict_parser(session.context['environ'], 'utf-8', '=', '\0')['PWD'], os.getcwd())
                    self.assertNotIn(b'not found', received)
                    self.assertTrue(session.prompts)
                    return session, received
            finally:
                os.close(input_slave)
                os.close(input_master)

    async def test_bash_scalar_hook_status_and_alias(self):
        session, _ = await self.run_shell('bash', "PROMPT_COMMAND='printf \"original:%s\\n\" \"$?\"; :'\nalias ll='ls -l'\nPS1='original> '\n")
        self.assertEqual(alias_parser(session.context['alias'], 'utf-8')['ll'], 'ls -l')
        self.assertEqual(session.prompts[-1], b'original> ')

    async def test_bash_array_hook_status(self):
        _, received = await self.run_shell('bash', "PROMPT_COMMAND=('printf \"first:%s\\n\" \"$?\"' 'printf \"second\\n\"')\n")
        self.assertIn(b'first:1\r\nsecond\r\n', received)

    async def test_zsh(self):
        _, received = await self.run_shell('zsh', "precmd() { print -r -- original:$?; :; }\nPS1='original> '\n")
        self.assertIn(b'original:1', received)

    async def test_zsh_failing_hook_array_still_updates_context(self):
        _, received = await self.run_shell('zsh', "first() { print -r -- first:$?; return 2; }\nsecond() { print SHOULD_NOT_RUN; }\nprecmd_functions=(first second)\n")
        self.assertIn(b'first:1', received)
        self.assertNotIn(b'SHOULD_NOT_RUN', received)

    async def test_tcsh(self):
        _, received = await self.run_shell('tcsh', "alias precmd 'echo original:$status'\nset prompt = 'original> '\n")
        self.assertIn(b'original:1', received)

    async def test_csh(self):
        await self.run_shell('csh', "set prompt = 'original> '\n")

    async def test_sh(self):
        await self.run_shell('sh', "PS1='original> '\n")


if __name__ == '__main__':
    unittest.main()
