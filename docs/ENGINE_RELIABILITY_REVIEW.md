# Interactive shell engine reliability review

Review date: 2026-09-12. The original review did not change production code.
The subsequent prompt-capture and exit-status repairs are described in the
follow-ups below. The foreground-pipeline finding remains open and is deferred
at the user's request.

## Assessment

The engine has useful, complementary guards for ordinary interactive sessions:
fresh prompt/context generations, input attachment epochs, cancellable send
generations, bounded queues, backpressure, cached process-exit checks, and
catchable termination cleanup. These are stronger than inactivity-based shell
state guesses. They do not establish universal prompt recognition, exactly-once
execution across cancellation, or cleanup of every possible process tree.

Three additional edge cases were reproduced during this review. They should be
addressed before making an unconditional reliability claim. Their reproduction
does not mean ordinary command execution, editing, or TUI return fails generally.

## Newly reproduced findings

### P2: A truncated replay can swallow the next real prompt and hold input

Status: addressed by the subsequent prompt-capture repair. The reproduction
and evidence below describe the original behavior.

In an otherwise normal Bash ish session, run:

```sh
printf '%s' "$PS1" | /usr/bin/head -c 45
```

The current session marker is 41 bytes long, so this outputs a complete start
marker and only part of its prompt payload. `Sequencer._between()` keeps looking
for the corresponding end marker. The next genuine prompt ID is treated as
payload, not dispatched to `_set_prompt_id()`. Its following end marker closes
the old capture. Meanwhile, the FIFO has already delivered the new context ID.

The unit probe observed `prompt_id=1`, `context_id=2`, no prompt event, and
`_handoff_pending=True`. Subsequent `echo AFTER` input was retained in
`_deferred_input`. The real CLI likewise failed to return the editor or execute
the next input; an explicit Ctrl+C restored operation.

Relevant paths: `Sequencer._between()` in
[sequencer.py](../src/ish/shell/sequencer.py), and `_update()`, `_input()`, and
`_exec()` in [base.py](../src/ish/shell/base.py). The five-second context timeout
does not protect this direction of a partial handshake: `_exec()` has not yet
finished waiting for `prompt_event`.

Suggested correction: let a verified fresh session marker resynchronize an
incomplete capture while preserving ordinary output; continue requiring its
matching FIFO generation before accepting input ownership. Also bound a partial
handshake that has context but no prompt. Do not inject recovery commands or
classify ordinary input waits by inactivity. Add fragmented/truncated replay
coverage in both FIFO/PTY arrival orders. The existing bounded-prompt test only
checks eventual arrival of the old end marker, not this nested fresh prompt.

Evidence: `dist/engine-reliability-unit-probes.jsonl`,
`dist/engine-reliability-cli-probes.jsonl`, and
`dist/engine-fragmented-prompt.pty`.

### P2: Native shell exit status is lost at the ish process boundary

Status: addressed by the exit-status follow-up below. The reproduction here
describes the original behavior.

`exit 7` ended the inner shell but returned status **0** from ish on Bash, zsh,
sh/dash, tcsh, and BSD csh. `_shell()` reads the child's status for observation,
but `_run_session()` and `ShellSignalController.run()` do not return it through
the normal completion path. `InteractiveShell.run()` receives `None` and passes
it to `sys.exit()`.

This is separate from the stored `$?`/`$status` for normal commands. It causes a
parent script or launcher to see success after a nonzero native shell exit.
Signal-driven ish termination still returns the tested TERM/HUP status.

Suggested correction: propagate a natural child's exit result through
`_shell()`, `_run_session()`, and the controller, with an explicit convention for
signal exits. Retain the first requested TERM/HUP status and define editor-only
exit separately. Cover `exit 0`, nonzero exit, and signal death at the CLI level.

Relevant paths: [base.py](../src/ish/shell/base.py) and
[signals.py](../src/ish/shell/signals.py). Evidence:
`dist/engine-reliability-cli-probes.jsonl`.

### P2: A foreground pipeline can outlive shutdown after its leader exits

Status: open; remediation is explicitly deferred at the user's request.

The probe ran `true | python3 stubborn.py`, where the second process stayed in
the foreground pipeline and ignored TERM/HUP. The first process, which was the
process-group leader, had already exited. Sending SIGTERM to ish returned 143,
but the second process remained alive. The diagnostic then killed only its own
captured surviving process and checked that cleanup completed.

`ShellSignalController._signal_foreground()` checks ownership with
`os.getsid(group)`. A process-group ID is not necessarily the PID of a living
process. When that leader has exited, the ownership check returns false even
though the foreground group still has members. Both the initial TERM and later
KILL path can consequently skip that group. This is a foreground cleanup gap,
not an intentionally detached daemon or an uncatchable SIGKILL/OOM case.

Suggested correction: verify the foreground group's session ownership using a
live member, with PID-reuse protection, during shutdown. Preserve the bounded
grace period and group-level kill fallback without adding an idle poller or
supervisor. Add a leader-exits-first pipeline to the termination regressions.

Relevant path: `_signal_foreground()` and `stop_child()` in
[signals.py](../src/ish/shell/signals.py). Evidence:
`dist/engine-reliability-pipeline-final.jsonl` and
`dist/engine-pipeline-shutdown.pty`.

The first diagnostic's final cleanup used `psutil.wait_procs()` and encountered
a WSL pidfd EINVAL after killing the captured processes. That was a diagnostic
error. The final probe uses bounded checks of captured `Process` identities and
confirms the same product finding without that error.

## Guard coverage and limits

| Mechanism | What it establishes | Remaining boundary |
| --- | --- | --- |
| Session-scoped markers and matching increasing prompt/context IDs | A fresh integration boundary before the editor resumes | Incomplete marker capture needs the resynchronization correction above; not a hostile same-user security boundary |
| Attachment epochs and one input owner | Queued callbacks from a previous attachment cannot read for a later owner | Direct plugin reads are outside these guards |
| Send generations and ordered FD writes | Old senders cannot resume cancelled chunks; partial writes and EAGAIN are handled | Bytes already executed or consumed inside a shell cannot be recalled |
| Deferred/returned input ordering | Older forwarded PTY bytes precede newer editor/tool input at the normal handoff | Interrupts deliberately discard some submitted input; no universal execution acknowledgement |
| Cached process exit and separate PTY EOF | An exited shell cannot regain input; final output has a separate drain path | Natural exit status must still be propagated to the caller |
| Terminal observation | Bounded diagnostics of mode, foreground group, and size changes | Observations do not independently prove that a shell is reading a command |
| Gated long input | Stage only verified first-line contexts and restore TTY modes before newline/interrupt | Later long lines and dash are rejected; native shell limits and custom trap restrictions remain |
| Event-driven integration and explicit reconnect | No recurring shell commands used to guess whether an application is waiting | Removed hooks can leave native input; real BSD csh intentionally requires explicit reconnect |

## Validation scope

The host is WSL2 Linux `6.6.87.2-microsoft-standard-WSL2`, Python 3.12.14,
with Bash 5.3.9, an upstream zsh 5.5.1 build, tcsh 6.20.00, sh/dash, and actual
BSD csh selected for the compatibility run. Installed newer zsh/tcsh versions
are 5.9 and 6.24.13. These builds do not reproduce the corporate RHEL 8.10 kernel,
distribution patches, startup files, SSH client, or terminal settings. Bash
4.4.20 is not present in this workspace.

The full suite uses `test_*.py`; the side-effectful legacy `tests/test.py` is
excluded. Diagnostic scripts are separate from discovery and preserve existing
test assertions and timeouts. No production fixes or rebuild of the compiled
ish executable are part of this review. A separate zsh compatibility fixture
was rebuilt after identifying a local build configuration defect, as described
below.

## Existing regression results

| Run | Result | Evidence |
| --- | --- | --- |
| Full discovery, using the original local zsh 5.5.1 fixture and tcsh 6.20.00 | 273 tests in 1,146.599 seconds: 272 passed, one failed, none skipped | `dist/engine-reliability-full-suite.log` |
| Isolated repetitions of the failing zsh test, with the original fixture | Three repetitions passed in 27.556 seconds | `dist/engine-zsh-input-recheck.log` |
| Relevant existing regressions with the separately verified zsh 5.5.1 fixture | All 19 tests passed in 170.437 seconds | `dist/engine-zsh-verified-regressions.log` |
| Ruff lint and format verification | Passed for 69 Python files, including the four review diagnostics | `ruff check` and `ruff format --check` |

The full-run failure was
`test_long_input_cli.LongInputCLITests.test_exact_long_input_and_native_read_on_each_shell`,
in its zsh subtest. After the long-input and native terminal-mode checks, it
observed an empty `answer` file instead of `ac`. The fixture waits for file
existence, which does not establish completion of `Path.write_text()`, so an
assertion timing race is plausible. The original failed run did not retain
enough evidence to prove that explanation or rule out an intermittent input
problem. Later successful runs do not erase this failure.

The three isolated repetitions retained terminal transcripts, payloads, and
observer snapshots under `dist/engine-zsh-input-recheck/`. They confirmed the
expected input, a resumed editor, matching prompt/context generations, and no
observer issues. The verified-fixture matrix includes the same previously
failing test, all twelve existing long-input CLI test methods, six native zsh
integration tests, and the native zsh `read` test. Only the parameterized shell
matrix was restricted to zsh; explicit other-shell cases still ran. No assertion
or timeout was weakened. The entire 273-test suite was not rerun with the new
fixture, so this review does not report a clean full-suite run.

## Previous RHEL-related symptoms

| Earlier symptom | Result in this review | Compatibility limit |
| --- | --- | --- |
| zsh custom prompt replaced by a default hostname prompt | Global startup options and custom prompt preservation passed with zsh 5.5.1 | Uses local test startup files, not corporate site configuration |
| zsh `precmd:8: permission denied` after every command | Empty/unset, missing, failing, autoloaded, and repeatedly installed hook cases passed | No claim about unprovided site hooks or distribution patches |
| Bash initialization timeout and repeated prompt | Initialization and existing `PROMPT_COMMAND` variants passed on Bash 5.3.9 | Bash 4.4.20 was unavailable; the exact earlier RHEL configuration remains unverified |
| tcsh 6.20.00 `foreach` Ctrl+C followed by `_ish_precmd`, `_ish_postcmd`, or `_ish_periodic: Command not found` | Secondary-prompt cancellation, existing/empty hooks, and disabled-editor cases passed | Upstream 6.20.00 build on WSL, not the RHEL binary |
| tcsh Vim exit clears the screen or delays input; man exit replays its status line and `^M` | Dedicated tcsh cases and the cross-shell Vim/man return cases passed | Real TUI programs on this terminal/test environment |
| Helper commands pollute history, `!!`, or queued input | History, runtime hook replacement, `foreach` typeahead, and pasted-block regressions passed | Real BSD csh still uses its documented explicit reconnect path |

Other passing groups cover input attachment epochs, partial Unicode transfer,
Python-tool handoff, cancellation generations, blocked writes, long-input
admission, custom signal traps, cache isolation, TERM/HUP during initialization
and execution, terminal disconnect, and cleanup of the process shapes already
represented in the suite. The leader-exits-first pipeline finding adds a
previously uncovered process shape; it does not contradict those passed cases.

No recurrence of the earlier symptoms was observed in these exercised cases.
This is useful compatibility evidence, not a guarantee for RHEL 8.10 or Bash
4.4.20. A final corporate-host run should use the actual binaries, site startup
files, and terminal connection.

## Local zsh fixture defect found during repetition

The original local zsh 5.5.1 executable stalled during repeated engine commands.
An instrumented run wrote 287 of 600 records before its 90-second deadline.
The zsh process was asleep in `pause()` with no child process left to wait for.
An earlier uninstrumented run had also timed out. These are failed measurements,
not successful resource runs.

Inspection found `BROKEN_POSIX_SIGSUSPEND` incorrectly enabled in that fixture's
generated `config.h`. Its original configure log shows the legacy implicit-int
test program rejected by the modern compiler. That failed compilation was
interpreted as a broken `sigsuspend()`. The selected fallback in zsh's
`Src/signals.c` uses separate `sigprocmask()` and `pause()` calls, leaving a lost
wakeup window. This does not establish that the corporate RHEL zsh binary has
the same configuration problem.

A control experiment ran 2,000 native pipelines without ish or its integration
scripts. The old executable timed out after 15 seconds with status 124. A
separate copy was rebuilt with the additional legacy-compiler flag
`-Wno-error=implicit-int`; configure then correctly left
`BROKEN_POSIX_SIGSUSPEND` undefined. The verified executable completed the same
native experiment with status 0 and `NATIVE_DONE`. It also passed the 3,000-command
engine run and the 19 existing regressions reported above.

The old fixture was preserved. The replacement is
`dist/zsh-compat/zsh-5.5.1-verified/Src/zsh`, built by
`dev/rebuild_zsh_probe.sh`. Evidence is in
`dist/engine-zsh-native-control.jsonl`,
`dist/engine-zsh-verified-native.jsonl`, and
`dist/engine-zsh-verified-build.log`. No upstream zsh source patch was applied.

## Repetition and resource measurements

The successful runs executed 5,400 numbered append commands across five shells,
with exactly one record per command in the expected order. Each shell used an
isolated home and cache directory. BSD csh explicitly reconnected after each
command, matching its supported fallback. These measurements drive the real
engine and PTY with a scripted prompt object; they are not a multi-day soak or
a continuous prompt-toolkit/TUI interaction benchmark. Actual UI interactions
are covered separately by the existing CLI tests.

| Shell | Successful commands | Process RSS at command 100 / final command (MiB) | First / final 100-command median round trip (ms) |
| --- | ---: | ---: | ---: |
| Bash 5.3.9 | 600 | 29.42 / 30.67 | 3.79 / 4.79 |
| sh/dash | 600 | 30.12 / 30.75 | 5.88 / 4.41 |
| tcsh 6.20.00 | 600 | 31.38 / 33.00 | 21.57 / 23.27 |
| BSD csh | 600 | 33.38 / 35.00 | 33.89 / 32.66 |
| Verified zsh 5.5.1 | 3,000 | 29.50 / 31.75 | 4.34 / 4.18 |

At every 100-command sample in these runs, the harness process had 15 open file
descriptors, four pending asyncio tasks, one shell child, and two engine FD
writers. The sampled input and output queues were empty. The observer reported
no issues and stopped growing at its 4,096-event capacity. After each engine
session, counts returned to the fixture baseline of ten descriptors, one
pending task, and no child; no session directory remained.

RSS was not flat. Scrollback was still filling, and Python retained allocations.
For example, the zsh run retained 711,792 scrollback bytes at command 3,000,
below the configured 10 MiB cap; its RSS rose from 30.75 MiB at command 600 to
31.75 MiB at command 3,000. This supports bounded resource behavior in the tested
interval but does not demonstrate a final memory plateau. Resource counts are
for the diagnostic parent process, not aggregate memory of every descendant.
Several shell runs share one Python process, so their absolute RSS values should
not be used to rank shell memory usage.

Round-trip time includes command execution, file appending, integration, and
scheduling. The sampled medians do not show a large sustained increase, but
they are not direct measurements of human key-to-screen latency. No zero-cost
or long-duration performance guarantee follows from them.

Twenty-two additional Python workers returned the expected status 7. Samples
after workers 2, 12, and 22 were identical: 35.625 MiB RSS, 11 descriptors, one
pending task, and one shared multiprocessing resource-tracker child. Workers
themselves did not accumulate.

Evidence: `dist/engine-reliability-soak.jsonl`,
`dist/engine-reliability-soak-followup.jsonl`, and
`dist/engine-zsh-verified-soak.jsonl`. The follow-up file intentionally retains
the failed old-zsh measurement alongside the successful later shell runs.

## Reproduction entry points

Run from the repository root in WSL. The environment overrides select actual
BSD csh and the legacy compatibility binaries rather than a csh-to-tcsh symlink
or newer installed shells:

```sh
export PYTHONPATH=src:tests
export ISH_TEST_ZSH="$PWD/dist/zsh-compat/zsh-5.5.1-verified/Src/zsh"
export ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh"
export ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh"

# Full existing suite; deliberately excludes tests/test.py.
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v

# Review findings, without production changes.
.venv/bin/python -B dev/engine_reliability_probe.py unit
.venv/bin/python -B dev/engine_reliability_probe.py cli

# Existing zsh regression subset, native control, and repeated engine commands.
.venv/bin/python -B dev/engine_zsh_input_recheck.py --matrix
.venv/bin/python -B dev/engine_reliability_probe.py native
.venv/bin/python -B dev/engine_reliability_soak.py --shells zsh --count 3000 --skip-workers
```

Diagnostic JSON is evidence, not an assertion suite: inspect `error`,
`exact_order`, and each finding's observed result rather than treating a zero
diagnostic process exit as a product pass. The pipeline probe cleans only its
own captured descendant processes after recording the shutdown defect.

## Follow-up: prompt capture repair

The parser now retains the active prompt capture independently of the ANSI
lexer's state. An ESC inside a capture enters the existing lexer. Registered
session controls are dispatched normally, while ordinary text and unrelated
controls stay in the capture. This includes zsh reader-readiness and interrupt
acknowledgements, so resynchronizing a prompt does not lose those replies.

Adapters mark only the prompt-ID prefix as a capture restart. Its existing
callback must accept a valid increasing ID before the old capture is released.
Stale, malformed, or foreign IDs cannot release it; a repeated prompt-start
marker by itself is not sufficient. On release, the retained contents are
emitted once in stream order, without their old opening marker and without
calling the abandoned prompt callback. Previously emitted passthrough contents
are not duplicated. Capture and control buffers retain their existing size
limits; a truncated capture includes the existing truncation notice.

The existing engine remains responsible for input ownership. A fresh ID alone
does not return the editor. A complete new prompt and the matching FIFO
generation are still required by `_accept_prompt()`. The older typeahead FIFO
is still drained before newer deferred input. No recovery command, synthetic
input, new timeout, or periodic check was added by this repair. If the shell
never sends a complete fresh prompt, this change cannot reconstruct one.

Controls inside an intact prompt are rendered by the prompt UI and do not
claim to have changed native terminal modes. When a stale capture is released
as terminal output, a bounded observer-only parse records its controls without
replaying any shell-integration callback. This exceptional path preserves mode
cleanup for the bytes that now actually reach the terminal.

`tests/test_prompt_resync.py` adds split-boundary coverage for primary,
continuation, buffered, and caret captures; malformed/foreign IDs; zsh replies;
bounded oversized captures; EOF; passthrough output; terminal observation; both
FIFO arrival orders; and real-shell queued input after a partial replay.

The alternating before/after parser benchmark is recorded in
`dist/prompt-resync-benchmark.jsonl` and can be repeated with
`PYTHONPATH=src .venv/bin/python -B dev/benchmark_prompt_resync.py` while HEAD
still contains the original parser. Plain and colored output showed no
observed median slowdown in this run. The synthetic 5,000-prompt stream took
0.419 seconds before and 0.490 seconds after: about 17% extra parser time, or
0.014 milliseconds per prompt. This is not a measurement of interactive input
latency; it reflects the additional parsing of controls inside prompt text.

Final follow-up validation: **282 tests passed in 1,219.453 seconds**, with no
failures or skips (273 existing tests plus nine new tests). Evidence:
`dist/prompt-resync-full-suite.log`. This full run used the verified zsh 5.5.1
fixture, tcsh 6.20.00, actual BSD csh, Bash 5.3.9, and sh/dash on WSL. It includes
long input, interruption, native input waits, Python tools, TERM/HUP cleanup,
runtime hook replacement, history, and Vim/man return. The previously failing
zsh long-input/native-read assertion also passed in this full run; this does
not retrospectively establish the cause of the earlier failure.

This is source-execution validation on the local WSL host, not a new test on
the corporate RHEL 8.10 system or a rebuilt Nuitka executable. The other two
P2 findings remain outside this repair.

## Follow-up: exit status propagation

The native child wait result now passes through `_shell()`, `_run_session()`,
the signal controller, and `main()` to `run()`. Final output and resource cleanup
still finish first. Async callers receive the raw child status; the CLI maps a
negative signal result to 128 plus the signal number. A requested TERM/HUP exit
still takes precedence, and the explicit `ish_exit` editor command returns zero
without adopting its cleanup-induced child HUP status.

The deferred foreground-pipeline cleanup logic is unchanged. See
[Exit status and Linux syscall validation](EXIT_STATUS_SYSCALL_VALIDATION.md)
for the exact contract, test scenarios, results, and environment limits.
