# Linux distribution and runtime files

ish uses two separate lifetimes for its runtime files:

| Files | Location | Lifetime |
| --- | --- | --- |
| Shell scripts and `ish_forward` | `config.CACHE_DIR/session-<random>/integration/` | One shell session |
| `shell.fifo`, `tty.fifo` | `config.CACHE_DIR/session-<random>/` | One shell session |
| Onefile executable, libraries, bundled helper | `~/ish/.cache/nuitka/<build-id>/launch-.../` | One application instance |

The default `CACHE_DIR` is `~/ish/.cache`. The session path is resolved after the
user rc runs, so a `config.ISH_HOME` override in `.ishrc.py` is respected. Each
session directory has mode 0700 and each FIFO has mode 0600. A concurrent session
gets its own directory. Normal exit, initialization failures, and coroutine
cancellation remove only the owning session's files.

The `TemporaryDirectory` API still manages session cleanup, but it receives an
explicit cache parent. Neither its name nor the API implies placement in `/tmp`.
No global cache sweep runs at startup. A force-killed process or power loss can
leave an abandoned session directory; remove it only after verifying its session
is no longer running. Do not clear live session directories or extraction caches.

## Onefile choice

The latest [frozen-release validation](RELEASE_VALIDATION_20260912.md) found
that Nuitka 4.2.1 Onefile converts targeted SIGTERM to SIGINT, which can lead to
forced child termination, an unrestored terminal and a leftover session.
Targeted SIGHUP can leave the extraction directory behind. **Use standalone
mode for the current release.** The private extraction design below still
protects normal operation and concurrent launches, but does not solve these
bootstrap signal-handling failures.

The build script uses `--onefile-cache-mode=temporary` and a private path under
`{HOME}/ish/.cache/nuitka/<build-id>/launch-{PID}-{TIME_US}-{RANDOM}`. Here, temporary
is a cleanup policy: the directory is removed when the owning instance exits.
It does not select `/tmp`. The extracted code stays available for delayed imports
and spawned Python tools even if system temporary files are cleaned.

Separate launch directories prevent simultaneous cold starts from writing shared
executables or libraries. A completed instance cannot clean up a peer's code.
This trades extraction on each launch for isolation and bounded retained disk
usage, which fits long-running interactive sessions. Default release IDs contain
a timestamp and source hash; never reuse an ID for different binaries.

During validation of Nuitka 4.2.1, pausing a process while it wrote `ish.bin` into
a shared cached extraction directory caused another launch to fail with
`Text file busy`. The final build therefore uses independent extraction paths,
and the smoke driver repeats this pause scenario to check isolation.

The onefile bootstrap runs before Python and `.ishrc.py`. Its extraction path is
fixed at build time and follows the default cache layout; changing `ISH_HOME` in
the rc changes session paths but cannot relocate that already-running bootstrap.
To change extraction policy, edit the builder's `--onefile-tempdir-spec` or use
standalone mode with a stable installation directory.

Both the extraction location and the session location must allow executable
files. A `noexec` home/cache policy requires an allowed installation/cache path.
Onefile packages the interpreter and libraries but still needs the selected shell
and GNU coreutils on the host. It does not embed Bash, zsh, csh, tcsh, Vim, or man.

Nuitka documents extraction-path selection and recommends validating standalone mode
before onefile in its [use cases](https://nuitka.net/user-documentation/use-cases.html).

## Rebuilding

Use Linux or WSL, GCC, and Python 3.12.14:

```sh
uv sync --locked --group build
uv run --locked --group build python tools/build_nuitka.py --mode standalone
uv run --locked --group build python tools/build_nuitka.py --mode onefile
```

Nuitka, patchelf, and zstandard are pinned in the optional `build` dependency group.
The helper is compiled once during the build and included in the distribution;
compiled ish copies it into each session without invoking GCC. The builder keeps
intermediate C files and logs in `~/.cache/ish-build/` to avoid slow WSL mounted-drive
compilation. It copies the final artifact, compilation report, and build manifest
to `dist/nuitka/<build-id>/<mode>/`. It refuses to overwrite a release directory.

For onefile, deploy `ish` alone. For standalone, deploy the entire `ish.dist`
directory and launch `ish.dist/ish`; copying only that executable is insufficient.
Keep executable permissions when copying files, or run `chmod +x ish` after a
transfer through a filesystem that drops them. Build reports are for diagnostics
and are not required on the deployment host.

Build on the oldest target OS. The current WSL environment uses glibc 2.43, so
artifacts built there are not RHEL 8 compatibility builds. RHEL 8 deployment needs
a separate build and test on RHEL 8 or an equivalent glibc 2.28 environment, using
the same Python version and locked dependencies. Onefile does not remove glibc
requirements; see Nuitka's [Linux portability explanation](https://nuitka.net/doc/commercial/portable-linux-support.html).

## Python tools and plugins

The multiprocessing entry point calls `freeze_support()`. A compiled executable
is not a Python command-line interpreter, so plugin discovery no longer runs
`sys.executable -c ...` when compiled. Normal sessions and already-available Python
tools do not require a host Python installation.

Optional plugin dependency installation still uses pip in an external Python
matching the embedded interpreter's major/minor version (3.12). Set its path when
needed:

```sh
export ISH_PLUGIN_PYTHON=/path/to/python3.12
./ish tcsh
```

That interpreter must provide pip. An absent or mismatched interpreter causes a
dependency-installation error, rather than relaunching ish as if it were Python.
In an offline environment, supply dependencies from an approved local wheelhouse
or preinstall them into the configured plugin library directory. Binary extension
dependencies also need compatible Python ABI and system libraries.

Dynamic imports used only by an external plugin may need explicit inclusion at
build time, for example `--include-package=your_package` on the build script. Build
with the intended plugins and test their actual tools; a standalone executable
cannot promise support for every arbitrary future package. The existing optional
standard-library preload remains in place, except that the builder excludes
`ensurepip` and pip because dependency installation uses an external interpreter.
The default build includes shell lexers and all Pygments styles. Add `--all-lexers`
if an rc or plugin needs syntax highlighting for other languages; explicitly
including `pygments` or `pygments.lexers` also enables that larger bundle.

## Automated deployment checks

```sh
uv run --locked python tools/smoke_distribution.py /absolute/path/to/ish \
  --report dist/distribution-test.json
uv run --locked python tools/smoke_distribution.py /absolute/path/to/ish \
  --shell tcsh --idle-seconds 30
```

The driver relocates the binary away from the checkout, omits `PYTHONPATH`, and
uses isolated homes. It checks real shell commands, rc loading, a source plugin
function in a spawned worker, a built-in Python tool, concurrent sessions, custom
cache paths containing spaces, deletion of a private TMPDIR, normal-exit cleanup,
terminal restoration, independent extraction cleanup, restart, native input
waits, ordered typeahead, and return from Vim and man on every shell. Sentinel
executables detect attempted host Python or GCC use. The driver itself runs in
the development environment and uses psutil to clean up failed test processes.
For onefile it also pauses a cold extraction during the executable write and
requires another instance and its Python worker to start before the first resumes.

All five shells must be installed for the default run. If `csh` is a symlink to
tcsh, set `ISH_TEST_CSH=/path/to/bsd-csh` to test BSD csh itself. These are bounded
PTY tests with a minimal cursor-query responder, not a complete terminal emulator
or a multi-day soak test. They do not establish RHEL compatibility or validate
forced termination while a TUI is active. Use `ish_exit` or normal shell exit for
the tested cleanup path.
