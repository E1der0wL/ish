"""Observe Bash parser boundaries and the existing input-return ACK in a real PTY.

These are shell protocol experiments, not evidence of live PTK ownership.
"""

import contextlib
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ish.shell.adapter.bash_edit import install
from ish.shell.constants import BASH_INTEGRATION_SCRIPT, FORWARD_BINARY
from ish.shell.integration import build_binary, install_scripts
from ish.shell.protocol import FrameDecoder

DEADLINE = 8
TOKEN = "a" * 32
VERSION_FIXTURE = """
_ish_prompt_id=0
_ish_shell_exit_code=0
_ish_update() { _ish_prompt_id=$((_ish_prompt_id + 1)); }
_ish_forward() { :; }
_ish_precmd() {
    _ish_update "$_ish_shell_exit_code"
    _ish_forward
}
PROMPT_COMMAND=(_ish_precmd)
"""


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class BashVersionPolicyTests(unittest.TestCase):
    """Test capability policy inputs separately from actual shell-version compatibility."""

    def test_minimum_version_bounds_the_reported_capability(self):
        """An isolated version fixture must not advertise unverified older Bash releases."""
        with tempfile.TemporaryDirectory(prefix="ish-bash-version-") as directory:
            root = Path(directory)
            for version, enabled in (
                ((5, 2, 99), 0),
                ((5, 3, 8), 0),
                ((5, 3, 9), 1),
                ((5, 4, 0), 1),
            ):
                with self.subTest(version=version):
                    source = install(
                        VERSION_FIXTURE, submission_path=root / "identity", token=TOKEN
                    )
                    # BASH_VERSINFO is readonly. Substitute only its input in
                    # this fixture; this is not an execution test on older Bash.
                    source = source.replace("BASH_VERSINFO", "_ish_test_version")
                    source += (
                        f"\n_ish_test_version=({' '.join(map(str, version))})\n"
                        + f"_ish_pipe={shlex.quote(str(root / 'mode'))}\n"
                        + "_ish_precmd\n"
                    )
                    result = subprocess.run(
                        [shutil.which("bash"), "--noprofile", "--norc", "-c", source],
                        capture_output=True,
                        timeout=DEADLINE,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertEqual(
                        (root / "mode").read_bytes(),
                        f"\x01edit_mode\x1f1;{TOKEN};1;{enabled}\x04".encode(),
                    )


class BashBoundary:
    """Keep observations separate from the parent's explicit write and ACK decisions."""

    def __init__(self, root, extra_rc=""):
        """Install the production primary helper and experimental parser notifications."""
        self.root = root
        self.output = bytearray()
        self.events = []
        self.records = []
        self.returned = bytearray()
        self.decoder = FrameDecoder()
        self.fds = {}
        for name in ("state", "returned", "ack"):
            path = root / name
            os.mkfifo(path)
            self.fds[name] = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.identity = root / "identity"
        self.identity.write_text("0\n")
        runtime = root / "runtime"
        if not build_binary(directory=runtime):
            raise RuntimeError("Could not build the input-return helper")
        install_scripts(
            directory=runtime,
            forward_path=runtime / FORWARD_BINARY,
            input_ack_path=root / "ack",
        )
        script = runtime / BASH_INTEGRATION_SCRIPT
        script.write_text(
            install(script.read_text(), submission_path=self.identity, token=TOKEN)
        )
        rc = root / "bashrc"
        rc.write_text(
            "PS1='READY> '\n"
            + extra_rc
            + "source "
            + " ".join(
                shlex.quote(str(path))
                for path in (
                    runtime / BASH_INTEGRATION_SCRIPT,
                    root / "state",
                    root / "returned",
                )
            )
            + "\n"
        )
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            os.execve(
                shutil.which("bash"),
                ["bash", "--noprofile", "--rcfile", str(rc), "--noediting", "-i"],
                {
                    "HOME": str(root),
                    "PATH": "/usr/bin:/bin",
                    "TERM": "xterm",
                    "LANG": "C.UTF-8",
                },
            )

    def pump(self):
        """Collect exact FIFO reports and terminal bytes without authorizing input."""
        readable, _, _ = select.select(
            [self.fd, *(fd for name, fd in self.fds.items() if name != "ack")],
            [],
            [],
            0.05,
        )
        for fd in readable:
            try:
                data = os.read(fd, 65536)
            except OSError:
                continue
            if fd == self.fd:
                self.output.extend(data)
            elif fd == self.fds["state"]:
                for category, body in self.decoder.feed(data):
                    self.records.append((category, body))
                    if category == "edit":
                        version, token, submission, request, kind = body.decode().split(
                            ";"
                        )
                        if (version, token) != ("1", TOKEN):
                            raise AssertionError("Unexpected event identity")
                        self.events.append((submission, request, kind))
            elif fd == self.fds["returned"]:
                self.returned.extend(data)

    def require(self, predicate):
        """Time only bounds a failed experiment; predicates use wire or file evidence."""
        deadline = time.monotonic() + DEADLINE
        while not predicate() and time.monotonic() < deadline:
            self.pump()
        if not predicate():
            raise AssertionError(
                (self.events, self.records, bytes(self.output[-3000:]))
            )

    def primary(self, identity):
        """Stop writes before acknowledging the production helper's input return."""
        value = str(identity).encode()
        self.require(lambda: ("prompt_id", value) in self.records)
        os.write(self.fds["ack"], value + b"\n")
        self.require(lambda: ("input_done", value) in self.records)

    def submit(self, identity, text):
        """Bind the parser's next event to the submission before writing its first line."""
        self.identity.write_text(f"{identity}\n")
        os.write(self.fd, text.encode())

    def close(self):
        """Reap the native shell and close only descriptors owned by this fixture."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        for fd in self.fds.values():
            os.close(fd)


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class BashProtocolTests(unittest.TestCase):
    """Assert parser event order and the final consumer of delayed native bytes."""

    def setUp(self):
        """Use a new controlling terminal and private protocol files per experiment."""
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ish-bash-protocol-")
        )
        self.root = Path(directory)
        self.shell = BashBoundary(self.root)
        self.addCleanup(self.shell.close)
        self.shell.primary(1)

    def test_continuation_chain_orders_requests_before_raw(self):
        """Only the final syntax reply enables execution of the collected command."""
        shell = self.shell
        shell.submit(11, "for item in one two; do\n")
        shell.require(lambda: len(shell.events) == 1)
        self.assertEqual(shell.events, [("11", "1", "C")])
        os.write(shell.fd, b'printf "%s\\n" "$item" >> result\n')
        shell.require(lambda: len(shell.events) == 2)
        self.assertEqual(shell.events[-1], ("11", "2", "C"))
        self.assertFalse((self.root / "result").exists())
        os.write(shell.fd, b"done\n")
        shell.require(lambda: len(shell.events) == 3)
        self.assertEqual(shell.events[-1], ("11", "3", "R"))
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), "one\ntwo\n")

    def test_late_raw_input_is_returned_before_the_next_parser_read(self):
        """The existing primary ACK gate catches data sent after a short command ends."""
        shell = self.shell
        shell.submit(12, "true\n")
        # Deliberately postpone handling R until the next context frame proves
        # the command has already completed and the primary helper is waiting.
        shell.require(lambda: ("prompt_id", b"2") in shell.records)
        self.assertEqual(shell.events, [("12", "1", "R")])
        late = b"printf escaped > escaped\n"
        os.write(shell.fd, late)
        shell.require(lambda: bytes(shell.returned) == late)
        self.assertFalse((self.root / "escaped").exists())
        shell.primary(2)
        shell.submit(13, "printf next > next\n")
        shell.primary(3)
        self.assertFalse((self.root / "escaped").exists())
        self.assertEqual((self.root / "next").read_text(), "next")

    def test_read_uses_native_stdin_after_raw_notification(self):
        """A builtin receives its stdin as data, without another syntax request."""
        shell = self.shell
        shell.submit(14, 'read -r value; printf "%s" "$value" > result\n')
        shell.require(lambda: len(shell.events) == 1)
        self.assertEqual(shell.events, [("14", "1", "R")])
        os.write(shell.fd, "  한 글 도구 이름  \n".encode())
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), "한 글 도구 이름")
        self.assertEqual(len(shell.events), 1)

    def test_source_stdin_is_execution_input_not_a_new_editor_request(self):
        """An executing source command owns both its syntax stream and ordinary stdin."""
        shell = self.shell
        shell.submit(15, "source /dev/stdin\n")
        shell.require(lambda: len(shell.events) == 1)
        self.assertEqual(shell.events, [("15", "1", "R")])
        os.write(shell.fd, b"if true; then\nprintf sourced > result\nfi\n\x04")
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), "sourced")
        self.assertEqual(shell.events, [("15", "1", "R")])

    def test_heredoc_preserves_spaces_unicode_and_command_names(self):
        """Every heredoc line is syntax input and its exact bytes reach the file."""
        shell = self.shell
        shell.submit(16, "cat <<'END' > result\n")
        shell.require(lambda: len(shell.events) == 1)
        self.assertEqual(shell.events[-1], ("16", "1", "C"))
        body = "  probe 한 글  \nish_exit\n"
        for index, line in enumerate(body.splitlines(keepends=True), 2):
            os.write(shell.fd, line.encode())
            shell.require(lambda index=index: len(shell.events) == index)
            self.assertEqual(shell.events[-1], ("16", str(index), "C"))
        os.write(shell.fd, b"END\n")
        shell.require(lambda: len(shell.events) == 4)
        self.assertEqual(shell.events[-1], ("16", "4", "R"))
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), body)

    def test_blank_and_error_return_without_raw_permission(self):
        """No command execution or native input permission is invented for empty/error input."""
        shell = self.shell
        shell.submit(17, "\n")
        shell.primary(2)
        self.assertEqual(shell.events, [])
        shell.submit(18, ")\n")
        shell.primary(3)
        self.assertEqual(shell.events, [])
        self.assertIn(("exitcode", b"2"), shell.records)

    def test_interrupt_retires_the_old_parser_chain(self):
        """Cancelling a syntax read produces a new primary without executing its body."""
        shell = self.shell
        shell.submit(19, "for item in one; do\n")
        shell.require(lambda: len(shell.events) == 1)
        os.write(shell.fd, b"\x03")
        shell.primary(2)
        shell.submit(20, "printf intact > result\n")
        shell.primary(3)
        self.assertEqual(shell.events, [("19", "1", "C"), ("20", "1", "R")])
        self.assertEqual((self.root / "result").read_text(), "intact")

    def test_eof_finishes_a_heredoc_without_consuming_the_next_command(self):
        """Native heredoc EOF completes the body and returns through the ACK boundary."""
        shell = self.shell
        shell.submit(21, "cat <<'END' > result\n")
        shell.require(lambda: len(shell.events) == 1)
        os.write(shell.fd, "한 글\n".encode())
        shell.require(lambda: len(shell.events) == 2)
        os.write(shell.fd, b"\x04")
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), "한 글\n")
        self.assertEqual(
            shell.events, [("21", "1", "C"), ("21", "2", "C"), ("21", "3", "R")]
        )

    def test_command_substitution_and_external_program_keep_native_stdin(self):
        """Execution-time reads never become command editor requests."""
        shell = self.shell
        shell.submit(
            22,
            'value=$(IFS= read -r line; printf "%s" "$line"); printf "%s" "$value" > result\n',
        )
        shell.require(lambda: len(shell.events) == 1)
        os.write(shell.fd, "  probe 한 글  \n".encode())
        shell.primary(2)
        self.assertEqual((self.root / "result").read_text(), "  probe 한 글  ")
        shell.submit(23, "cat > external\n")
        shell.require(lambda: len(shell.events) == 2)
        os.write(shell.fd, b"ish_exit\n\x04")
        shell.primary(3)
        self.assertEqual((self.root / "external").read_text(), "ish_exit\n")
        self.assertEqual(shell.events, [("22", "1", "R"), ("23", "1", "R")])

    def test_promptvars_opt_out_reports_native_mode(self):
        """Disabling prompt substitutions preserves the native continuation path."""
        shell = self.shell
        shell.submit(24, "shopt -u promptvars\n")
        shell.primary(2)
        self.assertIn(("edit_mode", f"1;{TOKEN};2;0".encode()), shell.records)
        shell.submit(25, "for item in one; do\nprintf native > result\ndone\n")
        shell.primary(3)
        self.assertEqual((self.root / "result").read_text(), "native")
        self.assertEqual(shell.events, [("24", "1", "R")])

    def test_later_prompt_hook_disables_semantic_mode(self):
        """A hook after ish cannot silently replace prompts while capability remains on."""
        shell = self.shell
        shell.submit(26, "PROMPT_COMMAND+=('PS0=custom')\n")
        shell.primary(2)
        self.assertIn(("edit_mode", f"1;{TOKEN};2;0".encode()), shell.records)
        shell.submit(27, "printf native > result\n")
        shell.primary(3)
        self.assertEqual((self.root / "result").read_text(), "native")
        self.assertEqual(shell.events, [("26", "1", "R")])

    def test_user_ps0_prompt_command_and_status_survive_wrapping(self):
        """User prompt output and the status observed by hooks and commands stay intact."""
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ish-bash-hooks-")
        )
        root = Path(directory)
        shell = BashBoundary(
            root,
            "PS0='USER:$? '\nPROMPT_COMMAND=('printf \"H:%s\\n\" \"$?\" >> hooks')\n",
        )
        self.addCleanup(shell.close)
        shell.primary(1)
        shell.submit(28, "false\n")
        shell.primary(2)
        shell.submit(29, 'printf "%s" "$?" > result\n')
        shell.primary(3)
        self.assertEqual((root / "result").read_text(), "1")
        self.assertEqual((root / "hooks").read_text(), "H:0\nH:1\nH:0\n")
        self.assertIn(b"USER:1 ", shell.output)


if __name__ == "__main__":
    unittest.main()
