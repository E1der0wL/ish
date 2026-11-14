"""Observe real zsh input boundaries before enabling semantic PTK ownership."""

import contextlib
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

from ish.shell.adapter import get_adapter
from ish.shell.adapter.scripts import render_scripts
from ish.shell.adapter.zsh_edit import install
from ish.shell.constants import SessionSignals
from ish.shell.integration import TTY_FORWARD
from ish.shell.protocol import FrameDecoder

DEADLINE = 8
TOKEN = "zsh-observation"

PROBE = r"""
unsetopt zle notify promptcr promptsp
setopt promptsubst
exec {probe_fd}> "$HOME/events"
typeset -gi probe_sequence=0
probe_event() {
    local previous=$?
    (( ++probe_sequence ))
    builtin printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$probe_sequence" "$previous" "$ZSH_SUBSHELL" "$ZSH_EVAL_CONTEXT" >&$probe_fd
    return "$previous"
}
probe_math() { probe_event C; (( 0 )); builtin true; }
functions -M probe_math 0 0 probe_math
preexec() { probe_event R; }
precmd() { probe_event P; }
PS1='PRIMARY> '
PS2='$((probe_math()))SECONDARY> '
"""


class ZshPTY:
    """Controlling PTY with separate observations and the production reclaim helper."""

    def __init__(self, root, helper, *, reclaim=True, semantic=False, freeze_c=False):
        """Prepare isolated shell files; no readiness is inferred from terminal text."""
        self.root = root
        self.channels = {}
        for name in ("events", "context", "returned", "ack"):
            os.mkfifo(root / name)
            self.channels[name] = os.open(root / name, os.O_RDWR | os.O_NONBLOCK)
        self.events, self.records = [], []
        self.event_tail = bytearray()
        self.returned, self.output = bytearray(), bytearray()
        self.decoder = FrameDecoder()
        (root / "submission").write_text("0\n")
        rc = PROBE
        if reclaim:
            adapter = get_adapter("zsh")
            sources = render_scripts(
                root,
                signals=SessionSignals(TOKEN),
                forward_path=helper,
                adapter=adapter,
                input_ack_path=root / "ack",
            )
            if semantic:
                sources[adapter.script] = install(
                    sources[adapter.script],
                    submission_path=root / "submission",
                    token=TOKEN,
                )
                if freeze_c:
                    sources[adapter.script] = sources[adapter.script].replace(
                        "    _ish_edit_event C\n",
                        "    _ish_edit_event C\n    builtin kill -STOP $$\n",
                        1,
                    )
            for name, source in sources.items():
                (root / name).write_text(source)
            rc += f'source "{root / adapter.script}" "{root / "context"}" "{root / "returned"}"\n'
            if not semantic:
                rc += "_ish_continuation_ready() { probe_event C; (( 0 )); builtin true; }\n"
        (root / ".zshrc").write_text(rc)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            env = {
                "HOME": str(root),
                "ZDOTDIR": str(root),
                "TERM": "xterm",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            }
            os.execve(shutil.which("zsh"), ["zsh", "-d", "-i"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        try:
            if reclaim:
                self.primary(1)
            else:
                self.wait(lambda: any(event[0] == "P" for event in self.events))
        except BaseException:
            self.close()
            raise
        self.events.clear()

    def pump(self):
        """Collect real bytes from each channel without assigning input permission."""
        fds = [self.fd, *(fd for name, fd in self.channels.items() if name != "ack")]
        for fd in select.select(fds, [], [], 0.02)[0]:
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                continue
            if fd == self.fd:
                self.output.extend(chunk)
            elif fd == self.channels["events"]:
                self.event_tail.extend(chunk)
                while b"\n" in self.event_tail:
                    line, _, remaining = self.event_tail.partition(b"\n")
                    self.event_tail[:] = remaining
                    self.events.append(line.decode().split("\t"))
            elif fd == self.channels["context"]:
                self.records.extend(self.decoder.feed(chunk))
            elif fd == self.channels["returned"]:
                self.returned.extend(chunk)

    def wait(self, predicate):
        """Use a timeout only to fail a missing observation."""
        deadline = time.monotonic() + DEADLINE
        while not predicate() and time.monotonic() < deadline:
            self.pump()
        if not predicate():
            raise AssertionError(
                f"events={self.events!r}; records={self.records!r}; output={bytes(self.output[-2500:])!r}"
            )

    def primary(self, generation):
        """Release the actual helper only after the context generation is observed."""
        identity = str(generation).encode()
        self.wait(lambda: ("prompt_id", identity) in self.records)
        os.write(self.channels["ack"], identity + b"\n")
        self.wait(lambda: ("input_done", identity) in self.records)

    def send(self, value):
        """Send exactly the test bytes to the shell's PTY."""
        os.write(self.fd, value.encode() if isinstance(value, str) else value)

    def submit(self, identity, value):
        """Write a parent-owned identity before delivering one command line."""
        (self.root / "submission").write_text(f"{identity}\n")
        self.send(value)

    def close(self):
        """Reap the controlling process and close the isolated channels."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        for fd in (self.fd, *self.channels.values()):
            os.close(fd)


class ZshBoundaryTests(unittest.TestCase):
    """Separate syntax requests, program stdin, and real input-reclaim guarantees."""

    @classmethod
    def setUpClass(cls):
        """Build the unchanged production helper for the standalone PTY harness."""
        if not shutil.which("zsh") or not shutil.which("cc"):
            raise unittest.SkipTest("zsh and a C compiler are required")
        cls.build = tempfile.TemporaryDirectory(prefix="ish-zsh-helper-")
        cls.addClassCleanup(cls.build.cleanup)
        source = Path(cls.build.name) / "forward.c"
        source.write_text(TTY_FORWARD)
        cls.helper = Path(cls.build.name) / "forward"
        subprocess.run(
            ["cc", str(source), "-o", str(cls.helper)],
            check=True,
            capture_output=True,
            timeout=30,
        )

    @contextlib.contextmanager
    def session(self, *, reclaim=True, semantic=False, freeze_c=False):
        """Create an isolated actual zsh session, not a model of its parser."""
        with tempfile.TemporaryDirectory(prefix="ish-zsh-boundary-") as directory:
            shell = ZshPTY(
                Path(directory),
                self.helper,
                reclaim=reclaim,
                semantic=semantic,
                freeze_c=freeze_c,
            )
            try:
                yield shell
            finally:
                shell.close()

    def test_continuation_requests_precede_execution_and_keep_the_same_shell(self):
        """Each PS2 expansion is in the owning shell before preexec authorizes stdin."""
        with self.session() as shell:
            shell.send("for value in A B; do\n")
            shell.wait(lambda: len(shell.events) == 1)
            self.assertEqual(shell.events[0][0], "C")
            shell.send('print -r -- "$value" >> result\n')
            shell.wait(lambda: len(shell.events) == 2)
            self.assertEqual([event[0] for event in shell.events], ["C", "C"])
            self.assertFalse((shell.root / "result").exists())
            shell.send("done\n")
            shell.primary(2)
            self.assertEqual([event[0] for event in shell.events], ["C", "C", "R", "P"])
            self.assertEqual([event[3] for event in shell.events], ["0"] * 4)
            self.assertEqual((shell.root / "result").read_bytes(), b"A\nB\n")

    def test_heredoc_data_and_builtin_stdin_have_different_boundaries(self):
        """Heredoc lines request syntax; an executing read consumes its own stdin."""
        with self.session() as shell:
            shell.send("cat > result <<'END'\n")
            shell.wait(lambda: any(event[0] == "C" for event in shell.events))
            shell.send("  probe 한 글  \n")
            shell.wait(lambda: len(shell.events) == 2)
            shell.send("END\n")
            shell.primary(2)
            self.assertEqual([event[0] for event in shell.events], ["C", "C", "R", "P"])
            self.assertEqual((shell.root / "result").read_text(), "  probe 한 글  \n")
            shell.events.clear()
            shell.send('read -r value; print -r -- "$value" > reply\n')
            shell.wait(lambda: bool(shell.events))
            self.assertEqual([event[0] for event in shell.events], ["R"])
            shell.send("native reply\n")
            shell.primary(3)
            self.assertEqual([event[0] for event in shell.events], ["R", "P"])
            self.assertEqual((shell.root / "reply").read_text(), "native reply\n")

    def test_late_raw_input_reaches_parser_without_reclaim(self):
        """Reproduce a short command finishing before its raw event is acted upon."""
        with self.session(reclaim=False) as shell:
            shell.send("print completed > completed\n")
            shell.wait(lambda: [event[0] for event in shell.events] == ["R", "P"])
            self.assertEqual((shell.root / "completed").read_text(), "completed\n")
            # The parent now handles the already old R event and forwards typeahead.
            shell.send("print escaped > leaked\n")
            shell.wait(lambda: (shell.root / "leaked").exists())
            self.assertEqual((shell.root / "leaked").read_text(), "escaped\n")

    def test_existing_helper_reclaims_late_input_before_the_next_parser_read(self):
        """The existing generation ACK, not a new handshake, holds late raw bytes."""
        with self.session() as shell:
            shell.send("print completed > completed\n")
            shell.wait(lambda: ("prompt_id", b"2") in shell.records)
            self.assertEqual((shell.root / "completed").read_text(), "completed\n")
            delayed = b"print escaped > leaked\n"
            shell.send(delayed)
            shell.wait(lambda: bytes(shell.returned) == delayed)
            self.assertNotIn(("input_done", b"2"), shell.records)
            shell.primary(2)
            shell.send("print safe > result\n")
            shell.primary(3)
            self.assertFalse((shell.root / "leaked").exists())
            self.assertEqual((shell.root / "result").read_text(), "safe\n")

    def test_semantic_wire_identifies_every_syntax_reply_and_raw_permission(self):
        """C/R frames correlate actual parser requests to the parent's submission ID."""
        with self.session(semantic=True) as shell:
            self.assertIn(("edit_mode", b"1;zsh-observation;1;1"), shell.records)
            shell.submit(11, "for value in A B; do\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;11;1;C") in shell.records)
            shell.send('print -r -- "$value" >> result\n')
            shell.wait(lambda: ("edit", b"1;zsh-observation;11;2;C") in shell.records)
            shell.send("done\n")
            shell.primary(2)
            self.assertEqual(
                [body for category, body in shell.records if category == "edit"],
                [
                    b"1;zsh-observation;11;1;C",
                    b"1;zsh-observation;11;2;C",
                    b"1;zsh-observation;11;3;R",
                ],
            )
            self.assertEqual((shell.root / "result").read_bytes(), b"A\nB\n")
            shell.submit(12, "cat > body <<'END'\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;12;1;C") in shell.records)
            shell.send("  probe 한 글  \n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;12;2;C") in shell.records)
            shell.send("END\n")
            shell.primary(3)
            self.assertIn(("edit", b"1;zsh-observation;12;3;R"), shell.records)
            self.assertEqual((shell.root / "body").read_text(), "  probe 한 글  \n")
            self.assertNotIn(b"\x1b]633;zsh-observation;s\x07", shell.output)

    def test_option_opt_out_disables_semantic_capability_until_restored(self):
        """Existing promptsubst opt-out retains native continuation, with explicit mode."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "unsetopt promptsubst\n")
            shell.primary(2)
            self.assertIn(("edit_mode", b"1;zsh-observation;2;0"), shell.records)
            shell.records.clear()
            shell.submit(2, "for value in A B; do\nprint $value >> result\ndone\n")
            shell.primary(3)
            self.assertFalse(any(category == "edit" for category, _ in shell.records))
            self.assertEqual((shell.root / "result").read_bytes(), b"A\nB\n")
            shell.submit(3, "setopt promptsubst\n")
            shell.primary(4)
            self.assertIn(("edit_mode", b"1;zsh-observation;4;1"), shell.records)

    def test_source_and_command_substitution_keep_program_stdin_native(self):
        """Source-file parsing and an executing substitution do not emit syntax grants."""
        with self.session(semantic=True) as shell:
            (shell.root / "source.zsh").write_text(
                "for value in A B; do\nprint $value >> source-result\ndone\n"
                'read -r value\nprint -r -- "$value" > source-reply\n'
            )
            shell.submit(1, "source source.zsh\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;1;1;R") in shell.records)
            shell.send("source stdin\n")
            shell.primary(2)
            self.assertEqual((shell.root / "source-result").read_bytes(), b"A\nB\n")
            self.assertEqual(
                (shell.root / "source-reply").read_text(), "source stdin\n"
            )
            shell.submit(
                2,
                'value=$(read -r nested; print -r -- "$nested"); print -r -- "$value" > substitution\n',
            )
            shell.wait(lambda: ("edit", b"1;zsh-observation;2;1;R") in shell.records)
            shell.send("substitution stdin\n")
            shell.primary(3)
            self.assertEqual(
                (shell.root / "substitution").read_text(), "substitution stdin\n"
            )
            self.assertEqual(
                [body for category, body in shell.records if category == "edit"],
                [b"1;zsh-observation;1;1;R", b"1;zsh-observation;2;1;R"],
            )

    def test_native_interrupt_acknowledgement_still_precedes_reader_release(self):
        """C requests preserve the existing LI/linefeed cancellation transaction."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "for value in A B; do\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;1;1;C") in shell.records)
            shell.send(b"\x03")
            shell.wait(lambda: b"\x1b]633;zsh-observation;LI;1;1\x07" in shell.output)
            shell.send(b"\n")
            shell.primary(2)
            self.assertIn(("exitcode", b"130"), shell.records)
            shell.submit(2, "print intact > result\n")
            shell.primary(3)
            self.assertEqual((shell.root / "result").read_text(), "intact\n")

    def test_interrupt_after_c_before_the_prompt_function_returns(self):
        """A controlled stop exposes SIGINT between C emission and the parser read."""
        with self.session(semantic=True, freeze_c=True) as shell:
            shell.submit(1, "for value in A B; do\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;1;1;C") in shell.records)
            shell.wait(
                lambda: (
                    os.waitid(
                        os.P_PID, shell.pid, os.WSTOPPED | os.WNOWAIT | os.WNOHANG
                    )
                    is not None
                )
            )
            shell.send(b"\x03")
            os.kill(shell.pid, signal.SIGCONT)
            shell.wait(lambda: b"\x1b]633;zsh-observation;LI;1;1\x07" in shell.output)
            shell.send(b"\n")
            shell.primary(2)
            shell.submit(2, "print intact > result\n")
            shell.primary(3)
            self.assertEqual((shell.root / "result").read_text(), "intact\n")

    def test_semantic_events_preserve_hook_status_and_empty_submission(self):
        """Status survives both hooks; a blank line returns primary without raw grant."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "false\n")
            shell.primary(2)
            self.assertEqual(shell.events[-1][0:3:2], ["P", "1"])
            shell.events.clear()
            shell.submit(2, 'print -r -- "$?" > result\n')
            shell.primary(3)
            self.assertEqual(shell.events[0][0:3:2], ["R", "1"])
            self.assertEqual((shell.root / "result").read_text(), "1\n")
            shell.records.clear()
            shell.submit(3, "\n")
            shell.primary(4)
            self.assertFalse(any(category == "edit" for category, _ in shell.records))

    def test_external_stdin_and_eof_are_native(self):
        """An external foreground reader owns literal bytes and canonical EOF."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "cat > result\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;1;1;R") in shell.records)
            shell.send("  external 한 글  \n")
            shell.send(b"\x04")
            shell.primary(2)
            self.assertEqual(
                (shell.root / "result").read_text(), "  external 한 글  \n"
            )
            self.assertEqual(
                [body for category, body in shell.records if category == "edit"],
                [b"1;zsh-observation;1;1;R"],
            )

    def test_continuation_eof_returns_without_executing_the_unfinished_body(self):
        """EOF aborts a partial syntax collection without turning its text into stdin."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "for value in A B; do\n")
            shell.wait(lambda: ("edit", b"1;zsh-observation;1;1;C") in shell.records)
            shell.send(b"\x04")
            shell.primary(2)
            self.assertFalse(
                any(
                    body.endswith(b";R")
                    for category, body in shell.records
                    if category == "edit"
                )
            )
            shell.submit(2, "print intact > result\n")
            shell.primary(3)
            self.assertEqual((shell.root / "result").read_text(), "intact\n")

    def test_recovery_keeps_one_emission_per_boundary(self):
        """Repeated integration source preserves hooks and does not duplicate requests."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "ish_recover\n")
            shell.primary(2)
            shell.submit(2, "ish_recover\n")
            shell.primary(3)
            shell.submit(3, "print intact > result\n")
            shell.primary(4)
            self.assertEqual(
                [body for category, body in shell.records if category == "edit"],
                [
                    b"1;zsh-observation;1;1;R",
                    b"1;zsh-observation;2;1;R",
                    b"1;zsh-observation;3;1;R",
                ],
            )
            self.assertEqual((shell.root / "result").read_text(), "intact\n")

    def test_enabling_zle_withdraws_the_unverified_semantic_mode(self):
        """Native ZLE input has not been verified for this no-ZLE handoff contract."""
        with self.session(semantic=True) as shell:
            shell.submit(1, "setopt zle\n")
            shell.primary(2)
            self.assertIn(("edit_mode", b"1;zsh-observation;2;0"), shell.records)


if __name__ == "__main__":
    unittest.main()
