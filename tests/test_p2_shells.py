"""Real interactive shells. Override ISH_TEST_<SHELL> with a test binary path."""

import asyncio
import os
import pty
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ish.parser.shell import alias_parser, dict_parser
from ish.shell.base import InteractiveShell
from ish.shell.signal import ShellExitRequest


class Session:
    """Fake UI returning scripted commands and recording prompt, state, and hook ordering."""

    def __init__(self, commands):
        """Prepare scripted commands and stores for prompt, state, and hook observations."""
        self.commands = iter(commands)
        self.app = SimpleNamespace(_on_resize=lambda: None)
        self.context = {}
        self.prompts = []
        self.events = []

    def set_prompt(self, data, encoder):
        """Record prompt bytes received by the fake UI."""
        self.prompts.append(data)

    def update_context(self, category, body):
        """Record the most recently received state for each category."""
        self.context[category] = body

    async def get(self, **kwargs):
        """Return the next scripted command or request session exit when exhausted."""
        try:
            return next(self.commands)
        except StopIteration:
            raise ShellExitRequest from None

    async def pre_exec(self):
        """Record where the pre-command hook is called."""
        self.events.append(("pre", None))

    async def post_exec(self):
        """Record the exit status received by the post-command hook."""
        self.events.append(("post", self.context.get("exitcode")))

    async def fallback(self):
        """Record the stored exit status when fallback is called."""
        self.events.append(("fallback", self.context.get("exitcode")))


class NativeShellTests(unittest.IsolatedAsyncioTestCase):
    """Verify synchronization between real shell rc files, existing hooks, exit status, and
    ish state.
    """

    async def run_shell(self, name, rc="", *, commands=None, extra_rc=None):
        """Run a real shell with a temporary rc, environment, and PTY to verify state and
        hook order.
        """
        path = os.environ.get("ISH_TEST_" + name.upper()) or shutil.which(name)
        if not path:
            self.skipTest(name + " not installed; set ISH_TEST_" + name.upper())
        with tempfile.TemporaryDirectory(prefix="ish p2 'quotes! ") as directory:
            root = Path(directory)
            runtime = root / "runtime 'space!"
            for filename, content in (extra_rc or {}).items():
                (root / filename).write_text(content)
            if name == "zsh":
                # An extracted Debian package has its modules beside its binary.
                modules = list(
                    Path(path).parent.parent.glob("lib/*/zsh/*/zsh/parameter.so")
                )
                if modules:
                    rc = (
                        'module_path=("'
                        + str(modules[0].parent.parent)
                        + '" $module_path)\n'
                        + rc
                    )
            rc_name = {
                "bash": ".bashrc",
                "zsh": ".zshrc",
                "tcsh": ".tcshrc",
                "csh": ".cshrc",
                "sh": ".shrc",
            }[name]
            (root / rc_name).write_text(rc)
            input_master, input_slave = pty.openpty()
            session = Session(commands or ["false", "true", "pwd"])
            try:
                with (
                    tempfile.TemporaryFile() as output,
                    patch(
                        "ish.shell.integration.config",
                        SimpleNamespace(XDG_DATA_HOME=runtime),
                    ),
                    patch(
                        "ish.shell.integration.ISH_FORWARD",
                        str(runtime / "ish_forward"),
                    ),
                    patch.dict(
                        os.environ,
                        {
                            "HOME": directory,
                            "ZDOTDIR": directory,
                            "ENV": str(root / ".shrc"),
                            "TERM": "xterm",
                            "PATH": "/usr/bin:/bin",
                        },
                        clear=True,
                    ),
                ):
                    shell = InteractiveShell(
                        path, stdin=input_slave, stdout=output.fileno(), prompt=session
                    )
                    try:
                        await asyncio.wait_for(shell.main(), 8)
                    except TimeoutError:
                        output.seek(0)
                        self.fail(
                            f"{name} timed out; init={bytes(shell._init_output)!r}; output={output.read()!r}; events={session.events!r}"
                        )
                    output.seek(0)
                    received = output.read()
                    self.assertIn(os.getcwd().encode(), received)
                    self.assertEqual(
                        session.events,
                        [
                            ("pre", None),
                            ("post", b"1"),
                            ("fallback", b"1"),
                            ("pre", None),
                            ("post", b"0"),
                            ("fallback", b"0"),
                            ("pre", None),
                            ("post", b"0"),
                            ("fallback", b"0"),
                        ],
                    )
                    self.assertEqual(
                        dict_parser(session.context["environ"], "utf-8", "=", "\0")[
                            "PWD"
                        ],
                        os.getcwd(),
                    )
                    self.assertNotIn(b"not found", received)
                    self.assertTrue(session.prompts)
                    return session, received
            finally:
                os.close(input_slave)
                os.close(input_master)

    async def test_bash_scalar_hook_status_and_alias(self):
        """Pass the original exit status to Bash string hooks and preserve aliases."""
        session, _ = await self.run_shell(
            "bash",
            "PROMPT_COMMAND='printf \"original:%s\\n\" \"$?\"; :'\nalias ll='ls -l'\nPS1='original> '\n",
        )
        self.assertEqual(alias_parser(session.context["alias"], "utf-8")["ll"], "ls -l")
        self.assertEqual(session.prompts[-1], b"original> ")

    async def test_bash_array_hook_status(self):
        """Preserve array hook order and the initial exit status on supported Bash
        versions.
        """
        _, received = await self.run_shell(
            "bash",
            'PROMPT_COMMAND=(\'printf "first:%s\\n" "$?"\' \'printf "second\\n"\')\n',
        )
        path = os.environ.get("ISH_TEST_BASH") or shutil.which("bash")
        version = subprocess.check_output(
            [
                path,
                "--norc",
                "-c",
                'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"',
            ],
            env={"PATH": "/usr/bin:/bin"},
        )
        if tuple(map(int, version.split(b"."))) >= (5, 1):
            self.assertIn(b"first:1\r\nsecond\r\n", received)
        else:
            self.assertIn(b"first:1\r\n", received)
            self.assertNotIn(b"second\r\n", received)

    async def test_bash_without_original_prompt_command(self):
        """Transfer state and display repeated prompts without an existing PROMPT_COMMAND."""
        session, received = await self.run_shell(
            "bash", "unset PROMPT_COMMAND\nPS1='original> '\n"
        )
        self.assertEqual(session.prompts[-1], b"original> ")
        self.assertNotIn(b"error", received)

    async def test_bash_multiline_hook_comments_and_repeated_source(self):
        """Preserve exit status with line comments in existing hooks and repeated
        integration loading.
        """
        session, received = await self.run_shell(
            "bash",
            "PROMPT_COMMAND=$'printf \"HOOK:%s\\\\n\" \"$?\"\n# trailing comment'\nPS1='original> '\n",
            commands=["ish_recover; ish_recover; false", "true", "pwd"],
        )
        self.assertEqual(received.count(b"HOOK:1\r\n"), 1)
        self.assertEqual(received.count(b"HOOK:0\r\n"), 2)
        self.assertEqual(session.prompts[-1], b"original> ")

    async def test_zsh(self):
        """Keep zsh's existing precmd working alongside prompt and state transfer."""
        _, received = await self.run_shell(
            "zsh", "precmd() { print -r -- original:$?; :; }\nPS1='original> '\n"
        )
        self.assertIn(b"original:1", received)

    async def test_zsh_failing_hook_array_still_updates_context(self):
        """Complete ish state transfer even when a zsh user hook fails."""
        _, received = await self.run_shell(
            "zsh",
            "first() { print -r -- first:$?; return 2; }\nsecond() { print SHOULD_NOT_RUN; }\nprecmd_functions=(first second)\n",
        )
        self.assertIn(b"first:1", received)
        self.assertNotIn(b"SHOULD_NOT_RUN", received)

    async def test_zsh_unset_hook_array_has_no_empty_command(self):
        """Avoid executing an empty function name when zsh hook arrays are empty."""
        _, received = await self.run_shell(
            "zsh", "unset precmd_functions\nPS1='original> '\n"
        )
        self.assertNotIn(b"precmd:", received)
        self.assertNotIn(b"permission denied", received)

    async def test_zsh_missing_hooks_are_skipped_and_later_hook_keeps_status(self):
        """Skip missing zsh hooks and pass the previous exit status to the next valid hook."""
        _, received = await self.run_shell(
            "zsh",
            "last() { print -r -- LAST:$?; }\nprecmd_functions=('' missing_hook /bin/false last)\n",
        )
        self.assertIn(b"LAST:1\r\n", received)
        self.assertEqual(received.count(b"LAST:0\r\n"), 2)
        self.assertNotIn(b"precmd:", received)

    async def test_zsh_autoload_hook_and_repeated_source(self):
        """Preserve zsh user hooks with autoload functions and repeated sourcing."""
        _, received = await self.run_shell(
            "zsh",
            'fpath=("$HOME" $fpath)\nautoload -Uz loaded_hook\nprecmd_functions=(loaded_hook)\n',
            extra_rc={"loaded_hook": "print -r -- AUTO:$?\n"},
            commands=["ish_recover; ish_recover; false", "true", "pwd"],
        )
        self.assertEqual(received.count(b"AUTO:1\r\n"), 1)
        self.assertEqual(received.count(b"AUTO:0\r\n"), 2)
        self.assertNotIn(b"precmd:", received)

    async def test_zsh_keeps_global_startup_option_and_prompt(self):
        # Simulate site rc loading under GLOBAL_RCS without touching /etc.
        """Preserve zsh global startup options and the prompt configured by those settings."""
        session, received = await self.run_shell(
            "zsh",
            extra_rc={
                ".zshenv": 'if [[ -o globalrcs ]]; then source "$HOME/site.zshrc"; fi\n',
                "site.zshrc": "PROMPT='[employee@workstation]~%% '\n",
            },
        )
        self.assertEqual(session.prompts[-1], b"[employee@workstation]~% ")
        self.assertNotIn(b"precmd:", received)

    async def test_tcsh(self):
        """Pass the original status to tcsh's existing precmd and preserve the primary
        prompt.
        """
        _, received = await self.run_shell(
            "tcsh", "alias precmd 'echo original:$status'\nset prompt = 'original> '\n"
        )
        self.assertIn(b"original:1", received)

    async def test_csh(self):
        """Synchronize exit status and environment through BSD csh's separate refresh
        syntax.
        """
        await self.run_shell("csh", "set prompt = 'original> '\n")

    async def test_sh(self):
        """Load integration with POSIX dot syntax and refresh state in dash."""
        await self.run_shell("sh", "PS1='original> '\n")


if __name__ == "__main__":
    unittest.main()
