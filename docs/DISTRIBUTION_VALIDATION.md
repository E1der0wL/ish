# Distribution validation: 2026-09-11

This is a historical validation record. The [2026-09-12 release
validation](RELEASE_VALIDATION_20260912.md) found that Nuitka 4.2.1 Onefile
bootstrap signal handling bypasses orderly TERM/HUP cleanup. Follow that newer
report for the current artifact and packaging decision.

The final Linux onefile executable is `dist/ish` (16.87 MiB). It is a copy of
`dist/nuitka/1.0.0-20260911-cache04/onefile/ish` with the same SHA-256:

```text
4afb3b24179664c578182f0dfa5bcea44986d4e5e04771d5c0a69f4915e5e13a
```

Run it directly from WSL without activating a Python environment:

```sh
cd /mnt/d/Programs/ish
./dist/ish tcsh
```

The valid standalone alternative is
`dist/nuitka/1.0.0-20260911-cache02/standalone/ish.dist/ish`. Deploy its entire
`ish.dist` directory (63.64 MiB), rather than only the executable.

## Environment and compatibility

Validation used Ubuntu 26.04 LTS under WSL2, Linux 6.6.87.2, glibc 2.43, GCC 15,
Python 3.12.14, Nuitka 4.2.1, patchelf 0.19.1.0, and zstandard 0.25.0.
The source suite used an extracted BSD csh package, not Ubuntu's `csh` symlink to
tcsh. Bash, zsh, dash, BSD csh, and tcsh all ran as real subprocesses.

**These binaries cannot run directly on RHEL 8.** ELF version inspection found a
GLIBC_2.38 requirement in the executable and the onefile bootstrap, and a
GLIBC_2.34 requirement in `ish_forward`. Rebuild and test with the same locked
Python/dependency versions on RHEL 8 or an equivalent glibc 2.28 build environment.
No RHEL execution was performed in this workspace.

## Results

| Check | Result |
| --- | --- |
| Full source regression suite | 163 tests passed in 278.889 seconds |
| Cache location, FIFO permissions, failure cleanup, compiled dependency choices | Four tests passed again after finalizing helper placement |
| Ruff lint and format | Passed |
| Standalone: Bash, zsh, dash, BSD csh, tcsh | All passed |
| Onefile: Bash, zsh, dash, BSD csh, tcsh | All passed |
| Compiled source-plugin function and built-in Python tools in spawned workers | Passed; worker executable paths belong to the distribution |
| Concurrent sessions, custom cache path containing spaces, private TMPDIR deletion | Passed |
| tcsh return from Vim and man, followed by immediate commands | Passed |
| Normal exit: FIFO/session cleanup, extraction cleanup, terminal restoration | Passed |
| Onefile: pause one executable extraction while another instance and worker start | Passed |
| Onefile: two tcsh sessions idle for 60 seconds after private TMPDIR deletion | Passed, including subsequent commands, workers, TUI return, and cleanup |
| Direct invocation of the final `dist/ish --help` | Passed |

The relocated onefile's initial prompt took approximately 0.57–1.27 seconds in
the five-shell run. These timings are observations, not a controlled performance
benchmark. Each shell scenario launched two concurrent instances, exited the
first while the second continued working, then checked a fresh restart.

The driver provided isolated homes, omitted `PYTHONPATH`, and installed PATH
sentinels for host Python and GCC. No sentinel was invoked. It inspected actual
FIFO types/permissions, the embedded helper's location, and worker executable
paths, and observed zero active extraction directories after normal exit.

## Why the final onefile uses private extraction

An earlier cached-extraction prototype shared one release directory. Pausing its
`ish.bin` write caused another launch to fail with `Text file busy`. That prototype
was superseded. The final build uses:

```text
{HOME}/ish/.cache/nuitka/<build-id>/launch-{PID}-{TIME_US}-{RANDOM}
```

Nuitka's `temporary` cache mode cleans only that launch's directory on exit. The
location remains outside `/tmp`, so system temporary-file cleanup does not affect
an active instance. The same paused-write scenario succeeded with the final build.

## Reproduction and limits

See [distribution instructions](DISTRIBUTION.md) for rebuilding and plugin setup.
The automated driver is `tools/smoke_distribution.py`. Raw test results are in
`dist/source-test.log`, `dist/standalone-bash.json`, `dist/standalone-shells.json`,
`dist/onefile-tests.json`, and `dist/onefile-idle-tests.json`. Versioned release
directories contain Nuitka compilation reports and build manifests.

The cache-specific unit tests are in `tests/test_runtime_cache.py`. The existing
Git ignore rule covers new files under `tests/`; explicitly include this test
file when committing. The legacy `tests/test.py` generator was not executed.

These are bounded functional tests and a 60-second idle check, not a multi-day
soak test or full terminal-screen emulation. They do not establish RHEL 8
compatibility, universal plugin compatibility, or cleanup after a force kill or
power loss. Already-running instances must retain access to their cache paths;
external cache deletion and `noexec` mount policies remain deployment constraints.
