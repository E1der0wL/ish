"""Exercise cancellation and pre-write rejection through the real PTY/PTK CLI."""

import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_semantic_edit_cli as semantic

LONG_COMMAND = "printf '%s' '" + "x" * 5000 + "' >> first-result"
CANCELLED_SUFFIX = "echo cancelled > cancelled-result"
TRACE_KEY = b"\x1b[21~"

OBSERVE_INPUT = r"""
from ish.runtime.observer import InputObserver
observer = InputObserver(enabled=True)
prompt.input_observer = observer
prompt.interactive_shell.input_observer = observer
prompt.app.input.observer = observer
def trace_input(event):
    with open('input-traces', 'a') as output:
        print(json.dumps(observer.snapshot()), file=output)
prompt.set_key('f10', handler=trace_input)
"""

REJECT_LEASE = r"""
from ish.shell.engine import InputModeLease
from ish.shell.input import InputRejected
original_lease_init = InputModeLease.__init__
def reject_first_lease(lease, *args, **kwargs):
    if not Path('lease-rejected').exists():
        Path('lease-rejected').write_text('before-write')
        raise InputRejected('Injected pre-write lease rejection')
    return original_lease_init(lease, *args, **kwargs)
InputModeLease.__init__ = reject_first_lease
"""

REJECT_SYNTAX_REPLY = r"""
from ish.shell.engine import InteractiveShell
from ish.shell.handoff import InputOwner
from ish.shell.input import InputRejected
original_validate = InteractiveShell.validate_submission
def reject_once_after_edit(shell, data):
    if (shell._handoff.owner is InputOwner.EDITOR
            and shell.edit_request is None
            and not Path('reply-rejected').exists()):
        Path('reply-rejected').write_text('before-reply')
        raise InputRejected('Injected syntax reply rejection')
    return original_validate(shell, data)
InteractiveShell.validate_submission = reject_once_after_edit
"""

GATED_REJECT = (
    REJECT_LEASE
    + r"""
import asyncio
from ish.shell.engine import InteractiveShell
original_staged = InteractiveShell._send_staged
async def gate_before_lease(shell, data, prefix, generation):
    if not Path('lease-rejected').exists():
        Path('lease-check-held').write_text('before-check')
        while not Path('release-lease').exists():
            await asyncio.sleep(0.001)
    return await original_staged(shell, data, prefix, generation)
InteractiveShell._send_staged = gate_before_lease
original_forward = InteractiveShell._forward_input
def observe_held_keys(shell, fd, epoch=None):
    original_forward(shell, fd, epoch)
    if b'\x1b[19~' in shell._deferred_input:
        Path('keys-held').write_text('fresh-key-queued')
InteractiveShell._forward_input = observe_held_keys
"""
)

CANCEL_SENDER = r'''
from ish.shell.engine import InputModeLease, InteractiveShell

os.mkfifo('release-primary')
with open('.bashrc', 'a') as startup:
    startup.write("""
_test_primary_gate() {
    if [[ -e hold-primary ]]; then
        printf waiting > primary-held
        IFS= read -r _test_release < release-primary
        command rm hold-primary
    fi
}
PROMPT_COMMAND=(_test_primary_gate)
""")

original_pending = InputModeLease.pending
def hold_before_newline(lease):
    if Path('cancel-armed').exists():
        Path('hold-primary').touch()
        Path('lease-held').write_text('prefix-delivered')
        return True
    return original_pending(lease)
InputModeLease.pending = hold_before_newline

original_cancel = InteractiveShell.cancel_submission
def cancel_then_delay_request(shell, control, remaining):
    submission = shell._handoff.submission
    sequence = shell._handoff.sequence + 1
    original_cancel(shell, control, remaining)
    Path('cancel-armed').unlink(missing_ok=True)
    body = f'1;{shell.signals.token};{submission};{sequence};C'.encode()
    frame = bytes((1,)) + b'edit' + bytes((31,)) + body + bytes((4,))
    fd = os.open(shell.shell_pipe_path, os.O_WRONLY)
    try:
        os.write(fd, frame)
    finally:
        os.close(fd)
    Path('stale-injected').write_text('after-cancel')
InteractiveShell.cancel_submission = cancel_then_delay_request

original_receive = InteractiveShell._receive_context
def observe_delayed_request(shell, fd):
    original_receive(shell, fd)
    if Path('stale-injected').exists():
        Path('stale-processed').write_text('received')
InteractiveShell._receive_context = observe_delayed_request

original_send = InteractiveShell._send
async def observe_transport(shell, data, generation=None):
    payload = data.encode(shell.encoder) if isinstance(data, str) else data
    if b'echo cancelled > cancelled-result' in payload:
        Path('suffix-sent').write_bytes(payload)
    return await original_send(shell, data, generation)
InteractiveShell._send = observe_transport
'''


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class SemanticRaceTests(unittest.TestCase):
    """Inject only boundary timing/failures; use real editors, shells, and file consumers."""

    @contextlib.contextmanager
    def session(self, injected):
        """Install isolated fault injection before starting the actual CLI."""
        with tempfile.TemporaryDirectory(prefix="ish-semantic-race-") as directory:
            root = Path(directory)
            with patch.object(semantic, "RC", semantic.RC + "\n" + injected):
                terminal = semantic.Terminal(root)
            try:
                terminal.require(
                    lambda: (
                        b"READY>" in terminal.output
                        and semantic.EDITOR_ON in terminal.output
                    ),
                    semantic.STARTUP_DEADLINE,
                )
                yield root, terminal
            finally:
                terminal.close()

    def test_prewrite_lease_rejection_restores_editor_and_command(self):
        """Reject once before any PTY byte, retry the same PTK text, and execute it once."""
        with self.session(REJECT_LEASE) as (root, terminal):
            terminal.send(
                semantic.PASTE_START
                + LONG_COMMAND.encode()
                + semantic.PASTE_END
                + b"\r"
                + semantic.WITNESS
            )
            terminal.result("lease-rejected", b"before-write")
            self.assertEqual(
                terminal.witness(sent=True),
                {"running": True, "primary": True, "text": LONG_COMMAND},
            )
            self.assertFalse((root / "first-result").exists())
            terminal.send(b"\r")
            terminal.result("first-result", b"x" * 5000)
            self.assertTrue(terminal.witness()["primary"])
            self.assertEqual((root / "first-result").read_bytes(), b"x" * 5000)

    def test_sender_cancel_rejects_delayed_request_and_drops_paste_suffix(self):
        """A delayed C after sender cancellation cannot release already-cancelled lines."""
        with self.session(CANCEL_SENDER) as (root, terminal):
            (root / "cancel-armed").touch()
            block = LONG_COMMAND + "\n" + CANCELLED_SUFFIX + "\n"
            terminal.send(
                semantic.PASTE_START + block.encode() + semantic.PASTE_END + b"\r"
            )
            terminal.result("lease-held", b"prefix-delivered")
            terminal.send(b"\x03")
            terminal.result("primary-held", b"waiting")
            terminal.result("stale-processed", b"received")
            with (root / "release-primary").open("w") as release:
                release.write("resume\n")
            self.assertTrue(terminal.witness()["primary"])
            terminal.send(
                'IFS= read -r value; printf "%s" "$value" > after-input\rkept\r'
            )
            terminal.result("after-input", b"kept")
            self.assertTrue(terminal.witness()["primary"])
            self.assertFalse((root / "first-result").exists())
            self.assertFalse((root / "cancelled-result").exists())
            self.assertFalse((root / "suffix-sent").exists())

    def test_key_arriving_during_rejected_sender_reaches_the_retry_editor(self):
        """Keys captured by the native callback before rejection remain fresh PTK keys."""
        with self.session(GATED_REJECT) as (root, terminal):
            terminal.send(
                semantic.PASTE_START
                + LONG_COMMAND.encode()
                + semantic.PASTE_END
                + b"\r"
            )
            terminal.result("lease-check-held", b"before-check")
            terminal.send(semantic.WITNESS)
            terminal.result("keys-held", b"fresh-key-queued")
            (root / "release-lease").touch()
            terminal.result("lease-rejected", b"before-write")
            self.assertEqual(
                terminal.witness(sent=True),
                {"running": True, "primary": True, "text": LONG_COMMAND},
            )
            terminal.send(b"\r")
            terminal.result("first-result", b"x" * 5000)
            self.assertTrue(terminal.witness()["primary"])
            self.assertEqual((root / "first-result").read_bytes(), b"x" * 5000)

    def test_rejected_syntax_reply_keeps_text_and_delivers_body_once(self):
        """Retry one C response in PTK and deliver its literal body exactly once."""
        with self.session(REJECT_SYNTAX_REPLY) as (root, terminal):
            terminal.send(b"cat <<'END' > body\r" + semantic.WITNESS)
            self.assertFalse(terminal.witness(sent=True)["primary"])
            body = "  probe 한 글  "
            terminal.send(body.encode() + b"\r" + semantic.WITNESS)
            terminal.result("reply-rejected", b"before-reply")
            self.assertEqual(
                terminal.witness(sent=True),
                {"running": True, "primary": False, "text": body},
            )
            terminal.send(b"\rEND\r")
            terminal.result("body", (body + "\n").encode())
            self.assertTrue(terminal.witness()["primary"])
            self.assertFalse((root / "probe-executed").exists())

    def test_repeated_continuation_and_raw_return_have_one_observed_reader(self):
        """Observe one PTK reader at each edit and preserve the native read result."""
        with self.session(OBSERVE_INPUT) as (root, terminal):
            traces = root / "input-traces"
            for count, text in enumerate(
                (
                    "if true; then",
                    "IFS= read -r value",
                    'printf "%s" "$value" > observed-result',
                ),
                start=1,
            ):
                terminal.send(text.encode() + b"\r" + TRACE_KEY)
                terminal.require(
                    lambda count=count: (
                        traces.exists()
                        and len(traces.read_text().splitlines()) == count
                    )
                )
                trace = json.loads(traces.read_text().splitlines()[-1])
                self.assertEqual(trace["issues"], {})
                self.assertEqual(trace["active"], ["EDITOR"])
                terminal.send(semantic.WITNESS)
                self.assertFalse(terminal.witness(sent=True)["primary"])
            terminal.send(b"fi\rraw payload\r")
            terminal.result("observed-result", b"raw payload")
            self.assertTrue(terminal.witness()["primary"])
            terminal.send(TRACE_KEY)
            terminal.require(lambda: len(traces.read_text().splitlines()) == 4)
            trace = json.loads(traces.read_text().splitlines()[-1])
            self.assertEqual(trace["issues"], {})
            self.assertEqual(trace["active"], ["EDITOR"])
            self.assertEqual((root / "observed-result").read_bytes(), b"raw payload")


if __name__ == "__main__":
    unittest.main()
