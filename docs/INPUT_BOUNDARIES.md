# Input boundaries and maintenance

This change addresses prompt replay, interruption during queued submissions, and
the environment dependencies and process cost of shell maintenance. Long-line
transport and SIGTERM cleanup were outside this change. The later
[termination update](TERMINATION.md) addresses catchable shutdown independently.

## Prompt confirmation

A primary prompt is staged only when its generation is newer than the last
accepted generation. The engine keeps forwarding input until the state FIFO
contains the same generation. A replay of the current PS1 is displayed as ordinary
text. It does not stop input forwarding or trigger a recovery write.

The engine no longer sends `source`, a refresh command, or a newline to recover
from a marker without a fresh state report. If reporting hooks are removed,
the user can run `ish_recover` at a known native command prompt. The command must
not be entered while an application is waiting for data.

Bash, zsh, and tcsh report through their existing hooks. Dash now reports from
PS1 expansion, avoiding the previous injected refresh command. Because dash
expands PS1 in a subshell, the helper supplies a monotonic generation instead of
relying on mutations of a parent-shell variable.

BSD csh lacks an equivalent prompt hook. Its safe fallback keeps native input
after a command; an explicit `ish_recover` returns to the ish editor and refreshes
context. This is a compatibility change for real BSD csh. A `csh` symlink to tcsh
uses tcsh's normal automatic reporting. No background inactivity probes were added.

These signals prevent accidental current-session replay. They are not a security
boundary against a program deliberately invoking the integration helper or writing
to the current user's private FIFO.

## Submission interruption

The raw-input reader is installed before transmission starts. The sender yields
even when a write can complete immediately. Normal keystrokes received during a
submission are queued separately so they cannot split a submitted command.

When VINTR arrives while terminal signal processing is enabled, the engine:

1. Cancels the sender and discards its pending FDWriter bytes.
2. Discards deferred input and invalidates earlier prompt candidates.
3. Flushes the master output queue and the slave input queue.
4. Forwards VINTR and waits for a fresh prompt/state confirmation.

Typeahead already moved into the FIFO by an earlier prompt hook is also discarded
through the interrupt boundary, so it cannot reappear as a later editor submission.

Flushing master input would incorrectly discard shell output. A short-lived
slave descriptor from `TIOCGPTPEER` allows the correct queue to be flushed without
keeping the slave open for the session lifetime. This ioctl is available since
Linux 4.13, preceding the target RHEL 8 kernel. See the
[Linux PTY ioctl documentation](https://www.man7.org/linux/man-pages/man2/TIOCPKT.2const.html).

Commands already executed cannot be undone. Shell/parser buffers already consumed
by the shell are handled by its normal interrupt behavior. Programs that disable
ISIG retain their ordinary byte-oriented Ctrl+C handling.

An interrupted state frame is discarded when the decoder encounters a new
versioned frame header. Old partial data cannot contaminate the next prompt state.

## Maintenance dependencies and cost

The existing C helper now streams base64 context frames and reads its inherited
exported environment. Each update replaces four `base64` invocations and one `env`
invocation with one helper invocation. Alias collection remains in the shell.
All helper paths are absolute; C shell `printf` is resolved at startup. tcsh's
existing periodic callback uses the helper clock instead of a PATH lookup for
`date`. User PATH values are preserved.

A Bash benchmark measured 100 `true` command/prompt round trips after startup,
using the previous and current script templates with the same engine and helper:

| Run | Previous templates | Current templates |
| --- | ---: | ---: |
| 1 | 2.3098 s | 0.3737 s |
| 2 | 2.2994 s | 0.3911 s |
| 3 | 2.3241 s | 0.4075 s |

This measures short-command maintenance on WSL, not a general application speedup
or a measurement on RHEL. Log: `dist/context-benchmark.log`.

Bash's per-PS2 readiness substitution remains. Its non-consuming check happens
before Bash reads the next line; inspecting a Python queue later cannot recover
that timing reliably. Removing it requires a separate input-consumption protocol
or a deliberate change to continuation-prompt display. The current change does
not claim to eliminate this remaining per-secondary-prompt subshell cost.

## Validation

Regression coverage includes same-session PS1 replay followed by `read`/`$<`,
empty PATH with exit-status preservation, interrupted 50,000-line submissions,
interruption while a command is running and while Bash is reading a block,
preservation of terminal output while flushing input, exact binary context
round trips, and recovery from every truncated-frame byte position.

Existing tests cover multiline paste, heredocs, secondary prompts, user hooks,
history, output buffering, and returning from Vim and man. Tests use isolated
homes, private PTYs, and session directories. The legacy `tests/test.py` generator
is not imported.

Results on WSL with Python 3.12.14:

- Full suite: **190 tests passed in 294.427 seconds**, with real BSD csh selected
  through `ISH_TEST_CSH`. Log: `dist/input-final-suite.log`.
- tcsh 6.20.00 compatibility set: **19 tests passed in 94.587 seconds**.
  Log: `dist/input-tcsh620.log`.
- After the final writer-close callback adjustment: **10 related tests passed**.
  Log: `dist/input-writer-final.log`.
- Ruff lint and format checks passed for all 21 changed/related Python files.
- The C helper compiled with `gcc -O3 -Wall -Wextra -Werror` without warnings.

The tcsh version matches the reported deployment version, but it was run on WSL,
not inside the corporate RHEL environment. Existing sessions retain their old
private scripts; restart source sessions to apply the change. Existing Nuitka
executables require a rebuild; this work did not rebuild the distribution binary.
