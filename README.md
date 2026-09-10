# ish

ish is a command-line UI that communicates with Linux shells through a PTY and
provides editing and completion through prompt-toolkit.
It supports Bash, zsh, BSD csh, tcsh, and dash. On Windows, run it inside WSL.

## Development environment

Use Python **3.12.14** and uv. Create `.venv` with Linux Python, including when
working in WSL; do not share it with Windows Python.
From the project directory (for example, `/mnt/d/Programs/ish` in WSL), run:

```sh
cd /mnt/d/Programs/ish
export PATH="$HOME/.local/bin:$PATH"
uv sync --locked
uv run ish tcsh
```

The PATH example assumes uv is installed in `~/.local/bin`. Adjust it if your
installation uses a different location.
uv selects the Python version in `.python-version` and installs the dependencies
recorded in `uv.lock`. The default sync also includes Ruff and psutil for development
and testing. `uv run` does not require manually activating the virtual environment.
Dependency bytecode is compiled during synchronization to reduce CLI startup work.
Keeping the virtual environment on a WSL mount such as `/mnt/d` can add several
seconds to the first launch because of filesystem access overhead.

The host needs the selected shell and GNU coreutils (`base64 -w0`, `env -0`).
Running from source also requires GCC; Nuitka distributions include a precompiled
helper. Integration scripts, the helper, and FIFOs are created under
`config.CACHE_DIR/session-...`. The cache filesystem must allow executable files.
The default cache location is `~/ish/.cache`.

## Linux distribution

Build with Python 3.12.14 on the oldest Linux/glibc release you intend to support:

```sh
uv sync --locked --group build
uv run --locked --group build python tools/build_nuitka.py --mode standalone
uv run --locked --group build python tools/build_nuitka.py --mode onefile
```

The builder prints the release directory under `dist/nuitka/`. Test the executable
in a real terminal, or run the automated PTY checks against it:

```sh
uv run --locked python tools/smoke_distribution.py /absolute/path/to/ish
```

Onefile extracts into a private directory under
`~/ish/.cache/nuitka/<build-id>/launch-...` instead of `/tmp`, and cleans up on exit.
Its files stay available for the full process lifetime. Shell sessions use separate
`config.CACHE_DIR/session-...` directories and remove only their own files on exit.
See [distribution details](docs/DISTRIBUTION.md) for cache lifetime, plugin dependency
installation, target compatibility, and the scope of automated verification.
The [local validation report](docs/DISTRIBUTION_VALIDATION.md) records the final
artifact, checksums, actual test results, and the RHEL 8 compatibility limitation.

## Stability and support scope

Normal interactive workflows have regression coverage across the five supported
shells, including continuation input, history, hook recovery, and return from Vim
and man. This supports controlled evaluation, but does not establish unattended
or multi-day reliability on every target host.

The [stability review](docs/STABILITY_REVIEW.md) records the remaining limitations
and subsequent fixes. Internal Python tools now inherit the shell PTY's dimensions
and receive live size changes. SIGTERM can still leave terminal settings and session
files unrestored. The existing WSL-built binary predates the worker-size fix and
requires a newer glibc than RHEL 8 provides. Rebuild and validate the actual deployment
environment before treating the release as production-ready.

## Code checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run python -B -m unittest discover -s tests -p 'test_*.py' -v
```

The local test suite is excluded from Git under the repository's current policy.
The test commands require a separately retained copy of `tests/`; a fresh checkout
after the tracking removal is committed will not contain it.

`tests/test.py` is a legacy generator that creates files when imported. It is
excluded from linting and test discovery. CLI PTY tests allow up to 30 seconds for
the first prompt to accommodate initial imports and helper compilation. Command
responses and returns from TUIs use the deadlines defined by each test.
Tests may skip shells that are not installed. If Ubuntu's `csh` is a symlink to
tcsh, set `ISH_TEST_CSH` to an actual BSD csh executable to test that implementation:

```sh
ISH_TEST_CSH=/usr/bin/bsd-csh uv run python -B -m unittest discover -s tests -p 'test_*.py' -v
```

## Architecture

- `src/ish/shell`: shell adapters, integration scripts, FIFO framing, PTY I/O,
  and prompt boundaries.
- `src/ish/ui`: the prompt-toolkit editor, ANSI prompt rendering, and completion.
- `src/ish/parser`: CLI options, shell output, command arguments, and completion context.
- `src/ish/app`: Python tools running in separate processes with PTY connections.
- `src/ish/plugin`: plugin registration, dependency checks, and loading.
- `src/ish/lang`: translations and default messages.

ish owns editing at the primary prompt. During command execution and continuation
prompts, input remains with the shell. The editor resumes after both the prompt
signals and the state FIFO have synchronized.
If both shell hooks and prompt markers are removed, run `ish_recover` after
confirming that the underlying shell is waiting for a command.
At the primary prompt, Ctrl+C cancels editing without changing the shell's exit
status. Native Readline and ZLE editing are intentionally disabled there.

## Prompt extension API

Use `set_tool`, `set_key`, and `set_float` in `.ishrc.py` to register, replace,
or remove extensions. These replace the former `add_*` and `delete_*` methods;
update existing rc files and plugins to use the new names.

```python
from prompt_toolkit.widgets import Label

prompt.set_tool("say", function=print)
prompt.set_tool("say", function=None)
prompt.set_tool("report", plugin_name="reports", function_name="run")


def insert_example(event):
    """Insert example text at the current cursor position."""
    event.current_buffer.insert_text("example")


prompt.set_key("c-x", handler=insert_example, eager=True)
prompt.set_key("c-x", handler=None)
prompt.set_key(insert_example)  # Remove all bindings using this handler.

panel = prompt.set_float(Label("Status"), top=0, right=0)
panel = prompt.set_float(Label("Updated"), target_float=panel, top=0, right=0)
prompt.set_float(target_float=panel)
prompt.set_float()  # Clear all custom floats.
```

Setting a tool replaces the callable for its command name. Tool functions run in
a spawned worker and must be pickleable; use an importable function such as `print`.
Setting a key replaces every binding for that exact sequence, including conditional
variants, and accepts the options supported by `KeyBindings.add`. A positional
handler removes all bindings using that function. Removing an absent entry is a no-op.

`set_float` returns a new `Float` handle. To replace a float, pass its current handle
as `target_float` and retain the returned handle. Replacement preserves list order
and uses default values for omitted `Float` options. Omitting content removes the
target, or clears all custom floats when the target is also omitted. Invalid tool
sources, key options, or float replacements raise errors before changing existing entries.

## Ruff policy

The project enables E/W/F/I/B/C4/UP with the type-annotation exceptions listed in
`pyproject.toml`. D100–D107 check for missing docstrings without enforcing the full
pydocstyle rule set. The E722 exception applies only to the optional imports in
`stdlib.py`; ordinary code avoids bare `except` clauses that also swallow
`KeyboardInterrupt` and `SystemExit`. No S101 exception is needed because the S
rule family is not enabled.

Ruff also checks locally available tests ignored by Git. The root-level `tests/`
and `dev/` directories remain excluded from version control.

Configuration references: [uv Python version selection](https://docs.astral.sh/uv/concepts/python-versions/),
[uv bytecode compilation](https://docs.astral.sh/uv/reference/settings/#compile-bytecode),
and [Ruff configuration](https://docs.astral.sh/ruff/configuration/).
