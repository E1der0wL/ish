# Stability review

Review date: 2026-09-11.

## Assessment

ish is suitable for controlled evaluation of its normal interactive workflows.
Its streaming design includes bounded transport buffers, output backpressure, incremental
control parsing, separate session files, and explicit prompt/context synchronization.
Keeping prompt-toolkit as the primary editor is compatible with this design.
Native primary-prompt editing and Ctrl+C exit-status behavior are intentionally
different from the underlying shell and are not defects in this assessment.

An unconditional production-ready claim would be premature. Termination cleanup
and worker sizing have since been fixed in source; the existing Linux artifact
still cannot run directly on the stated RHEL 8 target. Long-running operation also
needs more evidence than the existing short functional checks.

The original review changed documentation only. The follow-up implementation and
its validation are recorded alongside the corresponding finding below.

## Reproduced findings

### P2, fixed in source: SIGTERM bypasses terminal and session cleanup

Priority was changed to P2 and implementation was initially deferred at the user's
request. The later source fix routes SIGTERM/SIGHUP through one awaited shutdown,
including session initialization. See [termination behavior](TERMINATION.md) for
the implementation, validation, and remaining limits. The reproduction below
describes the earlier implementation and previously built executable.

Relevant code: `InteractiveShell.main` in [base.py](../src/ish/shell/base.py).
The session switches the outer terminal to raw mode and registers resource cleanup
with `ExitStack`, but installs only a SIGWINCH handler. An external SIGTERM does
not unwind the normal asynchronous cleanup path.

In separate source and onefile sessions, the probe waited for the tcsh primary
prompt and sent SIGTERM to the process it had launched. After exit, the PTY's
termios settings differed from their original values and one `session-*` directory
remained in each isolated cache. The onefile extraction directory was removed,
which did not restore the terminal or remove the shell session files.

This can leave the caller's terminal with altered echo/input behavior when ish is
terminated by a process manager or `kill`. When this deferred item is addressed,
route catchable termination signals through task cancellation and awaited cleanup, preserve prior
signal handlers, and check behavior both at the prompt and while a child TUI runs.
SIGKILL and power loss require a separate abandoned-file policy; they cannot use
an in-process cleanup handler.

### P2, fixed in source: Internal Python tools receive an unset terminal size

Relevant code: `ProcessHandler.run` in [pytool.py](../src/ish/app/pytool.py).
At review time, the worker received a newly created controlling PTY without
initializing its window dimensions or forwarding subsequent resize events.

The probe used an outer terminal with 24 rows and 100 columns. A shell command
reported `24 100`, while an importable tool registered with `set_tool` reported
`os.terminal_size(columns=0, lines=0)`. Both source and onefile runs reproduced
this result. Python tools that use curses, pagers, or terminal-size-based layouts
can therefore render incorrectly even when ordinary shell-launched Vim works.

Follow-up: `ProcessHandler` now receives a lazy getter for `InteractiveShell.master_fd`
and copies its current dimensions before starting a worker. The shell's existing
resize path updates its own PTY first and then notifies the active workers. This
does not install or replace another signal handler. Finished, cancelled, and failed
workers are removed from resize targets before their descriptors are closed.
Without a usable terminal, standalone callers receive a 24-row, 80-column fallback.

Six regressions in `tests/test_pytool_resize.py` passed: late PTY allocation, initial
size and actual worker SIGWINCH delivery, non-terminal fallback, cancellation,
spawn failure, and end-to-end Bash/tcsh resizing across tool and editor transitions.
The complete source suite then passed **169 tests in 234.632 seconds**, with no
failures or skips; Ruff lint and format checks also passed. The follow-up log is
`dist/pytool-resize-tests.log`.
The existing `dist/ish` was not rebuilt and still has the originally observed defect.

### P1 for RHEL 8 distribution: The current artifact requires newer glibc

The SHA-256 of the inspected `dist/ish` matches the artifact in the
[distribution validation report](DISTRIBUTION_VALIDATION.md):

```text
4afb3b24179664c578182f0dfa5bcea44986d4e5e04771d5c0a69f4915e5e13a
```

`readelf --version-info dist/ish` still reports GLIBC_2.38 among its requirements.
The stated RHEL 8 target provides glibc 2.28. This is a build-target mismatch;
onefile packaging does not remove it. Rebuild in a RHEL 8-compatible environment
and run the resulting artifact on the actual target, including its Bash 4.4.20
and zsh 5.5.1 startup configuration. Nuitka documents the general
[glibc compatibility constraint](https://nuitka.net/doc/commercial/portable-linux-support.html).

## Evidence and reproduction

The local source suite passed all **163 tests in 268.672 seconds**, with no failures
or skips. It ran with Python 3.12.14 on WSL, using real Bash, zsh, dash, tcsh, and
an extracted BSD csh executable. Ruff lint and format checks also passed (56 Python
files formatted). The legacy `tests/test.py` generator was excluded. Full source
results are retained locally in `dist/stability-source-tests.log`.

```sh
ISH_TEST_CSH=/absolute/path/to/bsd-csh \
  .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The additional probe is retained locally as `dist/stability_probe.py`, with its
results in `dist/stability-probes.json`. It uses temporary homes and PTYs, and
signals only its own processes. The observations were:

| Observation | Source | Onefile |
| --- | --- | --- |
| Shell reports the outer 24 x 100 size | Yes | Yes |
| Python worker reports columns=0, lines=0 | Yes | Yes |
| Terminal restored after SIGTERM | No | No |
| Session directories remaining after SIGTERM | 1 | 1 |

The earlier five-shell compiled smoke tests and the 60-second idle test are
recorded in the distribution report; this review does not rerun that entire
compiled matrix or rebuild the executable.

## Remaining validation work

- Exercise prolonged sessions on the target host with repeated commands, large
  output, continuation input, TUI transitions, workers, and concurrent instances.
  Track memory, open descriptors, child processes, response latency, and cache use.
- Confirm the later [termination fix](TERMINATION.md) and worker resizing on
  the target RHEL host, including its actual SSH client and terminal settings.
- Validate the intended plugins and their dependency installation in the offline
  environment. Plugin loading can synchronously invoke pip before the UI starts;
  `_load_library` has no overall subprocess timeout. This is a code-observed
  startup risk, not a reproduced network failure in this review.
- Keep a reproducible copy of the regression suite in a separate test repository
  or CI input if `tests/` remains excluded from Git. The current exclusion is
  intentional and has not been changed; a fresh checkout alone cannot reproduce
  all of the local regression checks.

The PTY tests are functional checks with a minimal cursor-query responder, not a
full terminal-screen emulator. Their success does not establish every visual
layout, arbitrary plugin compatibility, or multi-day resource stability.
