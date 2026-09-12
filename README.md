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

The host needs the selected shell. The bundled helper encodes state and collects
the exported environment without depending on the shell's current `PATH`.
C shell integration also uses the system `printf` resolved at startup.
Interrupting queued submissions uses Linux's `TIOCGPTPEER` ioctl (Linux 4.13+).
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
and receive live size changes. Catchable termination now restores terminal settings
and releases session files; see [termination behavior](docs/TERMINATION.md).
The existing WSL-built binary predates these source fixes and
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
signals and the state FIFO have synchronized on a fresh generation. Reprinting
`PS1` cannot authorize a recovery command or transfer input to the editor.
If reporting hooks are removed, input remains with the shell even when its
prompt markers survive. Run `ish_recover` after confirming that the underlying
shell is waiting for a command. No recovery commands are sent automatically.
Dash reports state during PS1 expansion. BSD csh has no equivalent prompt hook:
after a command it keeps native input until the user explicitly runs
`ish_recover` to resume the ish editor. This limitation does not apply to a
`csh` executable that resolves to tcsh.
At the primary prompt, Ctrl+C cancels editing without changing the shell's exit
status. Native Readline and ZLE editing are intentionally disabled there.

Oversized input is gated by shell capability and input context. Bash, zsh, tcsh, and
BSD csh may use a temporary TTY mode for a long first physical line at a confirmed
primary prompt. The saved settings are restored before its newline is sent.
Zsh additionally requires ish's verified SIGINT handler; existing custom or
ignored SIGINT traps are preserved and disable long input. Dash/sh rejects lines
over 4,095 encoded bytes; every shell rejects an
oversized later line in a pasted block. Rejection keeps the entire block in the
editor and displays a warning before running any part of it. Internal Python
tools do not use this shell transport. See [long-input handling](docs/LONG_INPUT.md)
for validation conditions, test results, and native-shell limits.

With Bash's default `promptvars` option enabled, secondary prompts for already
available input are omitted from command output. Incomplete blocks still show a
secondary prompt when more input is needed. The readiness check does not consume
input and runs only when Bash expands PS2. If `promptvars` is disabled, ish preserves
literal PS2 behavior, including its secondary prompt display.

During a large submission, Ctrl+C cancels unsent bytes and clears queued PTY
input before forwarding the interrupt. A program that disables terminal signal
processing continues to receive Ctrl+C as input, according to its terminal mode.
See [input-boundary validation](docs/INPUT_BOUNDARIES.md) for the behavior changes,
maintenance-cost measurements, and remaining limits.

Input observation is available through `prompt.input_observer`. It is disabled
by default and keeps a bounded metadata trace in memory when enabled. It does
not block consumers or change input queues. See
[input observation results](docs/INPUT_OBSERVATION.md) for setup, real-PTY
comparisons, and the input-boundary limitations found during those checks.
Those four input-transfer defects have since been addressed; see
[input transfer fixes](docs/INPUT_TRANSFER.md) for the current behavior and validation.

Once a fresh shell state frame announces prompt return, newer keyboard input is
held for the editor behind older forwarded input. Python tools receive already-read
editor typeahead, and unread terminal input is returned when a tool exits. A tool
that has already consumed bytes into its own memory cannot return them this way.
BSD csh receives pending user keystrokes even while it remains in native input;
its explicit `ish_recover` requirement for resuming the editor still applies.

Submission generations and cached process-exit checks guard against stale sends
and callbacks. Optional terminal snapshots observe mode, foreground group, and
size at transitions without changing them. See [session guards](docs/SESSION_GUARDS.md)
for cross-shell regressions, measured costs, and the cancellation policy deliberately
excluded because it interfered with programs that handle SIGINT and keep reading.

SIGTERM and SIGHUP request one awaited shutdown from shell-session initialization
onward, restoring terminal settings and removing the owning session directory.
Repeated signals do not interrupt cleanup. See [termination behavior](docs/TERMINATION.md)
for startup handling, TUI mode restoration, output deadlines, and limitations.

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
