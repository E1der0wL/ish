# Input observation and PTY validation

Validation date: 2026-09-12.

This is the original observation report, retained as before-change evidence.
The four defects below have since been addressed in the
[input transfer follow-up](INPUT_TRANSFER.md). Its results describe the current code.

The observer was added without changing input routing, recovery commands,
terminal modes, cancellation policy, or buffer transfers. The tests found
existing input-boundary limitations as well as successful cases. A trace with
no overlapping consumers is not proof that every byte reaches the intended
consumer.

## Observation scope

`prompt.input_observer` is shared by the shell engine, prompt-toolkit input,
and the Python tool runner. It records:

- Actual prompt-toolkit input attach/detach, including temporary detachment.
- Shell and Python tool input reader registration and removal.
- Read sizes (parsed key counts for the editor, bytes for shell/tool reads).
- Prompt candidates and accepted IDs, submission sizes, deferred input,
  forwarded input, and typeahead receipt/injection.
- Overlapping observed consumers and reads outside an observed lifetime.

The default is disabled. When enabled, the last 4,096 events are retained, with
cumulative event/issue counters and an explicit dropped-event count. Events
contain no command text, keystroke contents, or environment values. There is no
periodic probe, background process, automatic file write, or reader rejection.
Observation tokens do not grant or revoke input access.

The prompt-toolkit adapter delegates the existing input object, parser, callback,
terminal modes, and typeahead identity. It returns the original key lists without
filtering or consuming them. The observer does not inspect or drain the toolkit's
private typeahead store. Direct stdin reads by third-party plugins are outside
its coverage. Counts in different units cannot be summed to prove byte delivery.

Enable it in `~/ish/.ishrc.py` before the first prompt:

```python
import json

prompt.input_observer.enabled = True


async def save_input_trace():
    """Export the current metadata trace on an explicit editor command."""
    path = config.CACHE_DIR / "input-trace.json"
    path.write_text(
        json.dumps(prompt.input_observer.snapshot(), indent=2), encoding="utf-8"
    )


prompt.internal_commands["ish_input_trace"] = (save_input_trace, (), {})
```

Run `ish_input_trace` at an ish editor prompt to export the snapshot. Take
snapshots on the event-loop thread; do not toggle observation while a consumer
is already attached. The test runner additionally uses a test-only SIGUSR1
handler to schedule snapshot export without injecting a diagnostic command
into a waiting program. Production installs no such signal handler.

## Method and environment

Tests launch the real ish CLI under a PTY, with isolated home directories and
shell startup files. They compare exact ordered records appended to a fixture
file by the executed commands/tools, rather than counting terminal echoes.
Fixtures include unique tokens and Unicode input with spaces.

Every main scenario runs with observation disabled and enabled. Additional
failure comparisons remove the observation adapter and give prompt-toolkit its
original input object. The same failure behavior occurs without the adapter.

Environment: WSL2 Linux 6.6.87.2, glibc 2.43, Python 3.12.14,
prompt-toolkit 3.0.53; Bash 5.3.9, zsh 5.9, dash 0.5.12, tcsh 6.24.13,
real BSD csh, and a separately built tcsh 6.20.00 binary. The older tcsh version
matches the reported version, but this is not a run on RHEL 8.10 itself.

## Main scenario results

Each cell describes both observation-disabled and observation-enabled runs.

| Shell | Enter followed by typeahead | Multiline paste | Slow command with typeahead | Python tool transitions |
| --- | --- | --- | --- | --- |
| Bash 5.3.9 | Pass | Pass | Pass | Pass |
| zsh 5.9 | Pass | Pass | Pass | Pass |
| dash 0.5.12 | Pass | Pass | Pass | Pass |
| tcsh 6.24.13 | Pass | Pass | Pass | Pass |
| tcsh 6.20.00 | Delayed and reordered input | Pass | Pass | Pass |
| BSD csh | Editor typeahead remains pending | Pass with explicit reconnect | Pass with explicit reconnect | Pass with explicit reconnect |

- **Enter:** six batches of three distinct commands, alternating a single write,
  adjacent writes, and writes separated by about 20 ms. Passing sessions execute
  all 18 records in order exactly once.
- **Paste:** a complete Bash/POSIX `for` or C-shell `foreach` block produces 40
  records in order exactly once.
- **Slow command:** eight commands are typed while a fixture waits behind a
  release barrier. None execute before release. The completion marker and eight
  commands produce nine ordered records.
- **Tool transitions:** three shell → Python tool → shell cycles produce 15
  ordered records, including Unicode and spaces. Replies are sent after the
  tool indicates readiness; the following shell command is sent after return
  to the editor. The more aggressive boundaries below are separate checks.

BSD csh intentionally stays in native input until `ish_recover`. The passing
BSD tests explicitly reconnect at that boundary. They do not demonstrate
automatic editor restoration.

Across the six shell variants, **44 of 48 main scenario runs passed**. The four
failures are the enabled/disabled Enter cases for tcsh 6.20 and BSD csh. The
additional aggressive Python tool boundary checks are reported separately below.

No overlapping observed consumers, inactive reads, or dropped trace events
were reported in the main scenarios. There were no duplicate result records
in the passing cases. The failing cases show why lifecycle observations and
end-to-end result checks are both necessary.

## Remaining input-boundary limitations

### tcsh 6.20: late FIFO input can execute after a newer user command

The 20 ms typeahead case fails with observation enabled, disabled, and with the
original prompt-toolkit input object. One trace shows:

1. The shell reader forwards 52 bytes containing two further commands.
2. Prompt generation 8 is accepted and the editor attaches.
3. Another 26 bytes arrive through the typeahead FIFO while `EDITOR` is active.
4. A fresh prompt candidate for generation 9 arrives while the editor is active.

The result file contains 8 of the expected 9 records at the failed third batch.
The late FIFO bytes remain pending instead of entering the currently active
editor. The trace and source identify a gap between prompt acceptance and
typeahead injection; no simultaneous stdin readers are required for it to occur.

A follow-up run submitted a new command after this stall. Both the observed
and original-input runs produced the following suffix:

```text
enter-2-0
enter-2-1
later-user-command
enter-2-2
```

`enter-2-2` was typed before `later-user-command`. This confirms execution
reordering, not merely a delayed screen redraw or permanently lost bytes.

### Python tool entry: already-read editor keys do not become tool replies

Sending the tool command and its intended reply in one PTY write:

```text
observe_tool events 1<Enter>tool-early<Enter>
```

leaves the tool waiting. After a separately supplied fallback reply lets the
tool exit, `tool-early` is handled as a shell command. This was reproduced with
Bash and tcsh, observation on/off, and the original input object. The toolkit
retains already-read keys for the next editor invocation; the tool runner does
not receive that store.

### Python tool exit: trailing input does not carry over from the worker PTY

With the tool ready for one line, sending its final reply and a following shell
command in one write:

```text
tool-tail<Enter>/bin/sh emit.sh after-tool-tail<Enter>
```

records `tool-tail`, but does not execute `after-tool-tail`. The trace shows the
tool reader receiving all 42 bytes. The worker's separate PTY and buffers are
closed when the tool exits, and the runner has no transfer of remaining input
back to the shell/editor. This also reproduces with observation on/off and the
original input object in Bash and tcsh.

### BSD csh: editor typeahead waits behind native input

When the first command and two following commands arrive together, only the
first runs. The observer shows `EDITOR` reading 55 parsed keys, then handing
off to `SHELL`, which remains active. The remaining editor typeahead is not
processed while BSD csh stays in native input. The same outcome occurs without
the observation adapter. This is a consequence of the documented explicit
reconnect limitation, not an overlapping-reader event.

These limitations were recorded, not repaired by this change. Reader ownership
checks alone cannot fix them; buffer delivery and transfer policies need
separate, explicit design and regression coverage.

## Reproduction and evidence

The locally ignored `tests/input_observation_probe.py` is an explicit diagnostic
runner. Its failed checks return exit status 1 and remain in the JSON results;
they are not silently treated as passes. Example WSL command:

```sh
PYTHONPATH=src:tests .venv/bin/python -B tests/input_observation_probe.py \
    --shells bash zsh tcsh sh --output dist/input-observation
```

For worker boundaries, use `--scenarios tool_early tool_tail --modes on off native`.
Set `ISH_TEST_TCSH` to the older binary or `ISH_TEST_CSH` to the real BSD csh
binary to test those implementations rather than a `csh` symlink to tcsh.

Each output directory contains exact expected/actual results, a bounded metadata
trace, and a terminal transcript. Fixture-only terminal transcripts contain the
synthetic test input; the production metadata trace does not.

The standard regression suite passed **196 tests in 306.340 seconds**, including
the first six observer tests. A final targeted run passed **17 tests**, including
all eight observer contract tests, worker resize tests, and prompt-boundary tests.
Ruff lint and format checks passed for all seven changed Python files. No Nuitka rebuild was
performed; installed binaries need rebuilding to include this feature.

Locally generated evidence (ignored by Git):

- [Combined final results and environment](../dist/input-observation-results.json)
- [tcsh 6.20 execution-order confirmation](../dist/input-observation-tcsh620-order/summary.json)
- [Python tool boundaries](../dist/input-observation-tool-boundaries/summary.json)
- [Python tool boundaries with the original input](../dist/input-observation-native-boundaries/summary.json)
- [BSD csh with the original input](../dist/input-observation-csh-native/summary.json)
- [Full regression log](../dist/input-observation-suite.log)
- [Final validation log](../dist/input-observation-final-validation.log)
