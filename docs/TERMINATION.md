# Catchable termination

`SIGTERM` and `SIGHUP` now request one orderly shutdown of the shell session.
Handlers are installed before the session directory, helper build, PTY, and
FIFOs are acquired. The first signal determines the exit code: 143 for TERM and
129 for HUP. Later TERM/HUP signals do not cancel cleanup again. Previous Python
signal handlers are restored after the session resources have been released.

The signal callback records the request, invalidates pending input transmission,
and cancels the session once. The existing `_stop()` and `ExitStack` paths do the
actual asynchronous cleanup. A first signal arriving during an already-running
normal cleanup does not interrupt that cleanup either. Caller cancellation still
propagates as `CancelledError` when no termination signal was received.

## Resources and terminal state

- Stop the native input reader and cancel the editor/session tasks.
- Await Python-worker cleanup and reap the worker; its existing two-second
  termination grace period and kill fallback remain in effect.
- Terminate a verified foreground process group belonging to the shell's own
  session, hang up the shell, and allow a two-second grace period before the
  kill fallback. Reap the shell. A job ignoring HUP must not survive merely
  because the shell exited first. Group checks run only during shutdown.
- Allow at most 0.5 seconds for final terminal output during signal shutdown.
  A blocked output consumer can therefore lose the remaining output tail.
- Close FD writers, restore the original outer terminal attributes and descriptor
  flags, close owned descriptors, and remove only this session's cache directory.
- Restore observed common terminal modes if an interrupted native or Python TUI
  did not do so: alternate screen, cursor visibility, application keys, mouse and
  focus reporting, bracketed paste, and text attributes. This does not issue a
  full terminal reset or erase the screen.

Mode observation uses the existing shell control parser. Python tool output has
a separate bounded parser that skips plain chunks without escape characters.
Restoration assumes the ordinary caller-prompt defaults for observed modes; it
does not query or emulate arbitrary terminal state. Output restoration is best
effort if the terminal has already disconnected or cannot accept output. Such an
output error must not prevent descriptor and session-directory cleanup.
On an actual terminal disconnection, failure to restore termios on the vanished
device is tolerated. The CLI flushes its standard streams before exiting; a
broken stream is redirected to `/dev/null` at that final step so Python's final
flush cannot replace the signal-derived exit code with 120. Embedded `main()`
does not redirect caller streams.

Source builds now await the C compiler asynchronously. The compiler owns a
separate process group. If startup is cancelled, that group is terminated and
reaped before its files are removed, with a one-second grace period before the
kill fallback. Cancellation during compiler or shell process creation retains
ownership until subprocess setup finishes. Frozen builds continue to copy their
bundled helper and do not require a runtime compiler.

## Scope

The handlers cover `InteractiveShell.main()` from the start of session resource
acquisition. CLI imports, plugin installation, and user Python startup code run
before that entry point. Arbitrary blocking user code and uninterruptible kernel
I/O cannot be given an absolute shutdown-time guarantee by an event-loop handler.

No supervisor or idle polling is added. SIGKILL, OOM kills, and power loss remain
outside in-process cleanup. Detached jobs intentionally surviving their shell
are not recursively hunted down. Long-line transport, history retention, and
completion scheduling are unchanged by this work.

## Validation

The termination regressions in `tests/test_shutdown.py` use isolated homes and
real PTYs. They compare terminal attributes and FD flags before/after termination,
check the signal-derived exit status, verify removal of the owning session while
preserving a peer directory, and check that no captured non-zombie descendants
remain running. Cases cover startup and compiler cancellation, the primary and secondary
prompts, native reads, Vim, man, Python workers, repeated signals, and blocked
terminal output. Unit tests cover handler restoration and fragmented mode controls.

Validation on WSL with Python 3.12.14:

- All **40 real termination scenarios** passed, including the actual BSD csh
  executable and tcsh 6.20.00. The suite also covers six focused unit tests for
  signal coalescing, caller cancellation, writer cleanup, and terminal controls.
- The full suite ran **233 tests in 777.770 seconds**: 232 passed; one existing
  zsh CLI test reached its eight-second startup deadline before producing any
  output. That same test passed separately in 5.673 seconds without changing its
  deadline. Full log: `dist/shutdown-full-suite.log`; isolated log:
  `dist/shutdown-zsh-recheck.log`.
- The five-shell CLI set plus two additional zsh runs then passed **all seven
  tests in 33.772 seconds**, retaining the original startup deadlines. Log:
  `dist/shutdown-cli-recheck.log`.
- Ruff lint and formatting passed for all **58 Python files**; `git diff --check`
  passed.
- A separate closed-stdout CLI probe returned 143 without Python replacing the
  status with 120. Actual controlling-terminal disconnection is included among
  the 40 scenarios above.

The startup timeout is retained as a validation limitation, not silently treated
as a clean full-suite pass. These results exercise the old tcsh binary on WSL,
not the corporate RHEL kernel, Bash 4.4.20, or zsh 5.5.1.

To repeat the full suite with the compatibility binaries in this workspace:

```sh
ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh" \
ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh" \
PYTHONPATH=src:tests .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
```

The ignored legacy `tests/test.py` generator is excluded. This source change does
not rebuild previously produced Nuitka executables.
