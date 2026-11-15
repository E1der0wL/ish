"""Observe stock tcsh completions using actual PTY input and file results.

No terminal output is parsed, even for startup or completion readiness. Output
is only drained and saved as failure evidence. These probes enable no capability
and do not implement a PTK dropdown or a final-candidate export protocol.
"""

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import termios
import unittest
from pathlib import Path

from test_stream_shells import TCSH, Shell

FIXTURE = Path(__file__).with_name("tcsh_completion.py")
EVIDENCE = os.environ.get("ISH_COMPLETION_EVIDENCE")
DEFAULT_PAYLOAD = b"alpha\nbravo\n"
COMPLETE = b"\t"
LIST = b"\x18l"
LEFT = b"\x02"
END = b"\x05"


def records(root, name):
    """Read fixture records, never displayed terminal strings."""
    return [
        json.loads(path.read_text()) for path in sorted((root / name).glob("*.json"))
    ]


@unittest.skipUnless(TCSH, "tcsh is required")
class CompletionTests(unittest.TestCase):
    """Separate generator observation from tcsh's actual completion results."""

    @contextlib.contextmanager
    def session(
        self,
        *,
        rule=None,
        tapped=False,
        payload=DEFAULT_PAYLOAD,
        config="",
        producer=None,
        integration=False,
    ):
        """Reuse the real controlling-PTY fixture with an isolated completion setup."""
        with tempfile.TemporaryDirectory(prefix="ish-completion-") as directory:
            root = Path(directory)
            helper = root / "fixture.py"
            helper.write_text(f"#!{sys.executable}\n" + FIXTURE.read_text())
            helper.chmod(0o755)
            for name in ("pick", "generate", "tap", "reader"):
                (root / name).symlink_to(helper.name)
            settings = {"payload": payload.hex(), **(producer or {})}
            (root / "producer.json").write_text(json.dumps(settings))
            generator = "tap" if tapped else "generate"
            completion = (
                f"complete pick 'p/*/`{generator}`/'\n"
                if rule == "dynamic"
                else (f"complete pick '{rule}'\n" if rule else "")
            )
            (root / ".tcshrc").write_text(
                "set prompt = 'completion> '\nset prompt2 = 'more> '\n"
                "unset autologout\nset history = 100\nbindkey -e\n"
                "bindkey '^I' complete-word\nbindkey '^Xl' list-choices\n"
                "set path = ( . /usr/bin /bin )\n"
                "alias precmd 'set _saved = $status; /usr/bin/touch ready; set status = $_saved'\n"
                + completion
                + config,
                encoding="utf-8",
            )
            shell = Shell(root, integration=integration, startup=False)
            try:
                termios.tcsetwinsize(shell.fd, (24, 100))
                shell.until(lambda: (root / "ready").exists())
                yield root, shell
            finally:
                # A failed gated producer may still own the terminal. Stop only
                # process groups belonging to this fixture's controlling session.
                groups = {shell.pid}
                with contextlib.suppress(OSError):
                    groups.add(os.tcgetpgrp(shell.fd))
                for group in groups:
                    with contextlib.suppress(ProcessLookupError):
                        if group > 0 and os.getsid(group) == shell.pid:
                            os.killpg(group, signal.SIGKILL)
                shell.close()
                if EVIDENCE:
                    destination = Path(EVIDENCE, self._testMethodName, root.name)
                    destination.mkdir(parents=True)
                    shutil.copytree(
                        root, destination, dirs_exist_ok=True, symlinks=True
                    )
                    (destination / "pty-output.bin").write_bytes(shell.output)

    def arguments(self, root, shell, data, count=1):
        """Complete and execute a native line, then observe the actual argv consumer."""
        shell.send(data)
        shell.until(lambda: len(records(root, "arguments")) == count)
        return records(root, "arguments")

    def test_default_file_completion_has_no_generator_export(self):
        """The builtin file engine completes even though no generator/tap was called."""
        with self.session(tapped=True) as (root, shell):
            (root / "unique-file").touch()
            self.assertEqual(
                self.arguments(root, shell, b"pick uniq\t\n"), [["unique-file"]]
            )
            self.assertEqual(records(root, "taps"), [])
            self.assertEqual(records(root, "contexts"), [])

    def test_static_rule_is_not_a_computed_candidate_export(self):
        """Printing a completion definition does not run a completion query."""
        with self.session(rule="p/1/(alpha bravo)/", tapped=True) as (root, shell):
            shell.send("complete pick > rules; /usr/bin/touch rules-done\n")
            shell.until(lambda: (root / "rules-done").exists())
            self.assertIn(b"(alpha bravo)", (root / "rules").read_bytes())
            self.assertEqual(self.arguments(root, shell, b"pick br\t\n"), [["bravo"]])
            self.assertEqual(records(root, "taps"), [])

    def test_dynamic_tap_observes_pre_filter_candidates(self):
        """Both producer items are observed; only the matching one becomes argv."""
        for tapped in (False, True):
            with (
                self.subTest(tapped=tapped),
                self.session(rule="dynamic", tapped=tapped) as (root, shell),
            ):
                self.assertEqual(
                    self.arguments(root, shell, b"pick al\t\n"), [["alpha"]]
                )
                self.assertEqual(
                    records(root, "contexts"), [{"command_line": "pick al"}]
                )
                taps = records(root, "taps")
                self.assertEqual(len(taps), int(tapped))
                if tapped:
                    self.assertEqual(bytes.fromhex(taps[0]["stdout"]), DEFAULT_PAYLOAD)

    def test_command_line_omits_cursor_suffix(self):
        """COMMAND_LINE ends at the cursor, while the native suffix survives completion."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            data = b"pick al tail" + LEFT * 5 + COMPLETE + END + b"\n"
            self.assertEqual(self.arguments(root, shell, data), [["alpha", "tail"]])
            self.assertEqual(records(root, "taps")[0]["command_line"], "pick al")

    def test_command_line_omits_prior_command(self):
        """The generator receives the current command segment after a separator."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            self.assertEqual(
                self.arguments(root, shell, b"/bin/true; pick al\t\n"), [["alpha"]]
            )
            self.assertEqual(records(root, "taps")[0]["command_line"], " pick al")

    def test_command_line_is_not_raw_quoted_input(self):
        """The generator context need not retain the original quote syntax."""
        for tapped in (False, True):
            with (
                self.subTest(tapped=tapped),
                self.session(rule="dynamic", tapped=tapped) as (root, shell),
            ):
                # Native tcsh supplies the closing quote when accepting this match.
                self.assertEqual(
                    self.arguments(root, shell, b'pick "al\t\n'), [["alpha"]]
                )
                self.assertEqual(
                    records(root, "contexts")[0]["command_line"], "pick al"
                )

    def test_default_file_completion_preserves_spaces_and_unicode(self):
        """The native file engine quotes real names without our help."""
        for filename, prefix in (("space name", "spa"), ("한글 파일", "한")):
            with self.subTest(filename=filename), self.session() as (root, shell):
                (root / filename).touch()
                self.assertEqual(
                    self.arguments(root, shell, f"pick {prefix}\t\n"), [[filename]]
                )

    def test_dynamic_generator_whitespace_is_split_by_tcsh(self):
        """Raw producer bytes are not an unambiguous array of completion items."""
        for tapped in (False, True):
            with (
                self.subTest(tapped=tapped),
                self.session(
                    rule="dynamic", tapped=tapped, payload="space name\n한글\n".encode()
                ) as (root, shell),
            ):
                self.assertEqual(
                    self.arguments(root, shell, b"pick spa\t\n"), [["space"]]
                )
                self.arguments(root, shell, "pick 한\t\n", count=2)
                self.assertIn(["한글"], records(root, "arguments"))

    def test_list_choices_does_not_export_filtered_array(self):
        """Native list-choices calls the producer but exports only its original bytes."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            self.assertEqual(
                self.arguments(root, shell, b"pick al" + LIST + b"\n"), [["al"]]
            )
            self.assertEqual(
                bytes.fromhex(records(root, "taps")[0]["stdout"]), DEFAULT_PAYLOAD
            )

    def test_variable_candidates_preserve_spaces_without_generator(self):
        """Variable arrays are a distinct native source, not generator stdout lines."""
        config = "set choices = ( 'space name' '한글 항목' )\ncomplete pick 'p/*/$choices/'\n"
        with self.session(config=config, tapped=True) as (root, shell):
            self.assertEqual(
                self.arguments(root, shell, b"pick spa\t\n"), [["space name"]]
            )
            self.arguments(root, shell, "pick 한\t\n", count=2)
            self.assertIn(["한글 항목"], records(root, "arguments"))
            self.assertEqual(records(root, "taps"), [])

    def test_fignore_changes_native_file_matching_without_generator(self):
        """Directory entries alone do not describe the native recognition result."""
        with self.session(config="set fignore = ( .o )\n", tapped=True) as (
            root,
            shell,
        ):
            (root / "source.c").touch()
            (root / "source.o").touch()
            self.assertEqual(
                self.arguments(root, shell, b"pick sour\t\n"), [["source.c"]]
            )
            self.assertEqual(records(root, "taps"), [])

    def test_fast_typeahead_is_consumed_once_in_native_order(self):
        """One PTY write completes and then appends the remaining native keystrokes."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            self.assertEqual(
                self.arguments(root, shell, b"pick al\tTAIL\n"), [["alpha", "TAIL"]]
            )
            self.assertEqual(len(records(root, "taps")), 1)

    def test_cancelled_producer_does_not_execute_the_partial_line(self):
        """Ctrl+C cancels the actual foreground completion and leaves the next prompt usable."""
        with self.session(rule="dynamic", tapped=True, producer={"gate": True}) as (
            root,
            shell,
        ):
            shell.send(b"pick al\t")
            shell.until(lambda: len(records(root, "contexts")) == 1)
            (root / "ready").unlink()
            shell.send(b"\x03")
            shell.until(lambda: (root / "ready").exists())
            self.assertEqual(records(root, "arguments"), [])
            self.assertEqual(records(root, "taps"), [])
            self.assertEqual(
                self.arguments(root, shell, b"pick recovered\n"), [["recovered"]]
            )
            (root / "producer.json").write_text(
                json.dumps({"payload": DEFAULT_PAYLOAD.hex()})
            )
            self.arguments(root, shell, b"pick br\t\n", count=2)
            self.assertIn(["bravo"], records(root, "arguments"))
            self.assertEqual(len(records(root, "taps")), 1)

    def test_delayed_generator_keeps_typeahead_native(self):
        """An explicit gate delays a producer; it is not an ish input ownership grant."""
        with self.session(rule="dynamic", tapped=True, producer={"gate": True}) as (
            root,
            shell,
        ):
            shell.send(b"pick al\t")
            shell.until(lambda: len(records(root, "contexts")) == 1)
            self.assertEqual(records(root, "taps"), [])
            shell.send(b"TAIL\n")
            (root / "release-generator").touch()
            shell.until(lambda: len(records(root, "arguments")) == 1)
            self.assertEqual(records(root, "arguments"), [["alpha", "TAIL"]])

    def test_dollar_input_does_not_invoke_completion(self):
        """The shell's executing stdin reader also retains tabs as input data."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            shell.send('/usr/bin/touch reader-ready; set value = "$<"; pick "$value"\n')
            shell.until(lambda: (root / "reader-ready").exists())
            shell.send("al\t한글\n")
            shell.until(lambda: len(records(root, "arguments")) == 1)
            self.assertEqual(records(root, "arguments"), [["al\t한글"]])
            self.assertEqual(records(root, "taps"), [])

    def test_tap_preserves_fixture_hooks_and_status(self):
        """A wrapped producer preserves the tested native command/hook results."""
        observed = []
        config = (
            "alias precmd 'set _saved = $status; echo pre:$_saved >> hooks; /usr/bin/touch ready; if ( -e done ) /usr/bin/touch done-prompt; set status = $_saved'\n"
            "alias postcmd 'set _saved = $status; echo post:$_saved >> hooks; set status = $_saved'\n"
        )
        for tapped in (False, True):
            with (
                self.subTest(tapped=tapped),
                self.session(rule="dynamic", tapped=tapped, config=config) as (
                    root,
                    shell,
                ),
            ):
                shell.send(
                    "/bin/false\npick al\t\necho $status > command-status; /usr/bin/touch done\n"
                )
                shell.until(lambda: (root / "done-prompt").exists())
                self.assertEqual(records(root, "arguments"), [["alpha"]])
                observed.append(
                    (
                        (root / "hooks").read_bytes(),
                        (root / "command-status").read_bytes(),
                    )
                )
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0][1], b"0\n")

    def test_program_stdin_does_not_invoke_completion(self):
        """Tabs in program stdin remain bytes for that program."""
        with self.session(rule="dynamic", tapped=True) as (root, shell):
            shell.send("./reader\n")
            shell.until(lambda: (root / "reader-ready").exists())
            payload = "al\t한글\n".encode()
            shell.send(payload)
            shell.until(lambda: len(records(root, "stdin")) == 1)
            self.assertEqual(bytes.fromhex(records(root, "stdin")[0]["bytes"]), payload)
            self.assertEqual(records(root, "taps"), [])

    def test_existing_tcsh_integration_preserves_completion(self):
        """The same native completion still runs with the unchanged existing adapter."""
        for integration in (False, True):
            with (
                self.subTest(integration=integration),
                self.session(rule="dynamic", tapped=True, integration=integration) as (
                    root,
                    shell,
                ),
            ):
                self.assertEqual(
                    self.arguments(root, shell, b"pick br\t\n"), [["bravo"]]
                )
                self.assertEqual(
                    bytes.fromhex(records(root, "taps")[0]["stdout"]), DEFAULT_PAYLOAD
                )


if __name__ == "__main__":
    unittest.main()
