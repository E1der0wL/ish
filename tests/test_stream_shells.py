"""Observe optional tcsh decoration signals through its real controlling PTY."""

import contextlib
import errno
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import tempfile
import time
import unittest
from pathlib import Path

from ish.stream.shells import Integration

TCSH = shutil.which("tcsh")
NONCE = "0123456789abcdef0123456789abcdef"
MARKER = re.compile(rb"\x1b\]777;ish;" + NONCE.encode() + rb";(\d+);([PBH])\x07")


class Shell:
    """Drive native shell input without parsing it or implementing an editor."""

    def __init__(self, root, *, integration=True, startup=True, popup_socket=None):
        """Give every test a controlling terminal and isolated user startup file."""
        env = dict(os.environ, HOME=str(root), TERM="xterm-256color", LANG="C.UTF-8")
        self.integration = (
            Integration([TCSH, "-i"], env, nonce=NONCE, popup_socket=popup_socket)
            if integration
            else None
        )
        argv = self.integration.argv if self.integration else [TCSH, "-i"]
        env = self.integration.env if self.integration else env
        self.output = bytearray()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            os.execve(TCSH, argv, env)
        os.set_blocking(self.fd, False)
        if startup:
            self.until(lambda: self.output.endswith(b"READY> "))

    def until(self, predicate, timeout=4):
        """Use a deadline only to fail a missing observable result."""
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(repr(bytes(self.output)))
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    raise AssertionError("Shell exited: " + repr(bytes(self.output)))
                self.output.extend(data)

    def send(self, data):
        """Write exact native input bytes, including control keys and pasted lines."""
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def submit(self, data):
        """Observe the next native primary after a complete test command."""
        self.output.clear()
        self.send(data)
        self.until(lambda: self.output.endswith(b"READY> "))
        return bytes(self.output)

    def close(self):
        """Reap the isolated shell and remove optional startup state."""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        if self.integration:
            self.integration.close()


@unittest.skipUnless(TCSH, "tcsh is required")
class ShellTests(unittest.TestCase):
    """Keep notification protocol separate from the native input consumer."""

    maxDiff = None

    @contextlib.contextmanager
    def session(
        self,
        *,
        integration=True,
        config="",
        fallback=False,
        prefix="ish-stream-test-",
        popup=False,
    ):
        """Apply the same user configuration to wrapped and stock tcsh."""
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            root = Path(directory)
            (root / (".cshrc" if fallback else ".tcshrc")).write_text(
                "set prompt = 'READY> '\nset prompt2 = 'CONT> '\nunset autologout\n"
                "set history = 100\n" + config
            )
            listener = None
            if popup:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(root / "control.sock"))
                listener.listen()
                listener.settimeout(4)
            shell = Shell(
                root,
                integration=integration,
                popup_socket=str(root / "control.sock") if popup else None,
            )
            shell.listener = listener
            try:
                yield root, shell
            finally:
                shell.close()
                if listener:
                    listener.close()

    def events(self, data):
        """Decode test expectations independently of the production stream parser."""
        return [(int(serial), kind.decode()) for serial, kind in MARKER.findall(data)]

    def test_user_hooks_status_and_history_match_native(self):
        """Bookkeeping adds neither user hook invocations nor history commands."""
        results = []
        config = (
            "alias precmd 'echo PRE:$status >> hooks'\n"
            "alias postcmd 'echo POST:$status >> hooks'\n"
        )
        for enabled in (False, True):
            with self.session(integration=enabled, config=config) as (root, shell):
                shell.submit("/bin/false\n")
                shell.submit("echo $status > result\n")
                shell.submit("history -h > history-result\n")
                results.append(
                    tuple(
                        (root / name).read_bytes()
                        for name in ("hooks", "result", "history-result")
                    )
                )
                self.assertEqual((root / "result").read_bytes(), b"1\n")
                self.assertNotIn(b"_ish_stream", results[-1][2])
        self.assertEqual(*results)

    def test_ordered_primary_and_hide_leave_prompt_unchanged(self):
        """Hide precedes command output; primary precedes the native user prompt."""
        with self.session() as (_, shell):
            self.assertEqual(self.events(shell.output), [(1, "P")])
            output = shell.submit("echo RUN\n")
            self.assertEqual(self.events(output), [(2, "H"), (3, "P")])
            self.assertLess(output.index(b";H\x07"), output.index(b"RUN\r\n"))
            self.assertTrue(output.endswith(b";3;P\x07READY> "))

    def test_empty_and_syntax_error_return_to_confirmed_primary(self):
        """A skipped postcmd does not manufacture an execution acknowledgement."""
        with self.session() as (_, shell):
            for command in ("\n", "if (\n", "nosuch_ish_stream_command\n"):
                with self.subTest(command=command):
                    output = shell.submit(command)
                    self.assertEqual(self.events(output)[-1][1], "P")

    def test_foreach_keeps_continuation_native(self):
        """Only the primary editor is decorated; loop input remains native syntax."""
        with self.session() as (root, shell):
            shell.output.clear()
            shell.send("foreach value ( A B )\n")
            shell.until(lambda: shell.output.endswith(b"CONT> "))
            self.assertEqual([kind for _, kind in self.events(shell.output)], ["H"])
            shell.output.clear()
            shell.send("echo $value >> result\n")
            shell.until(lambda: shell.output.endswith(b"CONT> "))
            self.assertNotIn("P", [kind for _, kind in self.events(shell.output)])
            self.assertFalse((root / "result").exists())
            shell.submit("end\n")
            self.assertEqual((root / "result").read_bytes(), b"A\nB\n")

    def test_heredoc_preserves_unicode_spaces_and_tool_names(self):
        """Heredoc content has no internal dispatcher and reaches its actual file."""
        payload = "  한 글 🙂  \nish_exit\nprobe\n"
        with self.session() as (root, shell):
            output = shell.submit("cat << END > result\n" + payload + "END\n")
            self.assertEqual((root / "result").read_bytes(), payload.encode())
            self.assertEqual([kind for _, kind in self.events(output)].count("P"), 1)

    def test_dollar_input_and_external_stdin_keep_pty_consumer(self):
        """Executing code reads native input without a primary signal in between."""
        for command in (
            'echo WAIT; set value = "$<"; echo "$value" > result\n',
            "/bin/sh reader.sh\n",
        ):
            with self.subTest(command=command), self.session() as (root, shell):
                (root / "reader.sh").write_text(
                    'echo WAIT\nIFS= read -r value\nprintf "%s\\n" "$value" > result\n'
                )
                shell.output.clear()
                shell.send(command)
                shell.until(lambda: b"WAIT\r\n" in shell.output)
                self.assertEqual([kind for _, kind in self.events(shell.output)], ["H"])
                shell.send("  native 한 글  \n")
                shell.until(lambda: shell.output.endswith(b"READY> "))
                self.assertEqual((root / "result").read_text(), "  native 한 글  \n")

    def test_source_keeps_user_postcmd_order(self):
        """Source may emit repeated hides but calls the user's hook just as before."""
        results = []
        config = "alias precmd 'echo PRE:$status >> hooks'\nalias postcmd 'echo POST:$status >> hooks'\n"
        for enabled in (False, True):
            with self.session(integration=enabled, config=config) as (root, shell):
                (root / "inner").write_text(
                    "echo first >> result\n/bin/false\necho $status >> result\n"
                )
                shell.submit("source inner\n")
                results.append(
                    ((root / "hooks").read_bytes(), (root / "result").read_bytes())
                )
        self.assertEqual(*results)

    def test_multiline_typeahead_has_increasing_wire_identity(self):
        """Fast native input executes once and never reuses a notification serial."""
        with self.session() as (root, shell):
            shell.output.clear()
            shell.send("echo first > result\necho second >> result\necho done > done\n")
            shell.until(
                lambda: (root / "done").exists() and shell.output.endswith(b"READY> ")
            )
            self.assertEqual((root / "result").read_bytes(), b"first\nsecond\n")
            events = self.events(shell.output)
            self.assertEqual([serial for serial, _ in events], list(range(2, 8)))
            self.assertEqual([kind for _, kind in events], ["H", "P"] * 3)

    def test_interrupt_continuation_returns_to_primary(self):
        """Ctrl+C remains tcsh's cancellation and does not execute the loop body."""
        with self.session() as (root, shell):
            shell.output.clear()
            shell.send("foreach value ( A B )\n")
            shell.until(lambda: shell.output.endswith(b"CONT> "))
            shell.output.clear()
            shell.send(b"\x03")
            shell.until(lambda: shell.output.endswith(b"READY> "))
            self.assertEqual(self.events(shell.output)[-1][1], "P")
            self.assertFalse((root / "result").exists())

    def test_fallback_rc_and_home_are_preserved(self):
        """Fallback rc, literal HOME paths and shell home reach user code unchanged."""
        with self.session(fallback=True, prefix="ish-stream-quote'!-") as (root, shell):
            shell.submit('echo "$HOME" > env-home; echo "$home" > shell-home\n')
            self.assertEqual((root / "env-home").read_text().rstrip("\n"), str(root))
            self.assertEqual((root / "shell-home").read_text().rstrip("\n"), str(root))

    def test_changed_postcmd_is_wrapped_before_next_primary(self):
        """Changing the user's hide hook cannot leave a later primary unguarded."""
        with self.session() as (root, shell):
            shell.submit("alias postcmd 'echo changed >> hooks'\n")
            output = shell.submit("echo done > result\n")
            self.assertEqual([kind for _, kind in self.events(output)], ["H", "P"])
            self.assertEqual((root / "hooks").read_bytes(), b"changed\n")

    def test_command_substitution_does_not_capture_private_markers(self):
        """Protocol bytes never become command-substitution data."""
        with self.session() as (root, shell):
            shell.submit('set value = "`/bin/echo answer`"; echo "$value" > result\n')
            self.assertEqual((root / "result").read_bytes(), b"answer\n")

    def test_source_redirection_does_not_capture_private_markers(self):
        """Internal hide notifications must not pollute a sourced command's data."""
        with self.session() as (root, shell):
            (root / "inner").write_text("echo first\necho second\n")
            shell.submit("source inner > result\n")
            self.assertEqual((root / "result").read_bytes(), b"first\nsecond\n")

    def test_startup_failure_status_matches_native(self):
        """Setup must not replace a status left by the user's final rc command."""
        results = []
        for enabled in (False, True):
            with self.session(integration=enabled, config="/bin/false\n") as (
                root,
                shell,
            ):
                shell.submit("echo $status > result\n")
                results.append((root / "result").read_bytes())
        self.assertEqual(*results)
        self.assertEqual(results[0], b"1\n")

    def test_nested_while_foreach_keeps_native_execution_order(self):
        """Nested body collection stays in tcsh and writes each value once."""
        with self.session() as (root, shell):
            shell.submit("set i = 0\n")
            output = shell.submit(
                "while ( $i < 1 )\nforeach value ( A B )\necho $value >> result\nend\n@ i++\nend\n"
            )
            self.assertEqual((root / "result").read_bytes(), b"A\nB\n")
            self.assertEqual([kind for _, kind in self.events(output)].count("P"), 1)

    def test_no_integration_has_no_private_markers(self):
        """The undecorated baseline launches the exact native startup path."""
        with self.session(integration=False) as (_, shell):
            self.assertNotIn(b"\x1b]777;ish;", shell.output)
            self.assertNotIn(b"\x1b]777;ish;", shell.submit("echo ordinary\n"))

    def test_bound_command_identity_survives_user_hook_wrapping(self):
        """Current-command expansion distinguishes the actual native binding body."""
        config = 'alias _ish_stream_probe \'echo POP > popup\'\nset _probe_body = "`alias _ish_stream_probe`"\nbindkey -c \'\\eOQ\' \'_ish_stream_probe\'\nalias postcmd \'set _probe_command = "\\!#:q"; echo "$_probe_command==$_probe_body" >> hooks; if ("$_probe_command" == "$_probe_body") echo MATCH >> matched\'\n'
        with self.session(config=config) as (root, shell):
            shell.output.clear()
            shell.send(b"echo kept > result\x1bOQ")
            shell.until(lambda: (root / "popup").exists())
            shell.send(b"\n")
            shell.until(
                lambda: (root / "result").exists() and shell.output.endswith(b"READY> ")
            )
            self.assertEqual((root / "matched").read_bytes(), b"MATCH\n")
            self.assertEqual((root / "result").read_bytes(), b"kept\n")

    def test_popup_binding_waits_for_ack_without_consuming_native_input(self):
        """Native key context opens the socket and resumes the same editing line."""
        with self.session(popup=True) as (root, shell):
            shell.output.clear()
            shell.send(b"echo kept > result\x1bOQ")
            connection, _ = shell.listener.accept()
            with connection:
                connection.settimeout(4)
                request = json.loads(connection.makefile("rb").readline())
                self.assertEqual(request, {"nonce": NONCE, "serial": 2})
                shell.until(lambda: bool(self.events(shell.output)))
                self.assertEqual(self.events(shell.output), [(2, "B")])
                shell.send(b"\n")
                self.assertFalse((root / "result").exists())
                connection.sendall(b"done\n")
            shell.until(
                lambda: (root / "result").exists() and shell.output.endswith(b"READY> ")
            )
            self.assertEqual((root / "result").read_bytes(), b"kept\n")
            self.assertEqual(
                self.events(shell.output), [(2, "B"), (3, "P"), (4, "H"), (5, "P")]
            )

    def test_popup_cancel_preserves_status_and_does_not_repeat_precmd(self):
        """Popup cancellation returns to native editing without invoking user precmd."""
        config = "alias precmd 'echo PRE >> hooks'\n"
        with self.session(popup=True, config=config) as (root, shell):
            shell.submit("/bin/false\n")
            shell.output.clear()
            shell.send(b"echo $status > result\x1b[12~")
            connection, _ = shell.listener.accept()
            with connection:
                connection.settimeout(4)
                request = json.loads(connection.makefile("rb").readline())
                self.assertEqual(request["serial"], 4)
                connection.sendall(b"cancel\n")
            shell.until(lambda: self.events(shell.output)[-1:] == [(5, "P")])
            self.assertEqual((root / "hooks").read_bytes(), b"PRE\nPRE\n")
            shell.submit("\n")
            self.assertEqual((root / "result").read_bytes(), b"1\n")

    def test_popup_binding_does_not_activate_in_continuation(self):
        """A continuation editor cannot request the primary-only popup."""
        with self.session(popup=True) as (root, shell):
            shell.output.clear()
            shell.send("foreach value ( A )\n")
            shell.until(lambda: shell.output.endswith(b"CONT> "))
            shell.output.clear()
            shell.send(b"\x1bOQecho $value > result\nend\n")
            shell.until(
                lambda: (root / "result").exists() and shell.output.endswith(b"READY> ")
            )
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])
            self.assertEqual((root / "result").read_bytes(), b"A\n")
            self.assertFalse(select.select([shell.listener], [], [], 0)[0])

    def test_reserved_popup_binding_is_restored_at_each_primary(self):
        """The explicit demo shortcut cannot silently become a non-acknowledging key."""
        with self.session(popup=True) as (_, shell):
            shell.submit("bindkey -c '\\eOQ' 'echo replaced'\n")
            shell.output.clear()
            shell.send(b"\x1bOQ")
            connection, _ = shell.listener.accept()
            with connection:
                connection.settimeout(4)
                json.loads(connection.makefile("rb").readline())
                connection.sendall(b"cancel\n")
            shell.until(lambda: self.events(shell.output)[-1:] == [(5, "P")])


if __name__ == "__main__":
    unittest.main()
