# Shell signal policies and responsibility review

## Module names

All signal policies, including `ZshInterrupt` and `LineInterrupt`, are now in
`signals.py`. They remain separate classes: combining the files does not enable
the zsh policy on other shells or change the adapter registration interface.

| Previous import module | Current import module |
| --- | --- |
| `ish.shell.zsh_signals` | `ish.shell.signals` |
| `ish.shell.signal` | `ish.shell.request` |
| `ish.shell.long_input` | `ish.shell.input` |
| `ish.shell.output_line` | `ish.shell.prefix` |
| `ish.shell.terminal_state` | `ish.shell.state` |
| `ish.fdio` | `ish.runtime.fdio` |
| `ish.input_observer` | `ish.runtime.observer` |
| `ish.ui.observed_input` | `ish.ui.input` |

Project imports, test patches, and generated test startup snippets use these new
paths. External plugin/startup code importing the old modules needs the same
update. No compatibility modules retain the old filenames. `input.py` owns
submission validation and temporary command-input TTY settings; the engine still
owns the input buffers and transport order.

## Configuration

`ShellAdapter.signal_policy` selects optional overrides at session construction.
Omitting it, or using `SignalPolicy()`, retains the common defaults. It does not
install handlers for every OS signal.

```python
import signal
import termios

from .signals import ProcessSignal, SignalPolicy, TerminalSignal, ZshInterrupt

# The existing zsh adapter selects this terminal policy.
signal_policy=SignalPolicy(
    terminal=(TerminalSignal(signal.SIGINT, termios.VINTR, ZshInterrupt),),
)
```

The two registration namespaces are independent:

| Registration | Input | Default behavior |
| --- | --- | --- |
| `ProcessSignal` | A signal delivered to the ish process | TERM/HUP request one shutdown; WINCH resizes the PTY and Python tool |
| `TerminalSignal` | A configured TTY control byte during submission or handoff | VINTR cancels the pending transmission; native input otherwise goes directly to the PTY |

Adapter entries override the common entry with the same signal number. A new
number adds a handler. `handler=None` removes ish's binding; it does **not** set
the signal disposition to `SIG_IGN`, consume a key, or change the shell's traps.
For example, `ProcessSignal(signal.SIGUSR1, on_usr1)` adds a process callback with
the signature `on_usr1(controller, signum)`. Such callbacks run synchronously on
the event loop and must not block. They can request orderly termination through
`controller.request_shutdown(signum)` when that is their intended policy.

`ProcessSignal(signal.SIGWINCH, None)` opts out of ish's WINCH registration.
Prompt-toolkit's resize callback uses the same selected policy while its editor
owns the OS handler, so an override or opt-out also applies at the editor prompt.
`TerminalSignal(signal.SIGINT, termios.VINTR, None)` removes ish's submission-time
VINTR interception: bytes follow the existing normal/deferred input path. Long
staged submissions are then rejected because interruptible transport requires a
registered terminal SIGINT policy. Short commands retain their existing path.

Process registrations have a lifetime: `SignalScope.STARTUP` is installed before
session resource acquisition and restored after cleanup; `SignalScope.RUNTIME`
is installed after the PTY and I/O callbacks exist. WINCH uses the latter. Each
scope also unwinds registrations if a later installation fails. The first TERM
or HUP determines the exit status; later requests do not cancel cleanup again.

A terminal handler is a `TerminalSignalHandler` subclass constructed separately
for each session. Its `handle(control, data, phase)` receives the complete input
batch and either `InputPhase.SUBMISSION` or `InputPhase.HANDOFF`. The handler owns
the disposition of that batch and any bytes following the control key. For
multiple registered keys, the first key in byte order selects the batch owner.
Actual `termios` control slots and `ISIG` are respected. Unregistered keys retain
the previous behavior, including deferred VQUIT/VSUSP during a transmission.
This refactor does not implement new SIGQUIT or job-control semantics.

Stateful policies can implement `pending`, `hold_input`, `reset`,
`configure_sequencer`, and `validate_submission`. A pending cancellation policy
retains ownership of incoming batches until the engine confirms a fresh prompt.
It must preserve later input and release its deadlines on reset. Shell-side trap
and hook syntax remains in `scripts.py`; adding a policy requiring a protocol
reply also requires a matching integration template. Removing a Python policy
does not edit user traps or remove existing shell-side hooks. Adapter/script
changes are applied to new sessions, not hot-swapped into an active reader.

The default `SubmissionInterrupt` restores an active TTY lease before invoking
the engine's queue-cancellation operation. `ZshInterrupt` adds the existing
generation-checked acknowledgement and single-LF release for a staged prefix.
Bash does not construct the zsh state, register its protocol callbacks, wait for
its response, or run its timer. Zsh trap preservation and shell hook ordering
remain in the unchanged integration template.

Only the main thread can register OS handlers. A session must own the selected
event-loop signal registrations; the public asyncio API does not expose an
arbitrary previous asyncio callback for restoration. Previous Python signal
dispositions are restored. Multiple embedded sessions cannot independently own
the same process-wide signals. This is an existing ownership boundary, not a
new claim of concurrent-session support. SIGKILL and SIGSTOP registrations are
rejected before resource acquisition. The child watcher and Python-worker
reaping paths retain their own ownership; this registry does not intercept
SIGCHLD or add a supervisor.

## Responsibility review

| File / class | Responsibility | Assessment |
| --- | --- | --- |
| `base.py` / `InteractiveShell` | Acquire the session, sequence commands, own PTY/FIFO buffers, transfer input ownership, and coordinate output and cleanup | Still the largest coordinator, but it no longer selects OS signal handlers or implements the zsh cancellation protocol |
| `base.py` / `ScrollBack` | Bounded history and last-output retention | Cohesive standalone class; could move to `scrollback.py` later without redesigning the engine |
| `signals.py` / `ShellSignalController` | Resolve registrations, own their lifetimes, dispatch terminal policies, coalesce shutdown, and signal/reap the owned child | Signal decisions are centralized; descriptor closing and final output remain with the engine |
| `signals.py` / policy and binding classes | Immutable adapter configuration and per-session terminal behavior | OS signals and TTY keys are explicitly separate; new registrations do not mutate other adapters |
| `signals.py` / `ZshInterrupt`, `LineInterrupt` | Zsh capability, acknowledgement, timeout, and cancellation input hold | Kept as optional policy classes in the common signal module; only the zsh adapter constructs their state |
| `adapters.py` / `ShellAdapter`, `ShellSyntax`, `ShellBehavior` | Select executable arguments, syntax, ordinary runtime behavior, and signal policy | Declarative selection remains centralized; low-level readers and OS registrations do not belong here |
| `input.py` / `InputModeLease` and admission helpers | Check physical-line limits and compatible terminal modes; temporarily change and restore TTY attributes | Owns the reversible terminal operation and admission checks; input queues remain with the engine |
| `sequencer.py` / `Sequencer` | Incrementally parse PTY controls, prompt boundaries, and echo masking | Appropriate stream parsing boundary; callbacks decide meaning outside the parser |
| `protocol.py` / `FrameDecoder` | Decode bounded FIFO state frames and recover from interrupted frames | Separate from terminal controls and session policy |
| `context.py` / `ShellContext` | Parse/store state categories and issue change callbacks | Narrow responsibility; no transport ownership |
| `prefix.py` / `OutputLine` | Retain the relevant linear output prefix for prompt display | Separate from full screen emulation and raw transport |
| `state.py` / `TerminalState` | Observe terminal modes and generate minimal shutdown restoration controls | Appropriately separate from termios leases and signal dispatch |
| `scripts.py` | Render per-shell integration syntax, hooks, and protocol producers | Cohesive but large; per-shell template files are a possible later maintenance improvement |
| `integration.py` | Install generated scripts and build/copy the native helper | Compiler cancellation belongs to the compiler resource owner; it should not use interactive-shell foreground policy |
| `constants.py` / `SessionSignals` | Wire markers, script names, state keys, and session tokens | These are protocol markers, not OS signals |
| `limits.py` | Engine transport, queue, history, and diagnostic budgets | Names express purpose; equal values do not force unrelated limits to change together |
| `request.py` / request exceptions | Editor requests for session exit or a Python tool | The name distinguishes editor control-flow requests from OS signal handling |
| `__init__.py` | Export `InteractiveShell` as the package entry point | Contains no session policy or resource acquisition |

The separation is suitable for incremental extensions. It is not a complete
decoupling of the session state machine: policies still read the engine's
ownership/phase state and invoke its transport operations. Keeping the actual
buffers in one owner avoids introducing a second queue or copying input during
cancellation. If more policies require many new engine fields, introduce a
narrow explicit context interface at that point instead of allowing policy
modules to own competing copies of state.

The largest remaining optional extractions are `ScrollBack`, the output queue,
and subprocess/session resource acquisition. None requires changing the current
input protocol merely to shorten a file. This change intentionally leaves those
stable paths together and avoids a broad state-machine redesign.

## OutputLine and Sequencer

Keep these responsibilities separate. Both inspect terminal control bytes, but
they answer different questions and have different state lifetimes.

| Aspect | `Sequencer` | `OutputLine` |
| --- | --- | --- |
| Purpose | Recognize streamed controls, ish boundaries, and one-shot echo masking | Determine which output can safely be copied before the next editable prompt |
| Result | Display bytes plus ordered protocol/output callbacks | A bounded candidate prefix, or an empty prefix when unsafe |
| Applicability | Every supported shell | Enabled by `preserve_output_line`, currently the csh/tcsh adapters |
| Lifetime | Preserve fragmented stream state across reads and command boundaries | Reset for a new command; invalidation lasts for the rest of that command |
| Cursor movement, erasure, alternate screen | Preserve controls for the real terminal | Invalidate the candidate so TUI/status text cannot enter the editable prompt |
| Output device ownership | Return bytes to the engine | Observe bytes only; never write to a terminal |

The existing path is `PTY -> Sequencer -> output observer -> OutputLine`, while
the engine queues the returned bytes for actual terminal output. On recognizing
a prompt boundary, the sequencer delivers preceding output to the observer
before invoking the prompt callback. The engine freezes the prefix there.
Output arriving after that boundary, even in the same PTY read, must not enter
the saved prefix. This ordering is covered by the existing TUI-return tests.

For example, `CSI K` (erase line) is valid output that the sequencer preserves.
The prefix tracker must discard its candidate after that same control. Resetting
a combined parser at every command to satisfy prefix tracking could discard an
incomplete control sequence; retaining prefix state for the whole stream could
instead replay text from an earlier command or TUI. A single class would still
need two distinct states and lifetimes. Putting both classes into one file would
not remove their small overlap in lexical control recognition.

The prefix tracker now lives in `prefix.py`; its class remains `OutputLine`.
Terminal mode tracking lives in `state.py`, retaining the `TerminalState` class.
These file renames preserve behavior and the separation from `Sequencer`.

If terminal syntax needs broader support later, consider an ordered shared
text/control event interface while retaining a separate prefix policy. That is
a parsing change requiring fragmented-control, echo, callback-order, and Vim/man
regressions; it is not needed for the current module consolidation.

## Shared infrastructure outside shell and UI

`ish/runtime/fdio.py` and `ish/runtime/observer.py` serve multiple execution
paths, so they live in the common `ish.runtime` package.

| Module | Consumers | Responsibility |
| --- | --- | --- |
| `ish.runtime.fdio` / `FDWriter` | Interactive shell and Python tool worker | Ordered, nonblocking file-descriptor writes, backpressure, and pending-byte handoff |
| `ish.runtime.observer` / `InputObserver` | Interactive shell, prompt UI, observed toolkit input, and Python tool worker | Bounded diagnostics for input ownership and terminal state; no input routing or terminal mutation |

Neither module depends on shell syntax or prompt-toolkit. Moving either into
`ui` would place shared runtime code under presentation; moving them into
`shell` would make the independent Python tool path depend on the interactive
shell package for shared infrastructure. In particular, importing a `shell`
submodule also runs the package initializer, which imports `InteractiveShell`.

The `runtime` package initializer only documents its purpose; it does not
eagerly import shell or UI modules. The toolkit-specific input wrapper belongs
in `ui/input.py`. Its class remains `ObservedInput`, while the shared diagnostic
class remains `InputObserver`. No reader, queue, or observation policy changes
are needed for this move.

## Named budgets

The repeated value in `base.py` was **65,536**, not 65,535. No numeric limits were
increased or decreased.

- `STREAM_CHUNK_BYTES`: 64 KiB work chunks for transmission/output draining.
- `DIAGNOSTIC_TAIL_BYTES`: 64 KiB retained input/startup diagnostic tails.
- `TYPEAHEAD_LIMIT_BYTES`: 1 MiB for deferred or returned input buffers.
- `OUTPUT_QUEUE_LOW_BYTES`, `OUTPUT_QUEUE_HIGH_BYTES`, and
  `OUTPUT_QUEUE_LIMIT_BYTES`: distinct 1/2/4 MiB output queue thresholds.
- `DEFAULT_READ_CHUNK_BYTES`, `FIFO_READ_CHUNK_BYTES`, `SCROLLBACK_MAX_LINES`, and
  `SCROLLBACK_MAX_BYTES`: the existing default read/history budgets.

The parser's maximum control-sequence size and Linux's canonical line limit are
different concepts and do not share the transport chunk constant.
Queue-limit error messages derive their displayed size from the same budget.
The repeated prompt-generation bounds also use `PROMPT_ID_LIMIT` and
`PROMPT_ID_MAX_DIGITS` in `constants.py`, shared by OSC, FIFO, and zsh replies.

## Validation

The initial focused run passed all 39 existing admission, cancellation, input
transfer, terminal-state, worker, and termination unit tests. Policy tests cover
real SIGUSR1 dispatch, optional removal, per-shell overrides, state isolation,
registration rollback, native-input syscall guards, unchanged input disposition
when a terminal handler is omitted, and the prompt-toolkit resize callback.

Validation ran on WSL with Python 3.12.14. `tests/test.py` is excluded from
discovery and linting. No source changes to the shell integration templates were
needed for the refactor.

- Full regression: **272 tests passed in 1,195.732 seconds**, with no skips.
  Log: `dist/signal-refactor-full-suite.log`. This includes Bash, zsh, sh/dash,
  tcsh, actual BSD csh, and the configured tcsh 6.20 compatibility binary.
  Coverage includes input ownership, long-input cancellation, paste/typeahead,
  Python tools, initialization/termination cleanup, hooks/history, prompt
  rendering, and Vim/man return. The final policy/resize follow-up below also
  covers the extra toolkit-policy regression added during the full run.
- Final policy/resize follow-up: **18 tests passed in 24.822 seconds**, including
  all 12 policy tests and existing Python worker/real CLI resize tests. Log:
  `dist/signal-refactor-policy-resize.log`.
- Upstream zsh **5.5.1** compatibility: **18 tests passed in 224.951 seconds**.
  Log: `dist/signal-refactor-zsh-551.log`. This checks the original zsh version
  on WSL; the corporate RHEL 8.10 kernel and Bash 4.4 host were not available.
- Ruff lint passed; formatting passed for all **65 Python files**. The working
  diff also passed `git diff --check`.

To repeat the supported-shell suite in this workspace:

```sh
ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh" \
ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh" \
PYTHONPATH=src:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
```

These tests demonstrate no observed regression in the exercised cases. They do
not certify arbitrary future signal policies or the unavailable corporate RHEL
kernel/configuration. Previously compiled distribution binaries are unchanged.

### Module consolidation follow-up

The broader regression results above precede the file consolidation. After
combining the zsh policies and renaming the input/request modules, all **76
selected regressions passed in 118.336 seconds**, with no skips. Log:
`dist/shell-module-consolidation-tests.log`. The run used the zsh 5.5.1 and tcsh
6.20 compatibility binaries and actual BSD csh, alongside Bash and sh/dash.

Coverage includes policy registration, reader acknowledgement, input admission
and transfer, catchable termination, native startup/hooks, fragmented output and
prompt boundaries, Vim/man return, exact long-input transmission, blocked-prefix
Ctrl+C, post-interrupt typeahead, and dash rejection with Python tools retained.
The moved zsh class bodies were also verified AST-identical before deleting
their former module. Existing tests were updated to import the new module paths;
no output parser or prefix-tracking behavior changed.

Ruff lint and formatting passed for all **64 remaining Python files**, and
`git diff --check` passed. The old import paths remain only in the migration
table and historical backup/artifact data, not in active source or test imports.

### Prefix and state module rename follow-up

After renaming `output_line.py` to `prefix.py` and `terminal_state.py` to
`state.py`, all **47 selected regressions passed in 15.687 seconds**, with no
skips. Log: `dist/shell-prefix-state-rename-tests.log`. The WSL run covered native
startup/hooks for Bash, sh/dash, zsh 5.5.1, tcsh 6.20, and BSD csh, plus Vim/man
return, fragmented prefix/mode tracking, termination unit tests, ordered writes,
input observation, and input handoff. Class names and behavior are unchanged.

Ruff lint and formatting passed for all **64 Python files**, and
`git diff --check` passed. That run preceded the subsequent move of shared FD
transport and input diagnostics into `ish.runtime`.

### Shared runtime and UI input module move

`ish.fdio` moved to `ish.runtime.fdio`, `ish.input_observer` moved to
`ish.runtime.observer`, and `ish.ui.observed_input` moved to `ish.ui.input`.
Production imports, test imports, mock targets, and the benchmark import use the
new paths. The runtime module contents are byte-identical to their originals;
the UI wrapper differs only in its observer import. Class names are unchanged.

The final WSL run passed all **71 selected tests in 66.335 seconds**, with no
skips. Log: `dist/runtime-module-move-tests-recheck.log`. Coverage includes
ordered writes and backpressure, diagnostic observations, stale-reader and
cancellation guards, input handoffs and Unicode fragments, real Python tool
startup/exit/resize, and native Bash, sh/dash, zsh 5.5.1, tcsh 6.20, and BSD csh
startup/hooks.

The initial run passed 70 tests but hit the five-second deadline in
`test_worker_sentinel_can_precede_actual_exit`. That test passed unchanged in
isolation (2.955 seconds), then in the complete 71-test rerun. Its initial timeout
was not reproduced; the cause is unconfirmed. Initial and isolated logs are
`dist/runtime-module-move-tests.log` and
`dist/runtime-module-move-sentinel-recheck.log`. No timeout was extended and no
test was skipped to obtain the passing run.

Ruff lint and formatting passed for all **65 Python files**, and
`git diff --check` passed.
