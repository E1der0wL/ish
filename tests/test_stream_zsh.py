"""Observe native ZLE contracts through real PTYs, sockets and file results."""

from __future__ import annotations

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
import termios
import time
import unittest
from pathlib import Path

from ish.stream.bundled.stream_zsh.stream_zsh import build
from stream_fixture import shell_executable

ZSH = shutil.which(shell_executable("zsh"))
NONCE = "zsh-stream-test-0123456789abcdef"
MARKER = re.compile(rb"\x1b\]777;ish;" + NONCE.encode() + rb";(\d+);([PBH])\x07")
PROMPT = b"READY> "
MORE = b"MORE> "
F2 = b"\x1bOQ"
DEADLINE = 8
READER = """import os, termios, tty
from pathlib import Path
fd = os.open('/dev/tty', os.O_RDWR)
saved = termios.tcgetattr(fd)
tty.setraw(fd)
Path('ready').write_text('ready')
data = bytearray()
while not data.endswith(b'END'):
    data.extend(os.read(fd, 1024))
Path('input').write_bytes(data)
termios.tcsetattr(fd, termios.TCSANOW, saved)
"""


class Shell:
    """Drive a native zsh without using the production event parser."""

    def __init__(self, root, *, enabled, popup, environment=None, script_dir=None):
        """Start an isolated user home and optional integration in a real PTY."""
        self.root, self.enabled = root, enabled
        env = dict(os.environ, HOME=str(root), TERM="xterm-256color", LANG="C.UTF-8")
        env.pop("ZDOTDIR", None)
        env.update(environment or {})
        self.listener = None
        if popup:
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(str(root / "control"))
            self.listener.listen()
            self.listener.settimeout(DEADLINE)
        argv = [ZSH, "-i"]
        if enabled:
            argv, env = build(
                argv,
                env,
                root,
                NONCE,
                str(root / "control") if popup else None,
                script_dir=script_dir,
            )
        self.output = bytearray()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            termios.tcsetwinsize(0, (24, 80))
            os.chdir(root)
            os.execve(ZSH, argv, env)
        os.set_blocking(self.fd, False)
        try:
            self.until(self.primary)
        except BaseException:
            self.close()
            raise

    def primary(self):
        """Observe a fixture prompt and, when enabled, its native init marker."""
        return PROMPT in self.output and (not self.enabled or b";P\x07" in self.output)

    def until(self, predicate):
        """Bound a failing observation without inferring production readiness."""
        deadline = time.monotonic() + DEADLINE
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(repr(bytes(self.output)))
            if not select.select([self.fd], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                raise AssertionError("Shell exited: " + repr(bytes(self.output)))
            self.output.extend(data)

    def send(self, data):
        """Write literal external input to the shell's controlling terminal."""
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def submit(self, data):
        """Submit native syntax and observe the next primary prompt."""
        self.output.clear()
        self.send(data)
        self.until(self.primary)
        return bytes(self.output)

    def close(self):
        """Reap only this test's shell and private control socket."""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        if self.listener:
            self.listener.close()


@unittest.skipUnless(ZSH, "zsh is required")
class ZshTests(unittest.TestCase):
    """Separate adapter observations from the shared actual PTK CLI suite."""

    maxDiff = None

    @contextlib.contextmanager
    def session(
        self, *, enabled=True, popup=True, config="", setup=None, script_dir=None
    ):
        """Use matching native startup files for both sides of comparisons."""
        with tempfile.TemporaryDirectory(prefix="ish-stream-zsh-") as directory:
            root = Path(directory)
            (root / ".zshrc").write_text(
                "PS1='READY> '\nPS2='MORE> '\nHISTFILE=/dev/null\n"
                "unsetopt promptcr promptsp\n" + config
            )
            environment = setup(root) if setup else None
            shell = Shell(
                root,
                enabled=enabled,
                popup=popup,
                environment=environment,
                script_dir=script_dir,
            )
            try:
                yield root, shell
            finally:
                shell.close()

    def events(self, output):
        """Decode independent literal wire records and their increasing identity."""
        return [(int(serial), kind.decode()) for serial, kind in MARKER.findall(output)]

    def test_external_script_edits_apply_to_the_next_session(self):
        """A new shell loads the edited text while the Python adapter stays loaded."""
        bundled = Path(__file__).resolve().parents[1] / "src/ish/stream/integrations"
        with tempfile.TemporaryDirectory(prefix="ish-zsh-scripts-") as directory:
            scripts = Path(directory, "scripts")
            shutil.copytree(bundled, scripts)
            path = scripts / "zsh.zsh"
            header, source = path.read_text().split("\n", 1)
            for value in ("FIRST", "SECOND"):
                path.write_text(
                    header
                    + f"\nbuiltin print -r -- {value} > external-result\n"
                    + source
                )
                with self.session(script_dir=scripts) as (root, shell):
                    self.assertEqual(
                        (root / "external-result").read_text(), value + "\n"
                    )
                    shell.submit("print native > result\n")
                    self.assertEqual((root / "result").read_text(), "native\n")

    def test_primary_hides_before_execution_and_returns_after_empty_error(self):
        """Native init/finish boundaries survive blank and invalid submissions."""
        with self.session() as (root, shell):
            self.assertEqual(self.events(shell.output), [(1, "P")])
            for command in ("print RESULT\n", "\n", ")\n"):
                output = shell.submit(command)
                events = self.events(output)
                self.assertEqual([kind for _, kind in events], ["H", "P"])
                self.assertLess(events[0][0], events[1][0])
                if command.startswith("print"):
                    self.assertLess(
                        output.index(b";H\x07"), output.index(b"RESULT\r\n")
                    )
            shell.submit("print -r -- $ZSH_VERSION > version\n")
            self.assertTrue((root / "version").read_text().strip())

    def test_preexec_precmd_arrays_status_and_zle_hooks_match_native(self):
        """Existing hooks keep invocation order, status, widget name and context."""
        config = r"""
precmd() { local old=$?; print -r -- "PRE:$old" >> hooks; return $old; }
preexec() { local old=$?; print -r -- "EXEC:$old:$1" >> hooks; return $old; }
other_precmd() { local old=$?; print -r -- "ARRAY:$old" >> hooks; return $old; }
precmd_functions=(other_precmd)
user_init() { local old=$?; print -r -- "INIT:$old:$WIDGET:$CONTEXT" >> hooks; return $old; }
user_finish() { local old=$?; print -r -- "FINISH:$old:$WIDGET:$CONTEXT" >> hooks; return $old; }
zle -N zle-line-init user_init
zle -N zle-line-finish user_finish
TRAPINT() { print caught >> interrupts; return 130; }
"""
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, config=config) as (root, shell):
                shell.submit("/bin/false\n")
                shell.submit("print -r -- $? > status\n")
                shell.submit("\n")
                shell.submit("print -r -- $functions[TRAPINT] > trap\n")
                results.append(
                    tuple(
                        (root / name).read_bytes()
                        for name in ("hooks", "status", "trap")
                    )
                )
        self.assertEqual(*results)
        self.assertEqual(results[0][1], b"1\n")

    def test_zdotdir_changes_in_zshenv_reach_the_user_rc(self):
        """The user's env file and redirected rc see native ZDOTDIR values."""

        def setup(root):
            target = root / "config"
            target.mkdir()
            (root / ".zshenv").write_text(
                'print -r -- "${ZDOTDIR-unset}" > env-seen\n'
                'export ZDOTDIR="$HOME/config"\n'
            )
            (target / ".zshrc").write_text(
                (root / ".zshrc").read_text() + 'print -r -- "$ZDOTDIR" > rc-seen\n'
            )
            return {}

        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, setup=setup) as (root, shell):
                shell.submit('print -r -- "$ZDOTDIR" > final-dir\n')
                results.append(
                    tuple(
                        (root / name).read_text().replace(str(root), "<HOME>")
                        for name in ("env-seen", "rc-seen", "final-dir")
                    )
                )
        self.assertEqual(*results)
        self.assertEqual(results[0], ("unset\n", "<HOME>/config\n", "<HOME>/config\n"))

    def test_popup_identity_buffer_cursor_and_prior_status(self):
        """The native widget blocks on its exact socket request without editing input."""
        with self.session() as (root, shell):
            shell.submit("/bin/false\n")
            shell.output.clear()
            shell.send(b"print -r -- ac\x1b[D" + F2)
            shell.until(lambda: b";B\x07" in shell.output)
            connection, _ = shell.listener.accept()
            with connection:
                payload = json.loads(connection.recv(1024))
                serial = self.events(shell.output)[-1][0]
                self.assertEqual(payload, {"nonce": NONCE, "serial": serial})
                self.assertNotIn(b";P\x07", shell.output)
                connection.sendall(b"done\n")
            shell.until(lambda: b";P\x07" in shell.output)
            shell.submit(b"b\x05 > result\n")
            self.assertEqual((root / "result").read_bytes(), b"abc\n")
            # Opening a second popup must not reset the previous command status.
            shell.submit("/bin/false\n")
            shell.output.clear()
            shell.send(F2)
            connection, _ = shell.listener.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(b"cancel\n")
            shell.until(lambda: b";P\x07" in shell.output)
            shell.submit("print -r -- $? > status\n")
            self.assertEqual((root / "status").read_bytes(), b"1\n")

    def test_continuation_and_heredoc_restore_native_f2_macro(self):
        """Original F2 string bindings remain native in continuation and heredocs."""
        with self.session(config=r"bindkey -s '\eOQ' DATA" + "\n") as (root, shell):
            shell.output.clear()
            shell.send(b"cat > body << END\r")
            shell.until(lambda: MORE in shell.output)
            self.assertNotIn(b";P\x07", shell.output)
            shell.send(F2 + " 한글\nish_exit\npython\nEND\n".encode())
            shell.until(shell.primary)
            self.assertNotIn(b";B\x07", shell.output)
            self.assertEqual(
                (root / "body").read_text(), "DATA 한글\nish_exit\npython\n"
            )
            shell.output.clear()
            shell.send(b'print -r -- "open\r')
            shell.until(lambda: MORE in shell.output)
            shell.send(F2 + b'close" > continuation\n')
            shell.until(shell.primary)
            self.assertEqual((root / "continuation").read_text(), "open\nDATAclose\n")

    def test_user_widget_nested_alias_and_dynamic_install_keep_native_stdin(self):
        """Native user widgets consume every raw byte before the primary returns."""
        config = r"""
reader() {
    local old=$?
    print -r -- "$old:$WIDGET:$BUFFER:$CURSOR" >> widget-log
    /usr/bin/python3 "$HOME/reader.py"
    return $old
}
outer() { zle reader; }
zle -N reader
zle -N outer
zle -A reader alias-reader
bindkey '\eOR' reader
bindkey '\eOS' outer
bindkey '\e[15~' alias-reader
"""
        with self.session(config=config) as (root, shell):
            (root / "reader.py").write_text(READER)
            shell.submit("/bin/false\n")
            for key in (b"\x1bOR", b"\x1bOS", b"\x1b[15~"):
                (root / "ready").unlink(missing_ok=True)
                shell.output.clear()
                shell.send(b"print -r -- ac\x1b[D" + key)
                shell.until((root / "ready").exists)
                self.assertIn(b";H\x07", shell.output)
                self.assertNotIn(b";P\x07", shell.output)
                shell.send(F2 + b"END")
                shell.until(lambda: b";P\x07" in shell.output)
                self.assertEqual((root / "input").read_bytes(), F2 + b"END")
                shell.submit(b"b\x05 > result\n")
                self.assertEqual((root / "result").read_bytes(), b"abc\n")
            self.assertIn(
                "1:reader:print -r -- ac:13", (root / "widget-log").read_text()
            )
            shell.submit(
                "zle -N dynamic-reader reader; bindkey '\\e[17~' dynamic-reader\n"
            )
            (root / "ready").unlink()
            shell.output.clear()
            shell.send(b"\x1b[17~")
            shell.until((root / "ready").exists)
            shell.send(F2 + b"END")
            shell.until(lambda: b";P\x07" in shell.output)
            self.assertEqual((root / "input").read_bytes(), F2 + b"END")

    def test_builtin_completion_keeps_its_native_program_input(self):
        """Builtin completion may execute user code which reads the controlling TTY."""
        config = r"""
autoload -Uz compinit
compinit -D
native_complete() { /usr/bin/python3 "$HOME/reader.py"; compadd completion; }
compdef native_complete completion_command
"""
        with self.session(config=config) as (root, shell):
            (root / "reader.py").write_text(READER)
            shell.output.clear()
            shell.send(b"completion_command \t")
            shell.until((root / "ready").exists)
            self.assertIn(b";H\x07", shell.output)
            self.assertNotIn(b";P\x07", shell.output)
            shell.send(F2 + b"END")
            shell.until(lambda: b";P\x07" in shell.output)
            self.assertEqual((root / "input").read_bytes(), F2 + b"END")

    def test_interrupt_returns_to_primary_and_native_status(self):
        """Ctrl+C cancels native continuation and exposes the shell's status."""
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled) as (root, shell):
                shell.output.clear()
                shell.send(b"for item in one; do\r")
                shell.until(
                    lambda: (
                        MORE in shell.output
                        and b"\x1b[?2004h" in shell.output[shell.output.rfind(MORE) :]
                    )
                )
                shell.send(b"\x03")
                shell.until(shell.primary)
                shell.submit("print -r -- $? > status\n")
                results.append((root / "status").read_bytes())
        self.assertEqual(*results)
        self.assertEqual(results[0], b"130\n")

    def test_new_init_revokes_aborted_primary_before_a_user_hook_reads(self):
        """A native send-break cannot leave UI eligible during the next user hook."""
        config = r"""
user_init() {
    local old=$?
    if [[ -e "$HOME/arm" ]]; then /usr/bin/python3 "$HOME/reader.py"; fi
    return $old
}
zle -N zle-line-init user_init
"""
        with self.session(config=config) as (root, shell):
            (root / "reader.py").write_text(READER)
            (root / "arm").write_text("armed")
            shell.output.clear()
            shell.send(b"\x07")
            shell.until((root / "ready").exists)
            self.assertIn(b";H\x07", shell.output)
            self.assertNotIn(b";P\x07", shell.output)
            shell.send(F2 + b"END")
            shell.until(lambda: b";P\x07" in shell.output)
            self.assertEqual((root / "input").read_bytes(), F2 + b"END")

    def test_array_and_split_options_keep_native_command_status(self):
        """Common array/splitting options do not change private widget operations."""
        for option in ("ksharrays", "shwordsplit", "errreturn", "nounset"):
            results = []
            for enabled in (False, True):
                with self.session(
                    enabled=enabled, config="setopt " + option + "\n"
                ) as (root, shell):
                    shell.submit("/bin/false\n")
                    shell.submit("print -r -- $? > status\n")
                    results.append((root / "status").read_bytes())
            self.assertEqual(results, [b"1\n", b"1\n"])

    def test_errreturn_preserves_user_hook_status_and_widget_early_return(self):
        """Restoring a failed status must not bypass hooks or disable ERR_RETURN."""
        config = r"""
setopt errreturn
user_init() {
    local old=$?
    print -r -- "$old:$options[errreturn]" >> init-log
    return 0
}
failing_widget() {
    print -r -- "$WIDGET:$options[errreturn]" >> widget-log
    /bin/false
    print unexpected >> widget-log
}
zle -N zle-line-init user_init
zle -N failing-widget failing_widget
bindkey '\eOR' failing-widget
"""
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, config=config) as (root, shell):
                shell.submit("/bin/false\n")
                shell.output.clear()
                shell.send(b"\x1bOR")
                shell.until((root / "widget-log").exists)
                shell.submit("\n")
                # The previous widget's P marker and the next prompt text may
                # arrive before this submission's user init hook runs. Wait for
                # its actual file result, not an unrelated redraw/marker pair.
                shell.until(lambda: (root / "init-log").read_bytes().count(b"\n") >= 3)
                results.append(
                    (
                        (root / "init-log").read_bytes(),
                        (root / "widget-log").read_bytes(),
                    )
                )
        self.assertEqual(
            results,
            [(b"0:on\n1:on\n1:on\n", b"failing-widget:on\n")] * 2,
        )


if __name__ == "__main__":
    unittest.main()
