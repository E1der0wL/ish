"""Observe Bash prompt notifications and native Readline grants in a real PTY."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import tempfile
import termios
import time
import unittest
from pathlib import Path

import pyte

from ish.stream.bundled.stream_bash.stream_bash import build
from stream_fixture import shell_executable
from stream_state import INPUT_BYTES, failure, snapshot

BASH = shutil.which(shell_executable("bash"))
NONCE = "bash-stream-test-0123456789abcdef"
MARKER = re.compile(rb"\x1b\]777;ish;" + NONCE.encode() + rb";(\d+);([PBH])\x07")
PROMPT = b"READY> "
MORE = b"MORE> "
F2 = b"\x1bOQ"
DEADLINE = 8


class Shell:
    """Drive actual shell input, observing output and independent file results."""

    def __init__(self, root, *, enabled, popup, script_dir=None):
        """Start a controlling PTY using a private HOME and optional adapter."""
        self.root = root
        self.enabled = enabled
        self.pid = self.fd = self.status = None
        self.listener = None
        self.cleanup_started = False
        self.before_cleanup = None
        self.sent = bytearray()
        self.pty_end = None
        self.output = bytearray()
        self.prompt_after = 0
        try:
            self._start(root, enabled=enabled, popup=popup, script_dir=script_dir)
        except BaseException:
            self.close()
            raise

    def _start(self, root, *, enabled, popup, script_dir):
        env = dict(os.environ, HOME=str(root), TERM="xterm-256color", LANG="C.UTF-8")
        if popup:
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(str(root / "control"))
            self.listener.listen()
            self.listener.settimeout(DEADLINE)
        if enabled:
            argv, env = build(
                [BASH, "-i"],
                env,
                root,
                NONCE,
                str(root / "control") if popup else None,
                script_dir=script_dir,
            )
        else:
            argv = [BASH, "--noprofile", "--rcfile", str(root / ".bashrc"), "-i"]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                termios.tcsetwinsize(0, (24, 80))
                os.chdir(root)
                os.execve(BASH, argv, env)
            except BaseException as error:
                os.write(2, f"test child exec failed: {error}\n".encode())
                os._exit(127)
        os.set_blocking(self.fd, False)
        self.until(lambda: PROMPT in self.output)

    def state(self):
        """Observe the child before cleanup without stealing its exit status."""
        return snapshot(
            self.pid, self.fd, returncode=self.status, cleanup=self.cleanup_started
        )

    def diagnostic(self, reason):
        """Report a failed observation without equating PTY closure with exit."""
        return failure(
            reason, self.state(), self.output, self.sent, pty_end=self.pty_end
        )

    def until(self, predicate):
        """Wait for explicit observations; time only bounds a failing test."""
        deadline = time.monotonic() + DEADLINE
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(
                    self.diagnostic("Timed out waiting for observation")
                )
            if not select.select([self.fd], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
                self.pty_end = {"kind": "EIO", "state": self.state()}
            if not data:
                if self.pty_end is None:
                    self.pty_end = {"kind": "EOF", "state": self.state()}
                raise AssertionError(self.diagnostic("PTY ended before observation"))
            self.output.extend(data)

    def send(self, data):
        """Send literal bytes or UTF-8 text to the native shell's PTY."""
        data = data.encode() if isinstance(data, str) else data
        self.sent.extend(data[-INPUT_BYTES:])
        del self.sent[:-INPUT_BYTES]
        try:
            os.write(self.fd, data)
        except OSError as error:
            raise AssertionError(
                self.diagnostic(f"PTY write failed: {error}")
            ) from error

    def primary(self, *, marked=True):
        """Distinguish an actual new prompt from Readline's same-line redisplay."""
        if not MARKER.sub(b"", self.output).endswith(PROMPT):
            return False
        if not self.enabled or not marked:
            return True
        records = [(int(serial), kind) for serial, kind in MARKER.findall(self.output)]
        if not records:
            return False
        serial, kind = max(records)
        return serial > self.prompt_after and kind == b"P"

    def clear(self):
        """Start an observation after all identities already received in this PTY."""
        self.prompt_after = max(
            [self.prompt_after]
            + [int(serial) for serial, _ in MARKER.findall(self.output)]
        )
        self.output.clear()

    def submit(self, text, *, marked=True):
        """Observe the next primary marker after a complete native command."""
        self.clear()
        self.send(text)
        self.until(lambda: self.primary(marked=marked))
        return bytes(self.output)

    def close(self):
        """Reap only this isolated test shell and close its private listener."""
        if self.cleanup_started:
            return
        self.before_cleanup = self.state()
        self.cleanup_started = True
        try:
            if self.pid is not None:
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status = os.waitpid(self.pid, 0)
                self.status = os.waitstatus_to_exitcode(status)
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.listener:
                self.listener.close()


@unittest.skipUnless(BASH, "Bash is required")
class BashTests(unittest.TestCase):
    """Keep adapter observations separate from the full PTK CLI regressions."""

    maxDiff = None

    @contextlib.contextmanager
    def session(self, *, enabled=True, popup=True, config="", script_dir=None):
        """Apply the same private startup configuration to native and wrapped Bash."""
        with tempfile.TemporaryDirectory(prefix="ish-stream-bash-") as directory:
            root = Path(directory)
            (root / ".bashrc").write_text(
                "PS1='READY> '\nPS2='MORE> '\nPS0=''\n"
                "HISTFILE=/dev/null\nbind 'set enable-bracketed-paste off'\n" + config
            )
            shell = Shell(root, enabled=enabled, popup=popup, script_dir=script_dir)
            try:
                yield root, shell
            finally:
                shell.close()

    def events(self, output, *, after=0):
        """Observe literal records newer than an explicitly observed prompt identity."""
        return [
            (int(serial), kind.decode())
            for serial, kind in MARKER.findall(output)
            if int(serial) > after
        ]

    def test_ordered_primary_and_hide_with_empty_error_and_output(self):
        """Native parser boundaries precede unchanged output and user prompts."""
        with self.session() as (_, shell):
            self.assertEqual(self.events(shell.output), [(1, "P")])
            self.assertEqual(
                self.events(shell.submit("echo RESULT\n")), [(2, "H"), (3, "P")]
            )
            self.assertEqual(self.events(shell.submit("\n")), [(4, "H"), (5, "P")])
            error = shell.submit(";\n")
            self.assertIn(b"syntax error", error)
            self.assertEqual(self.events(error), [(6, "H"), (7, "P")])

    def test_prompt_commands_scalar_array_status_and_debug_are_preserved(self):
        """Existing hook code/type, failure status and DEBUG definition stay intact."""
        for prompt_command in (
            'PROMPT_COMMAND=\'printf "PC:%s\\n" "$?" >> hooks\'\n',
            "PROMPT_COMMAND=('printf \"FIRST:%s\\n\" \"$?\" >> hooks' 'printf LAST\\\\n >> hooks')\n",
        ):
            results = []
            for enabled in (False, True):
                with self.session(
                    enabled=enabled,
                    config=prompt_command
                    + "trap 'case $BASH_COMMAND in /bin/false) printf DEBUG\\\\n >> debug;; esac' DEBUG\n",
                ) as (root, shell):
                    shell.submit("/bin/false\n")
                    shell.submit("printf '%s\\n' \"$?\" > status\n")
                    shell.submit(
                        "declare -p PROMPT_COMMAND > declaration; trap -p DEBUG > trap\n"
                    )
                    declaration = (root / "declaration").read_bytes()
                    expected_kind = (
                        b"declare -a "
                        if "PROMPT_COMMAND=(" in prompt_command
                        else b"declare -- "
                    )
                    self.assertTrue(declaration.startswith(expected_kind), declaration)
                    results.append(
                        tuple(
                            (root / name).read_bytes()
                            for name in ("hooks", "status", "trap", "debug")
                        )
                    )
            self.assertEqual(*results)

    def test_user_prompt_text_and_status_match_native(self):
        """PS0/PS1/PS2 expansions retain their original text and status values."""
        results = []
        for enabled in (False, True):
            with self.session(
                enabled=enabled, config="PS0='START:$? '\nPS1='STATUS:$? READY> '\n"
            ) as (_, shell):
                initial = bytes(shell.output)
                output = shell.submit("/bin/false\n")
                shell.clear()
                shell.send("for word in one; do\n")
                shell.until(lambda: MORE in shell.output)
                continuation = bytes(shell.output)
                result = shell.submit("echo $word; done\n")
                screen = pyte.Screen(80, 24)
                terminal = pyte.ByteStream(screen)
                terminal.feed(MARKER.sub(b"", initial + output + continuation + result))
                results.append((screen.display, screen.cursor.x, screen.cursor.y))
        self.assertEqual(*results)

    def test_continuation_and_heredoc_reserve_f2_without_popup(self):
        """Secondary editing consumes the reserved key and preserves body text."""
        config = r"""bind '"\eOQ":"NATIVE"'
"""
        with self.session(config=config) as (root, shell):
            shell.clear()
            shell.send("cat > body << END\n")
            shell.until(lambda: MORE in shell.output)
            self.assertEqual([kind for _, kind in self.events(shell.output)], ["H"])
            shell.send(F2 + " 한글 python\nEND\n".encode())
            shell.until(lambda: (root / "body").exists() and shell.primary())
            self.assertEqual((root / "body").read_text(), " 한글 python\n")
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])
            self.assertFalse(select.select([shell.listener], [], [], 0)[0])

    def test_read_e_reserves_f2_and_preserves_native_input(self):
        """Readline consumes reserved F2 without granting a popup during execution."""
        config = r"""bind '"\eOQ":"NATIVE"'
"""
        with self.session(config=config) as (root, shell):
            shell.clear()
            shell.send(
                "read -e -p 'INPUT> ' value; printf '%s\\n' \"$value\" > read-result\n"
            )
            shell.until(
                lambda: b"INPUT> " in shell.output and b";H\x07" in shell.output
            )
            shell.send(F2 + b" reader\n")
            shell.until(lambda: (root / "read-result").exists() and shell.primary())
            self.assertEqual((root / "read-result").read_text(), "reader\n")
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])

    def test_popup_grant_preserves_readline_buffer_and_cursor(self):
        """Only a native widget grants the matching socket and leaves editing intact."""
        with self.session() as (root, shell):
            shell.clear()
            shell.send(b"printf '%s' abcd > result" + b"\x1b[D" * 11 + F2)
            connection, _ = shell.listener.accept()
            with connection:
                payload = json.loads(connection.recv(1024))
                shell.until(lambda: b";B\x07" in shell.output)
                self.assertEqual(self.events(shell.output), [(2, "B")])
                self.assertEqual(payload, {"nonce": NONCE, "serial": 2})
                self.assertFalse((root / "result").exists())
                connection.sendall(b"done\n")
            shell.until(lambda: b";3;P\x07" in shell.output)
            shell.send(b"XY\n")
            shell.until(
                lambda: (root / "result").exists() and b";5;P\x07" in shell.output
            )
            self.assertEqual((root / "result").read_text(), "abXYcd")

    def test_replaced_secondary_prompt_disables_popup_capability(self):
        """Replacing a required boundary revokes popup capability for reserved F2."""
        with self.session(
            config=r"""bind '"\eOQ":"NATIVE"'
"""
        ) as (root, shell):
            output = shell.submit("PS2='custom> '\n", marked=False)
            self.assertNotIn("P", [kind for _, kind in self.events(output)])
            shell.send(b"echo " + F2 + b" > restored\n")
            shell.until(lambda: (root / "restored").exists())
            self.assertEqual((root / "restored").read_text(), "\n")

    def test_disabled_readline_does_not_advertise_popup(self):
        """Native canonical input has no bind-x boundary and must not receive P."""
        with self.session(config="set +o emacs\n") as (_, shell):
            self.assertNotIn("P", [kind for _, kind in self.events(shell.output)])
            output = shell.submit("echo native\n", marked=False)
            self.assertIn(b"native\r\n", output)
            self.assertNotIn("P", [kind for _, kind in self.events(output)])

    def test_ctrl_c_continuation_returns_to_primary(self):
        """Native cancellation emits a new primary without executing the pending body."""
        with self.session() as (root, shell):
            shell.clear()
            shell.send("for word in one; do\n")
            shell.until(lambda: MORE in shell.output)
            shell.send(b"\x03")
            shell.until(shell.primary)
            self.assertEqual(self.events(shell.output)[-1][1], "P")
            shell.submit("echo alive > result\n")
            self.assertEqual((root / "result").read_text(), "alive\n")

    def test_native_widget_stdin_has_hide_and_primary_boundaries(self):
        """A user's bind-x reader receives F2 bytes while popup capability is hidden."""
        config = r"""native_widget() {
    builtin read -r -p 'WIDGET_INPUT> ' value
    printf '%s' "$value" > widget-input
}
bind -x '"\C-x":native_widget'
"""
        with self.session(config=config) as (root, shell):
            self.assertEqual(self.events(shell.output), [(1, "P")])
            shell.clear()
            shell.send(b"\x18")
            shell.until(lambda: b"WIDGET_INPUT> " in shell.output)
            self.assertEqual(self.events(shell.output), [(2, "H")])
            shell.send(F2 + b"END\n")
            shell.until(
                lambda: (root / "widget-input").exists() and b";3;P\x07" in shell.output
            )
            self.assertEqual((root / "widget-input").read_bytes(), F2 + b"END")
            self.assertFalse(select.select([shell.listener], [], [], 0)[0])

    def test_native_widget_quotes_and_substitutions_execute_once(self):
        """Readline serialization is preserved without evaluating command data at setup."""
        body = (
            "printf '%s' \"$(/bin/sh action.sh)\" > widget-result\n"
            "READLINE_LINE='echo kept > result'\n"
            "READLINE_POINT=9 # trailing comment"
        )
        config = "bind -x " + shlex.quote('"\\C-t":' + body) + "\n"
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, config=config) as (root, shell):
                (root / "action.sh").write_text(
                    "printf 'called\\n' >> calls\n"
                    "printf '%s' 'value ; \"quotes\" \\ backslash'\n"
                )
                self.assertFalse((root / "calls").exists())
                shell.clear()
                shell.send(b"\x14")
                shell.until(lambda: (root / "widget-result").exists())
                shell.send(b"XY\n")
                shell.until(lambda: (root / "result").exists() and shell.primary())
                results.append(
                    tuple(
                        (root / name).read_bytes()
                        for name in ("calls", "widget-result", "result")
                    )
                )
                self.assertEqual(
                    results[-1],
                    (b"called\n", b'value ; "quotes" \\ backslash', b"keptXY\n"),
                )
        self.assertEqual(*results)

    def test_completion_and_expansion_stdin_suspend_decoration(self):
        """Readline callbacks outside PS0 own their stdin, including F2 bytes."""
        config = r"""completion() {
    read -r -p 'COMP_INPUT> ' value
    printf '%s' "$value" > comp-input
    COMPREPLY=(done)
}
complete -F completion fixture
"""
        with self.session(config=config) as (root, shell):
            serial = max(serial for serial, _ in self.events(shell.output))
            shell.clear()
            shell.send(b"fixture \t")
            shell.until(lambda: b"COMP_INPUT> " in shell.output)
            self.assertEqual(
                [kind for _, kind in self.events(shell.output, after=serial)], ["H"]
            )
            shell.send(F2 + b"END\n")
            shell.until(
                lambda: (root / "comp-input").exists() and b"done" in shell.output
            )
            self.assertEqual((root / "comp-input").read_bytes(), F2 + b"END")
            self.assertNotIn(
                "P", [kind for _, kind in self.events(shell.output, after=serial)]
            )
            shell.send(b"\x03")
            shell.until(shell.primary)
            serial = max(serial for serial, _ in self.events(shell.output))
            (root / "reader.sh").write_text(
                "printf 'EXP_INPUT> ' >&2\nread -r value\n"
                "printf '%s' \"$value\" > expansion-input\nprintf done\n"
            )
            shell.clear()
            shell.send(b"echo $(/bin/sh reader.sh)\x1b\x05")
            shell.until(lambda: b"EXP_INPUT> " in shell.output)
            self.assertEqual(
                [kind for _, kind in self.events(shell.output, after=serial)], ["H"]
            )
            shell.send(F2 + b"END\n")
            shell.until(
                lambda: (root / "expansion-input").exists() and b"done" in shell.output
            )
            self.assertEqual((root / "expansion-input").read_bytes(), F2 + b"END")
            self.assertNotIn(
                "P", [kind for _, kind in self.events(shell.output, after=serial)]
            )

    def test_quoted_insert_and_history_search_stay_native(self):
        """Readline's temporary key-reading modes never grant a PTK popup."""
        with self.session() as (root, shell):
            shell.clear()
            shell.send(b"printf '%s' \x16")
            shell.until(lambda: b";H\x07" in shell.output)
            shell.send(F2 + b"END > quoted\n")
            shell.until(lambda: (root / "quoted").exists() and shell.primary())
            self.assertEqual((root / "quoted").read_bytes(), F2 + b"END")
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])
            serial = max(serial for serial, _ in self.events(shell.output))
            shell.clear()
            shell.send(b"\x12")
            shell.until(lambda: b"reverse-i-search" in shell.output)
            self.assertEqual(
                [kind for _, kind in self.events(shell.output, after=serial)], ["H"]
            )
            shell.send(F2 + b"\x03")
            shell.until(shell.primary)
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])
            shell.submit("echo native > after-search\n")
            self.assertEqual((root / "after-search").read_text(), "native\n")

    def test_empty_submission_revokes_popup_before_prompt_hook_stdin(self):
        """A PROMPT_COMMAND reader consumes reserved F2 even when PS0 never runs."""
        config = r"""bind '"\eOQ":"NATIVE"'
calls=0
prompt_hook() {
    calls=$((calls + 1))
    if (( calls == 2 )); then
        read -e -p 'PC_INPUT> ' value
        printf '%s' "$value" > hook-input
    fi
}
PROMPT_COMMAND=prompt_hook
"""
        with self.session(config=config) as (root, shell):
            shell.clear()
            shell.send(b"\n")
            shell.until(lambda: b"PC_INPUT> " in shell.output)
            self.assertEqual([kind for _, kind in self.events(shell.output)], ["H"])
            shell.send(F2 + b" reader\n")
            shell.until(lambda: (root / "hook-input").exists() and shell.primary())
            self.assertEqual((root / "hook-input").read_text(), "reader")
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])

    def test_readonly_and_replaced_prompt_commands_disable_popup(self):
        """An unmaintainable prompt boundary must preserve native operation."""
        with self.session(config="readonly PROMPT_COMMAND=':'\n") as (_, shell):
            self.assertNotIn("P", [kind for _, kind in self.events(shell.output)])
            output = shell.submit("echo native\n", marked=False)
            self.assertIn(b"native\r\n", output)
            self.assertNotIn("P", [kind for _, kind in self.events(output)])
        with self.session(
            config=r"""bind '"\eOQ":"NATIVE"'
"""
        ) as (root, shell):
            output = shell.submit("PROMPT_COMMAND=':'\n", marked=False)
            self.assertNotIn("P", [kind for _, kind in self.events(output)])
            shell.send(b"echo " + F2 + b" > restored\n")
            shell.until(lambda: (root / "restored").exists())
            self.assertEqual((root / "restored").read_text(), "\n")

    def test_vi_insert_and_movement_popup_keep_native_buffer(self):
        """Both native vi keymaps return to the same editable command after F2."""
        for movement in (False, True):
            with (
                self.subTest(movement=movement),
                self.session(config="set -o vi\n") as (root, shell),
            ):
                shell.clear()
                shell.send(b"echo kept > result" + (b"\x1b" if movement else b"") + F2)
                connection, _ = shell.listener.accept()
                with connection:
                    payload = json.loads(connection.recv(1024))
                    self.assertEqual(payload["nonce"], NONCE)
                    connection.sendall(b"done\n")
                shell.send(b"\n")
                shell.until(lambda: (root / "result").exists() and shell.primary())
                self.assertEqual((root / "result").read_text(), "kept\n")

    def test_prompt_expansion_reader_cannot_grant_popup(self):
        """The primary marker follows user prompt expansion and its native reader."""
        config = r"""prompt_reader() {
    if [[ -e prompt-read ]]; then
        read -e -r -p 'PROMPT_INPUT> ' value
        printf '%s' "$value" > prompt-input
    fi
}
PS1='$(prompt_reader)READY> '
"""
        with self.session(config=config) as (root, shell):
            shell.clear()
            shell.send("touch prompt-read\n")
            shell.until(lambda: b"PROMPT_INPUT> " in shell.output)
            self.assertEqual(self.events(shell.output), [(2, "H")])
            shell.send(F2 + b"native\n")
            shell.until(lambda: (root / "prompt-input").exists() and shell.primary())
            self.assertEqual((root / "prompt-input").read_text(), "native")
            self.assertNotIn("B", [kind for _, kind in self.events(shell.output)])
            self.assertFalse(select.select([shell.listener], [], [], 0)[0])

    def test_widget_positional_parameters_match_native(self):
        """A guard does not replace the user's positional parameters with its body."""
        config = r"""set -- 'first value' second
bind -x '"\C-t":printf "%s\n" "$@" > widget-arguments'
"""
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, config=config) as (root, shell):
                shell.send(b"\x14")
                shell.until(
                    lambda: (
                        (root / "widget-arguments").exists()
                        and (root / "widget-arguments").stat().st_size > 0
                    )
                )
                results.append((root / "widget-arguments").read_text())
        self.assertEqual(results, ["first value\nsecond\n"] * 2)

    def test_nested_widget_readline_never_activates_popup(self):
        """Readline nested inside a user widget consumes the reserved key privately."""
        config = r"""native_reader() {
    IFS= read -e -r -p 'NESTED_INPUT> ' value
    printf '%s' "$value" > nested-input
}
bind -x '"\C-t":native_reader'
"""
        with self.session(config=config) as (root, shell):
            shell.clear()
            shell.send(b"\x14")
            shell.until(lambda: b"NESTED_INPUT> " in shell.output)
            shell.send(F2 + b"native\n")
            shell.until(lambda: (root / "nested-input").exists() and shell.primary())
            self.assertEqual((root / "nested-input").read_text(), "native")
            observed = self.events(shell.output, after=1)
            self.assertEqual(observed[0], (2, "H"))
            self.assertEqual(observed[-1][1], "P")
            self.assertNotIn("B", [kind for _, kind in observed])
            self.assertFalse(select.select([shell.listener], [], [], 0)[0])

    def test_external_script_edits_apply_to_the_next_session(self):
        """New sessions read the edited file without reloading Python modules."""
        original = Path(__file__).parents[1] / "src/ish/stream/integrations/bash.sh"
        with tempfile.TemporaryDirectory(prefix="ish-bash-script-") as directory:
            scripts = Path(directory)
            for value in ("first", "changed"):
                (scripts / "bash.sh").write_text(
                    original.read_text().replace(
                        'builtin return "$_ish_stream_status"\n',
                        f"builtin printf '%s' {shlex.quote(value)} > script-result\n"
                        + 'builtin return "$_ish_stream_status"\n',
                    )
                )
                with self.session(script_dir=scripts) as (root, shell):
                    self.assertEqual((root / "script-result").read_text(), value)
                    shell.submit("echo native > native-result\n")
                    self.assertEqual((root / "native-result").read_text(), "native\n")

    def test_unicode_widget_stays_stable_across_prompts(self):
        """Native macro bytes and quoting do not accumulate another wrapper per prompt."""
        body = "printf '%s' '한글 \\ \"quoted\"' > unicode-widget"
        config = "bind -x " + shlex.quote('"\\C-t":' + body) + "\n"
        results = []
        for enabled in (False, True):
            with self.session(enabled=enabled, config=config) as (root, shell):
                for index in range(6):
                    shell.submit(f"echo {index} > iteration\n")
                shell.send(b"\x14")
                shell.until(
                    lambda: (
                        (root / "unicode-widget").exists()
                        and (root / "unicode-widget").stat().st_size > 0
                    )
                )
                results.append((root / "unicode-widget").read_text())
        self.assertEqual(results, ['한글 \\ "quoted"'] * 2)

    def test_literal_nul_and_backslash_keys_are_not_prefix_shadows(self):
        """Prefix shadow normalization never merges a user's distinct key sequence."""
        config = r"""nul_widget() { printf '%s' NUL > nul-key; }
literal_widget() { printf '%s' LITERAL > literal-key; }
bind -x '"\C-t\C-@":nul_widget'
bind -x '"\C-t\\000":literal_widget'
"""
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                self.session(enabled=enabled, config=config) as (root, shell),
            ):
                shell.send(b"\x14\x00")
                shell.until(
                    lambda: (
                        (root / "nul-key").exists()
                        and (root / "nul-key").stat().st_size > 0
                    )
                )
                self.assertEqual((root / "nul-key").read_text(), "NUL")
                shell.send(b"\x14\\000")
                shell.until(
                    lambda: (
                        (root / "literal-key").exists()
                        and (root / "literal-key").stat().st_size > 0
                    )
                )
                self.assertEqual((root / "literal-key").read_text(), "LITERAL")


if __name__ == "__main__":
    unittest.main()
