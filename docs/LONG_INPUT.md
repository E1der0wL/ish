# Gated long-input handling

## Scope and admission

The adapter registry selects `LongInputMode`; new adapters default to `REJECT`.
This is a tested capability policy, not automatic certification of an arbitrary
shell executable or version at startup. There are no synthetic startup commands
or periodic health probes for this feature.

| Adapter | Oversized first line at an acknowledged primary prompt |
| --- | --- |
| Bash | Stage with temporary noncanonical input, then restore before LF |
| zsh | Stage only with the acknowledged ish SIGINT handler; custom traps reject long input |
| tcsh | Stage with temporary noncanonical input, then restore before LF |
| BSD csh | Stage with the same limits; native word-size restrictions remain |
| sh/dash | Reject in the editor; parser-recovery acceptance test failed |

The editor validates the **entire submitted block** before accepting Enter,
adding it to history, running execution hooks, or writing shell input. Length is
measured after encoding, per physical LF-delimited line: 4,095 payload bytes plus
the terminating newline fit the Linux canonical line buffer. Unicode characters
can occupy multiple bytes. A block containing many short lines remains allowed.

A verified adapter may stage an oversized **first physical line**, starting from
an acknowledged primary shell prompt. A later oversized line rejects the whole
block, including any short commands preceding it. The editor does not guess
whether later lines belong to a shell parser, a here-document, or a program's
stdin. Rejection displays `Not sent: ...` in the validation toolbar and leaves
the text and cursor available for editing. Caller-supplied validators remain
active. Cached asynchronous validation cannot bypass the transport check.

Literal calls to internal Python tools bypass shell transport limits. Compound
commands that must go through the shell still receive the usual admission check.
The engine revalidates before execution for embedded callers and hooks that
change terminal settings. If this later check rejects, it reports the reason and
reopens the submitted text; editor-history preservation applies to rejection
before the original editor accepts the command.

Native continuation prompts and program stdin retain their existing input
ownership. The prompt-toolkit guard does not intercept these raw input streams.
BSD csh still requires explicit `ish_recover` before returning to the ish editor.
Native shell restrictions, including BSD csh's word-size limit and the operating
system's argument-size limit, remain in effect.

## Transport and cancellation

For an eligible first line, the engine checks matching prompt/state generations,
the shell foreground process group, and compatible terminal settings. It rejects
an already noncanonical terminal, disabled signal handling, conflicting special
characters, and unverified input-transforming modes. ASCII control characters
other than tabs are not accepted in a staged prefix.

The sender opens a temporary slave descriptor, saves its exact attributes, and
disables `ICANON` and `ECHO`. It sends the first-line prefix through the existing
backpressure-aware writer while withholding LF. Once slave `poll` reports the
prefix consumed, the sender restores the saved attributes and then sends LF and
any remaining short lines. The native editor remains disabled. There is no
helper command added to history and no permanent additional descriptor.

Ctrl+C invalidates pending sends, restores attributes **before** delivering the
interrupt, and discards cancelled queued input through the existing cancellation
path. Cleanup also restores the attributes if SIGTERM, SIGHUP, or an exception
cancels the sender. Restoration is idempotent so a cancelled coroutine cannot
later overwrite settings chosen by the next native reader.

The readiness loop runs only during an oversized submission, yielding to input
and signal processing. A timeout of the final queue-readiness wait or an
unexpected prompt before LF aborts the session instead of committing uncertain
input. The ordinary short-input path
adds no terminal-state syscalls for admission and performs no idle polling.

An empty kernel queue is not a parser acknowledgement. This is why testing parser
errors and interruption is required independently for each adapter, and why the
policy remains conservative. Kernel and shell upgrades require revalidation.

## Zsh cancellation acknowledgement

With ZLE disabled, zsh's `shingetchar()` retries an interrupted read. Its caller
checks the interrupt flag only after the physical line is complete. If ish
cancels a staged prefix while withholding LF, that reader remains blocked until
another newline arrives. The behavior was reproduced in a native zsh PTY without
ish. See zsh's [input reader](https://github.com/zsh-users/zsh/blob/zsh-5.9/Src/input.c)
and [signal handling](https://github.com/zsh-users/zsh/blob/zsh-5.9/Src/signals.c).

The integration installs a `TRAPINT` function only when SIGINT has its default
behavior. It reports a session-scoped cancellation marker and returns 130 to
abort parsing. The engine releases **one LF only after this acknowledgement**,
checking the pending prompt generation and foreground owner. This completes the
cancelled line so zsh can discard it. An unrelated interrupt, old marker, or
duplicate acknowledgement cannot release a newline. If acknowledgement is absent
for five seconds, the session fails closed instead of committing uncertain input.

New keys stay in ish's typeahead buffer until a fresh prompt and its state frame
agree. Repeated Ctrl+C during this interval discards older typeahead and is
coalesced, so it cannot flush the cancellation LF. The normal command, native
stdin, and TUI paths do not receive this synthetic LF.

Zsh's nonzero `TRAPINT` return can also leave a return flag that skips the next
`precmd` function. A hook-array callback reruns the integration wrapper once if
that happened; user hooks retain their order and receive status 130. The normal
wrapper clears the flag so the callback cannot run hooks twice.

Startup function traps, string traps, and ignored SIGINT are preserved. A private
`zsh-traps` file captures string traps in the parent shell when sourcing the
integration, because command substitution resets those traps. This file is
overwritten on reconnect and removed with the session directory. Each prompt
then checks only the installed function body and reports readiness with its
generation. There is no new external process, idle command, or per-prompt file
access. A changed or removed handler disables long input at the next prompt.
To reenable after deliberately removing a custom trap, run `trap - INT` followed
by `ish_recover`. Ish never removes a custom trap automatically.

## Rejected candidate and earlier zsh result

- **dash/sh:** a syntax error in an early fragment can make a later fragment run
  as a separate command. See the original [experiment](LONG_INPUT_REVIEW.md).
- **Earlier zsh 5.9 candidate:** in a deterministic test with the shell reader stopped during a
  150 KB prefix, Ctrl+C restores the TTY but does not return the prompt after the
  reader resumes. A subsequent Enter is required. This repeated in two focused
  runs; no suffix executed. Evidence: `dist/zsh-cancel-before.log`. With the
  acknowledgement and hook correction, both repetitions return without Enter;
  see `dist/zsh-cancel-hook-fix.log` and the automated acceptance tests.

Dash remains rejected. Zsh now uses the guarded transport described above.

## Validation

The local environment is WSL2 kernel `6.6.87.2-microsoft-standard-WSL2`, Python
3.12.14, and prompt-toolkit 3.0.53. The matrix includes Bash 5.3.9, zsh 5.9,
tcsh 6.24.13, and BSD csh from package `csh_20240808-4_amd64.deb`. Dash 0.5.12
remains rejected. The full regression run selects tcsh 6.20 using
`ISH_TEST_TCSH`. A separate zsh 5.5.1 build uses the official release source,
with WSL's compiler and terminal library, without changing its input-engine
source. `dev/build_zsh_551.py` records the local build procedure.

Final zsh follow-up (2026-09-12): **261 tests passed in 1,236.687 seconds**, with
no skips (`dist/zsh-long-input-full-suite.log`). This includes 28 admission/CLI
test methods and their per-shell subcases. Ruff lint and format checks passed
for all 61 source/test files. The earlier 255-test gating baseline is retained
in `dist/long-input-full-suite.log`.

The separate zsh 5.5.1 follow-up passed **18 tests in 245.897 seconds**, with no
skips (`dist/zsh-551-acceptance.log`). To repeat that version's acceptance cases:

```sh
ISH_TEST_ZSH="$PWD/dist/zsh-compat/zsh-5.5.1/Src/zsh" \
PYTHONPATH=src:tests .venv/bin/python -B dev/zsh_compat_suite.py
```

An initial focused run had one Bash paste/typeahead deadline failure. Its cause
was not established. The full run's corresponding test subsequently passed, as
did **eight additional repetitions in 133.386 seconds**
(`dist/zsh-bash-typeahead-repeat.log`). These results do not establish the cause
of that initial timeout; the observation remains a limitation of this validation.

Admission tests cover encoded byte boundaries, Unicode, whole-block rejection,
custom TTY control characters, stale prompt generations, foreground ownership,
cached/user validation, retained cursor/history, and direct Python-tool dispatch.
The short-input test verifies that admission performs no extra TTY queries.

Real CLI tests cover exact 12 KB ASCII/UTF-8 transfer; canonical input and erase
behavior after a long line; loop headers and native continuations; syntax and BSD
csh word-limit errors; pasted stdin and subsequent commands; immediate typeahead;
Python-tool transitions; Vim/man return; Ctrl+C during a stopped 150 KB prefix;
and TERM/HUP cleanup during the same blocked transmission. Terminal attributes
are compared with their saved values, and cancelled suffixes must not execute.
The reader-stop barrier makes interruption testing deterministic.

Zsh-specific additions test an executable prefix already consumed by the reader
before cancellation, an unfinished quoted prefix, repeated Ctrl+C, post-cancel
typeahead exactly once, hook order and status 130, stale/duplicate markers,
changed foreground ownership, absent acknowledgements, startup/runtime custom
traps, and explicit restoration of default SIGINT handling. Native `read`, a
foreground sleep, and Python stdin cancellation must not receive a synthetic LF.

Diagnostics use atomic snapshots of actual editor/input-owner state. A previous
prompt still visible in the output is not accepted as proof that a later command
has finished. Shutdown checks observe attributes before PTY destruction and
continue checking process exit after PTY EOF.

Run the complete suite from the project root in WSL:

```sh
ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh" \
ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh" \
PYTHONPATH=src:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

The `test_*.py` pattern excludes the side-effectful legacy `tests/test.py`.
`tests/`, `dev/`, and `dist/` retain their existing Git exclusions.

The actual RHEL 8.10 kernel 4.18 host and its Bash 4.4.20 were not available.
Running tcsh 6.20 and an upstream zsh 5.5.1 build locally does not certify that
complete RHEL setup or its distribution patches and site configuration.
Re-run the acceptance matrix there before relying on this capability in that
environment. These bounded tests do not certify arbitrary shell modifications,
all parsing options, or unlimited input length and memory use.
