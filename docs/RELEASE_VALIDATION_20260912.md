# Latest frozen release validation: 2026-09-12

## Recommended artifact and results

The recommended release is the **standalone** archive
`dist/ish-1.0.0-20260912-linux-x86_64.tar.gz` (22.43 MiB).
It contains the complete `ish.dist` directory (64.35 MiB, seven files),
`build.json`, and `FILES.sha256`. Deploy the entire directory.

```sh
tar -xzf ish-1.0.0-20260912-linux-x86_64.tar.gz
sha256sum -c FILES.sha256
./ish.dist/ish tcsh
```

Inside this checkout, `./dist/ish tcsh` launches the same tested bundle with
`exec`; it leaves no wrapper process running. That convenience launcher is not
a self-contained distribution file. The old Onefile remains in its versioned
directory, and the failed new Onefile candidate is retained for diagnosis.

Standalone build ID: `1.0.0-20260912T124954188207Z-7278cf26`.

| Artifact | SHA-256 |
| --- | --- |
| Standalone `ish.dist/ish` | `b2db43318589793783f40a51aa0724bf8e465e9f75f9e42aa945b922bed7142f` |
| Exported `.tar.gz` | `2e07e28846bb3fca852622fece819dce7c11aa682bc148102bf7228b86e9ccc5` |

The archive also has an adjacent `.sha256` file. Final validation completed with:

| Check | Result |
| --- | --- |
| Standalone functional smoke matrix, five shells | All passed; input waits, ordered typeahead, workers, Vim/man, concurrent sessions, restart and cleanup |
| Empty HOME, no shell/ish rc or preexisting cache | 5/5 passed |
| Ordinary/native-signal exit and TERM/HUP at prompt/read, plus tcsh TUI shutdown | 42/42 passed |
| Native C API cases through the frozen program | 35/35 passed |
| Extended standalone suite total | 82/82 scenarios passed |
| Archive unpacked into another fresh location | All seven payload hashes matched; tcsh functional smoke passed |
| `dist/ish` convenience launcher | Exit 7, SIGTERM 143 and SIGHUP 129 passed, with terminal/session cleanup |

Standalone startup observations were 0.375-0.454 seconds in this smoke run;
these are not controlled benchmark results. Each shell case included two
concurrent sessions, five seconds idle after deleting its private TMPDIR, and
a later restart. No forbidden host Python/GCC sentinel was invoked.

Evidence: `dist/standalone-latest-smoke.json`,
`dist/standalone-latest-extended.json`, and
`dist/standalone-archive-validation.json`, with adjacent `.log` files.
No shell-engine source changes were needed during packaging validation.

## Onefile finding

The latest source was compiled with Python 3.12.14, Nuitka 4.2.1 and GCC 15
on WSL Linux 6.6.87.2, glibc 2.43. The source/build-input SHA-256 is
`7278cf26fdf8981dff86c0a823225591e04b6a1015e20613f99ce893aeb62d04`.
Both compilation and the five-shell functional Onefile smoke test succeeded.
The more complete shutdown checks found a packaging-specific failure.

The Onefile candidate is retained for diagnosis at
`dist/nuitka/1.0.0-20260912T123627175636Z-7278cf26/onefile/ish`.
Its SHA-256 is
`972b37bfffb07b68d8056480186a8327f6fbe9518df44e54e67f01f16bbf308f`.
It is **not the recommended release artifact**.

### Targeted signal comparison

Each case used a relocated binary and a private HOME/cache. Signals were sent
to a known owned PID, either the public Onefile bootstrap PID or the actual ish
application PID recorded by its isolated rc.

| Target | Signal | Observed status | Terminal restored | Session directories remaining | Extractions remaining |
| --- | --- | --- | --- | --- | --- |
| Onefile bootstrap | SIGTERM | 0, after 5.040 s | No | 1 | 0 |
| Onefile bootstrap | SIGHUP | Killed by SIGHUP | Yes | 0 | 1 |
| Inner application | SIGTERM | 143, after 0.145 s | Yes | 0 | 0 |
| Inner application | SIGHUP | 129, after 0.140 s | Yes | 0 | 0 |

The raw subprocess status for bootstrap SIGHUP was -1, which a calling shell
normally reports as 129. The concrete defect in that case is the extraction
directory left behind. No live captured descendants remained in these probes.
The diagnostic removed only its own isolated fixtures after recording evidence.

The installed Nuitka 4.2.1 `build/static_src/OnefileBootstrap.c` explains the
difference. `ourConsoleCtrlHandler()` calls `cleanupChildProcess()`, which sends
SIGINT to the application even for a targeted SIGTERM. Its default five-second
grace period can then escalate to SIGKILL. The bootstrap does not register a
SIGHUP handler to clean its extraction directory. Increasing the grace period
does not preserve the original signal identity or add that missing cleanup.

This bypasses the ish TERM/HUP policy tested from source. The application-level
control cases pass, so the shell engine was not changed to work around the
bootstrap. The deferred leaderless-pipeline finding is also unchanged.

Evidence: `dist/onefile-bootstrap-shutdown.json`,
`dist/onefile-bootstrap-shutdown.log`, `dist/onefile-latest-smoke.json`,
and the deliberately incomplete `dist/onefile-latest-extended.json`.

## Validation method

`tools/smoke_distribution.py` relocates the artifact outside the checkout,
omits `PYTHONPATH`, sets isolated HOME/TMPDIR paths, and places sentinel commands
in PATH to detect runtime use of host Python or GCC. Its controlled rc exercises
a source plugin, multiprocessing and a custom cache directory containing spaces.
The enhanced driver checks waiting shell reads, ordered typeahead and Vim/man
return on all five shells, plus concurrent sessions, restart and cleanup.
BSD csh retains its documented explicit `ish_recover` condition.

`dev/validate_frozen_release.py` adds five truly empty-HOME first launches,
ordinary/signal-derived exit status, TERM/HUP at the prompt and during a native
read, tcsh TUI shutdown, and the seven native C API workloads from
`tests/syscall_fixture.c`. The test controller uses the development Python/GCC;
the deployed ish process does not. This is home/config/cache isolation on the
same WSL installation, not a new OS image or container.

The selected shells are Bash 5.3.9, sh/dash, verified upstream zsh 5.5.1,
tcsh 6.20.00 and actual BSD csh. These checks are bounded functional runs,
not a multi-day soak test or full terminal-screen emulation.

Both the standalone ELF and Onefile ELF require GLIBC_2.38. This WSL build is not a RHEL 8.10 binary;
RHEL distribution still requires building and validating on a compatible target.

## Repeat the compiled checks

```sh
export PYTHONPATH=src:tools
export ISH_TEST_ZSH="$PWD/dist/zsh-compat/zsh-5.5.1-verified/Src/zsh"
export ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh"
export ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh"
binary="$PWD/dist/nuitka/1.0.0-20260912T124954188207Z-7278cf26/standalone/ish.dist/ish"
.venv/bin/python tools/smoke_distribution.py "$binary" --idle-seconds 5
.venv/bin/python dev/validate_frozen_release.py "$binary" --report dist/recheck.json
```

The test controller uses the project development environment. Every target
process receives its own explicitly constructed environment without PYTHONPATH.
The existing Git ignores for `dev/`, `tests/`, and `dist/` remain unchanged.
