# Session guards and cross-shell validation

This follow-up checks the input-transfer fixes across supported shells and adds
conservative cancellation, exit, and terminal-observation guards. It keeps the
prompt-toolkit editor and the established native-input boundaries.

## Applied changes and costs

| Area | Change | Cost and limits |
| --- | --- | --- |
| Cancellation | Each submission has a generation; invalidation prevents an old sender from resuming another chunk. Reader attachments have separate epochs, so a queued callback from a previous attachment cannot read a new owner's input. | Integer comparisons per chunk/callback, with no polling or additional process. Bytes already consumed by a program cannot be recalled. |
| Process exit | Input reads and prompt acceptance consult asyncio's cached child status. Final output still drains independently of process exit. Initialization cannot report success when exit is already confirmed. | Cached state checks; no `ps`, `/proc` scans, or periodic status commands. PTY EOF is recorded separately and does not invent an exit code. |
| Python worker exit | A readable sentinel starts exit verification; input remains available until actual exit is confirmed, then unread PTY input is recovered. | When sentinel EOF precedes waitpid readiness, a shared executor thread waits using `waitid(WNOWAIT)`; multiprocessing retains reaping ownership. There is no supervisor process or busy polling. |
| Terminal observation | Optional snapshots record ICANON, ECHO, ISIG, foreground process group, and terminal size at editor/shell/tool transitions. | Disabled by default: no terminal queries. Enabled: three read-only terminal queries per snapshot and bounded metadata storage. Values never authorize recovery, flush input, or restore terminal modes. |

BSD csh retains its explicit reconnect policy. It has no automatic prompt hook,
so following input continues to the native shell after cancellation; an implicit
`ish_recover` is never inserted.

## A deliberately excluded policy

An experimental policy held every newly read byte after bulk-submission Ctrl+C
until the next primary prompt. It preserved a fast sequence of post-cancel
commands, but a real PTY test showed that it starved a program which handled
SIGINT and then called `input()`. That policy was removed.

Only a fresh shell state/prompt handshake can establish that the command has
finished. Later raw input therefore continues to the current native consumer
until that handshake. The existing handling of the suffix already read in the
same Ctrl+C callback remains. The generation guard protects pending sends and
callbacks; it is not a universal acknowledgement protocol for bytes already in
the PTY or forwarded FIFO. Arbitrary typeahead across an interrupt is not given
an exactly-once guarantee by this change. Strengthening that guarantee requires
a separate consumption protocol or an explicit UX policy with its own tradeoff.

Primary-prompt Ctrl+C remains an editor action. Readline/ZLE settings, user
`stty` changes, shell hooks, and TUI mode restoration retain their existing
behavior. SIGTERM cleanup and unlimited logical-line transport were outside this
change. A subsequent source update addresses catchable termination; see
[termination behavior](TERMINATION.md). Long-line transport remains unchanged.

Enable snapshots through the existing `prompt.input_observer.enabled = True`
setting and export `prompt.input_observer.snapshot()` explicitly when needed.
The existing 4,096-event ring rotates sooner with the additional transition
records; it remains bounded and writes no files automatically.

## Regression coverage

The real PTY matrix compares exact ordered file records for Enter typeahead,
multiline paste, slow commands, repeated Python tool transitions, early tool
replies, unused tool input, and split Unicode characters. It runs with
observation both enabled and disabled for Bash, zsh, dash (`sh`), tcsh, BSD csh,
and a separate tcsh 6.20 binary.

Additional tests cover stale reader attachments, stale senders, Ctrl+C during
large submissions, a SIGINT-handling program that continues reading, cached exit
status, final output after exit, early worker sentinel EOF, disabled observation
syscalls, and terminal-mode preservation. Real Vim and man are exercised on all
five shell adapters, including immediate editing after return. BSD csh tests
explicitly reconnect when an ish editor session is needed.

Existing regression tests cover the previously reported RHEL symptoms:

- Bash initialization with existing or absent `PROMPT_COMMAND`, hook comments,
  exit-status preservation, and repeated sourcing.
- zsh startup options and customized prompts, empty/missing/failing hook arrays,
  autoload hooks, and the empty-command permission-error regression.
- tcsh `foreach` interruption, custom precmd/postcmd/periodic aliases, history
  contamination, delayed command substitution, and disabled secondary editing.
- Multiline PS2 suppression, waiting `read`/`$<`, explicit recovery, replayed
  prompts, output backpressure, PTY resizing, Python tools, and cache cleanup.

The host is WSL2 Linux 6.6.87.2 with Python 3.12.14 and prompt-toolkit 3.0.53.
Installed versions are Bash 5.3.9, zsh 5.9, dash 0.5.12, tcsh 6.24.13, real BSD
csh, and the separate tcsh 6.20.00 executable. Bash 4.4.20 and zsh 5.5.1 are not
installed here. Reproducing the historical scenarios on WSL does not establish
compatibility with the corporate RHEL 8.10 installation or its site rc files.

## Results

The tcsh 6.20 compatibility regression set passed **40 tests in 330.839 seconds**.
It includes legacy foreach/hook/history/TUI cases and the new cross-shell
cancellation and Vim/man checks. Evidence:
[legacy regression log](../dist/session-guard-legacy-regression.log).

The standalone full suite passed **221 tests in 444.844 seconds**. After the
final worker-exit adjustment (preserving input until actual exit, including a
worker that closes its sentinel and keeps reading), **25 affected tests passed
in 47.064 seconds**. Ruff lint and format checks pass for all 55 Python files.

- [Full-suite log](../dist/session-guard-full-suite-final.log)
- [Final worker/input regression log](../dist/session-guard-worker-final.log)

The first concurrent full-suite run exposed incomplete test doubles for the new
cached exit-status check, an optional snapshot error on an invalid simulated FD,
and one cold-start timeout under concurrent load. The doubles and optional
snapshot error were corrected. The existing startup timeout was retained; BSD
csh passed both the isolated follow-up and the standalone full suite.

All **96 real-PTY input scenarios passed**, with exact ordered records, zero
observed ownership conflicts, and no truncated traces:

| Shell | Observation disabled | Observation enabled |
| --- | --- | --- |
| Bash 5.3.9 | 8/8 | 8/8 |
| zsh 5.9 | 8/8 | 8/8 |
| dash 0.5.12 | 8/8 | 8/8 |
| tcsh 6.24.13 | 8/8 | 8/8 |
| BSD csh | 8/8 | 8/8 |
| tcsh 6.20.00 | 8/8 | 8/8 |

[Exact inputs/results and evidence locations](../dist/session-guard-results.json)
are retained locally. This validates the four earlier input-transfer fixes on
shells other than tcsh as well as both tested tcsh versions.

## Performance

Measurements compare a source snapshot from immediately before this change with
the final source. Each configuration discards ten warm-up commands. Two clean,
interleaved repetitions cover 4,000 shell command/prompt round trips and 64
Python worker lifecycles. No functional test ran concurrently with these
repetitions. Earlier provisional measurements overlapped a focused regression
run and are not used here.

The values below are the medians of the two per-run median latencies, in ms.
Shell measurements isolate the engine with a scripted UI; they are not a
measurement of every prompt-toolkit rendering or completion operation. Worker
measurements include interpreter spawn and cleanup.

| Path | Before, observation off | After, observation off | Before, observation on | After, observation on |
| --- | ---: | ---: | ---: | ---: |
| Bash | 3.744 | 3.918 | 4.122 | 4.249 |
| zsh | 4.619 | 4.528 | 4.972 | 5.566 |
| dash | 4.229 | 4.397 | 5.264 | 5.174 |
| tcsh | 20.581 | 20.042 | 21.581 | 20.782 |
| BSD csh, including explicit reconnect | 26.242 | 25.922 | 27.130 | 29.805 |
| Python worker | 1115.311 | 988.235 | 1113.464 | 969.113 |

The short runs showed an approximately 0.17 ms increase for default-mode Bash
and dash. To check that result, an additional 12,000 commands were run across
three interleaved 1,000-command repetitions per shell and source version:

| Default mode | Before, median of run medians | After, median of run medians |
| --- | ---: | ---: |
| Bash | 4.165 ms | 3.793 ms |
| dash | 4.452 ms | 4.306 ms |

Individual differences changed direction between repetitions. These results do
not show a consistent default-mode slowdown, but also do not prove zero overhead
or a speedup. The observed variation is too large to attribute small elapsed-time
differences to a few cached state checks.

A separate five-repeat measurement of 10,000 terminal-snapshot calls per repeat
measured **0.093 microseconds/call disabled** and **5.78 microseconds/call enabled**
on this WSL host. The disabled path makes no terminal syscalls. The enabled path
performs three read-only queries and records metadata, so its cost is real and
observation remains opt-in. It is sampled at transitions, not on every key or
on an idle timer. No performance result here establishes a bound on a slower
RHEL host, multi-day sessions, or arbitrary plugins.

- [Clean comparison samples](../dist/session-guard-benchmark-clean.json)
- [Longer Bash/dash comparison](../dist/session-guard-benchmark-long.json)
- [Snapshot-call costs](../dist/session-guard-observer-cost.json)

Reproduce the complete regression suite with the project virtual environment:

```sh
PYTHONPATH=src:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
```

Set `ISH_TEST_CSH` to the real BSD csh binary and `ISH_TEST_TCSH` to tcsh 6.20
when testing those implementations. Otherwise `csh` may resolve to a tcsh
symlink. For the deployment claim, repeat the tests on the actual RHEL 8.10 host
with its Bash 4.4.20, zsh 5.5.1, and site configuration.

Local evidence is retained under `dist/` and the reusable test programs under
`tests/`; both directories remain intentionally ignored by Git. The legacy
`tests/test.py` generator is excluded from discovery and was not imported.

This change does not rebuild the Nuitka executable. Source sessions must be
restarted; distributed executables require a separate rebuild on the appropriate
target platform.
