# Exit status and Linux syscall validation

Date: 2026-09-12.

## Change

The child wait result now travels through `InteractiveShell._shell()`,
`_run_session()`, `ShellSignalController.run()`, and `main()` to the synchronous
`run()` entry point. `_shell()` still drains the final PTY output before returning.
Resource cleanup finishes before the caller receives the result.

| Exit cause | Asynchronous `main()` result | CLI exit status |
| --- | --- | --- |
| Native `exit 0`, `exit 7`, `exit 255` | 0, 7, 255 respectively | 0, 7, 255 respectively |
| Native shell killed by signal N | -N, as returned by asyncio | 128 + N |
| Explicit editor command `ish_exit` | 0 | 0 |
| First catchable termination signal sent to ish | 128 + N | 128 + N |

A termination request received during cleanup continues to take precedence
over a natural child result. Caller cancellation and exceptions still propagate.
The HUP used to close a still-running shell after `ish_exit` does not turn that
successful editor exit into a failure.

The foreground-pipeline cleanup finding is explicitly deferred. Neither group
ownership validation nor TERM/HUP/KILL delivery in `stop_child()` was changed.
The production change only carries the result along the existing completion
path; it adds no input-time checks, background tasks, or process scans.

## Test scope

Shells and their child programs invoke the kernel directly; ish does not emulate
Linux syscalls. Its PTY, input ownership, terminal modes, and signal delivery can
nevertheless affect blocking calls and interactive programs. These checks cover
those boundaries and representative file/process/network workloads, not every
Linux syscall or every possible combination.

`tests/syscall_fixture.c` uses native C APIs rather than Python wrappers that
may automatically retry interrupted calls. It is compiled with GCC using
`-std=c11 -Wall -Wextra -Werror -O2` by `tests/test_syscalls_cli.py`.

| Case | Operations and assertions |
| --- | --- |
| File I/O | `openat`, `writev`, `readv`, `lseek`, `fsync`, `fstat`, `mmap`, `msync`, `munmap`, `pread`, `renameat`, `unlinkat`; exact contents and sizes |
| Processes and pipes | `pipe2`, `fork`, `dup2`, `exec`, `waitpid`; child output/EOF and exit 37; child stop/continue and exit 23 |
| FIFO | `mkfifo`, blocking `open`, `read`, `write`, `close`, `unlink`; delayed writer, exact data and EOF, child reaping |
| Sockets | Unix `socketpair`, `send`, `recv`, `shutdown`; loopback TCP `bind`, `listen`, `connect`, `accept4`, exact payload |
| Waiting for terminal input | `poll`, `select`, `epoll_wait`, `read`; no early editor return, each distinct input line consumed once |
| Interrupted waits | `sigaction`, `setitimer`, blocking `read`, `ppoll`, `nanosleep`; actual EINTR delivery without SA_RESTART, then successful I/O |
| Terminal control | `ioctl`, termios, `/dev/tty`, foreground process group, blocking stdin, 24x100 size; raw Ctrl+C/Ctrl+Z/NUL bytes preserved and terminal mode restored |

Each case runs under both the plain interactive shell and ish, then returns to
the prompt and executes a follow-up command exactly once. Five shells times
seven cases times two launch modes gives 70 comparisons. BSD csh uses the
existing explicit `ish_recover` path after each completed command inside ish;
the report records this condition. Its missing automatic hook is not hidden by
an idle recovery command.

`tests/test_exit_status.py` covers five actual CLI exit cases per shell:
`exit 0`, `exit 7`, `exit 255`, SIGUSR1 sent only to the inner shell, and
`ish_exit` after a failed command. It also checks controller propagation and
signal precedence. The existing final-output test now asserts `_shell()` returns
the observed exit code as well as retaining its output.

## Results

The new exit/status and syscall suite passed **7 test methods in 163.710 seconds**,
with no failures or skips. Those methods contain **25 real CLI exit scenarios**
(five per shell) and **70 native/ish syscall scenarios**, plus controller and
CLI-conversion unit checks. Every syscall case and its follow-up command passed.

| Shell | CLI exit scenarios | Plain-shell syscall cases | ish syscall cases |
| --- | --- | --- | --- |
| Bash | 5/5 | 7/7 | 7/7 |
| sh/dash | 5/5 | 7/7 | 7/7 |
| zsh 5.5.1 | 5/5 | 7/7 | 7/7 |
| tcsh 6.20.00 | 5/5 | 7/7 | 7/7 |
| BSD csh | 5/5 | 7/7 | 7/7, explicit reconnect |

Evidence: `dist/exit-syscall-tests.log` and `dist/syscall-comparison.json`.
The initial test development run contained fixture errors: an eight-byte TCP
payload was read with a sixteen-byte MSG_WAITALL request, editor exit was
incorrectly assumed to use Ctrl+D, and BSD csh lacked its explicit reconnect.
These were corrected in the tests before the successful comparison. They did
not require further production changes.

The selected existing regression suite passed **94 tests in 354.927 seconds**,
with no failures or skips. It covers TERM/HUP during startup and runtime,
terminal disconnection, Python workers, backpressure and partial writes,
native input waits, multiline submissions, bulk cancellation, hooks and shell
startup settings, tcsh foreach cancellation, prompt resynchronization, and
Vim/man return on all five shells. Evidence: `dist/exit-syscall-regressions.log`.

Together, **101 test methods passed** in this follow-up. This is the affected
regression subset plus new coverage, not a rerun of the entire earlier suite.
Ruff lint and format checks passed for all 68 Python files under `src` and
`tests`; GCC's warning/error check for the C fixture and `git diff --check` also
passed. No product failure was detected within this tested scope.

## Environment and reproduction

Validation uses Python 3.12.14 on WSL Linux 6.6.87.2, with Bash 5.3.9, sh/dash,
verified zsh 5.5.1, tcsh 6.20.00, and actual BSD csh. The compatibility binaries
are the same fixtures used by the earlier review. This is not execution on the
corporate RHEL 8.10 kernel or Bash 4.4.20, and no Nuitka rebuild is included.

From the repository root in WSL:

```sh
export PYTHONPATH=src:tests
export ISH_TEST_ZSH="$PWD/dist/zsh-compat/zsh-5.5.1-verified/Src/zsh"
export ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh"
export ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh"
export ISH_SYSCALL_REPORT="$PWD/dist/syscall-comparison.json"
.venv/bin/python -B -m unittest test_exit_status test_syscalls_cli -v

# Existing regressions affected by session completion or input ownership.
.venv/bin/python -B -m unittest \
  test_shutdown test_signal_policy test_session_guards test_streaming_cli \
  test_p2_shells test_tcsh_interrupt test_prompt_resync \
  test_p1.InitializationTests test_p1.WriterTests test_p1.WorkerTests -v
```

Tests and diagnostic output remain under the repository's existing `tests/`
and `dist/` Git ignores. The legacy `tests/test.py` generator is excluded.
