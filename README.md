# ish

ish is an interactive command editor for Linux shells. It adds multiline editing,
syntax highlighting, completion, paired-character input, and Python extensions
through prompt-toolkit while your selected shell executes commands.

Use ish on Linux or inside WSL on Windows. Native Windows and macOS execution are
not supported. Run `ish --help` for command-line options and `ish --diagnose tcsh`
for an environment report without starting an interactive session.

## Supported shells

The selected shell must be installed on the host; the distribution does not bundle
shell executables. Specify a shell name or its executable path. If omitted, ish
selects the login shell by name.

| Shell | Argument | Support notes |
| --- | --- | --- |
| Bash | `bash` | Uses ish editing at the primary prompt with native Readline disabled. |
| zsh | `zsh` | Uses ish editing at the primary prompt with native ZLE disabled. |
| tcsh | `tcsh` | Supports prompt hooks and return to the ish editor. |
| BSD csh | `csh` or `bsd-csh` | After a command, run `ish_recover` at the native prompt to resume ish editing. |
| dash / sh | `dash` or `sh` | The sh integration is exercised with dash. Input lines over 4,095 encoded bytes are rejected. |

A `csh` executable that resolves to tcsh uses tcsh integration. Other implementations
named `sh` are not automatically covered by dash support.

## Building with build.sh

Build on Linux or inside WSL. Install **uv**, **GCC**, and the usual C development
tools and headers first, and make `uv` available on `PATH`. The project selects
Python **3.12.14** through `.python-version`. uv prepares the project environment
and installs the locked application and build dependencies, including Nuitka.
Downloads require network access unless the required Python and packages are
already installed or cached.

From the repository root:

```sh
chmod +x tools/build.sh
./tools/build.sh
```

The default is a **standalone** build. Build options are forwarded to the Python
driver:

```sh
./tools/build.sh --help
./tools/build.sh --jobs 4
./tools/build.sh --output-dir "$HOME/ish-release"
```

The script also works when invoked by an absolute path from another directory.
Relative `--output-dir` values are resolved against the repository root. Existing
release directories are not overwritten.

By default, the result is written under
`dist/nuitka/<build-id>/standalone/`. The builder prints the actual path. Distribute
the **entire `ish.dist` directory**, then run its executable:

```sh
cd /path/to/release
./ish.dist/ish bash
./ish.dist/ish tcsh
./ish.dist/ish --home "$HOME/ish-profile" zsh
```

The standalone application includes Python and the compiled state helper; normal
use does not require host Python or GCC. Installing additional Python plugin
dependencies can require a compatible external Python with pip. The selected
shell and external commands must still be installed on the host.

Onefile remains available through `--mode onefile`, but is not recommended with
the pinned Nuitka 4.2.1: its bootstrap can bypass ish's TERM/HUP cleanup. Use
standalone for the current release. Build on the oldest Linux/glibc environment
you intend to support; a newer Ubuntu build is not automatically compatible with
RHEL 8.10.

## Customizing ish

Create `~/ish/.ishrc.py` for your settings. With `--home DIR`, use
`DIR/.ishrc.py` instead. This file receives `prompt`, `config`, `logger`, `option`,
and `plugin` objects. Enter `ish_reload` to reload the rc file in a running session;
restart ish after changing plugin source files.

`--no-rc` skips this startup file. `--no-plugins` skips automatic plugin loading.
Neither option skips the selected shell's own startup files. `--home` changes
ish's settings location without changing the shell's `HOME`.

### pre_hook, post_hook, and fallback_hook

Hooks run for shell submissions made through the ish editor. The normal order is
`pre_hook`, shell execution, `post_hook`, and then `fallback_hook` when a nonempty
submission reports a nonzero shell exit status. Post/fallback hooks run after
control returns from the shell; they are not shutdown callbacks. Internal Python
tools do not trigger these shell hooks.

Each hook is a synchronous function called **without arguments** in a separate
Python worker. Hooks do not receive `prompt`, command text, or exit status, and
their return values are ignored. Changing worker globals does not update the UI
or the parent shell. Keep hooks short, since ish waits for them to finish.

Define hooks and tools in an importable module. For example, create
`~/ish/plugin/script/my_tools/__init__.py` with:

```python
PLUGIN_META = {"name": "my_tools", "version": "1.0.0"}


def before_command():
    """Announce a shell submission."""
    print("Starting command...", flush=True)


def after_command():
    """Announce return from a shell submission."""
    print("Command finished.", flush=True)


def on_failure():
    """Report a nonzero shell exit status."""
    print("The command reported a failure.", flush=True)


def greet(name="world"):
    """Print a greeting from an internal Python tool."""
    print(f"Hello, {name}!", flush=True)
```

For a custom home, place the module under `DIR/plugin/script/my_tools/`.
Then connect the hooks in `.ishrc.py`:

```python
from my_tools import after_command, before_command, on_failure

prompt.pre_hook = before_command
prompt.post_hook = after_command
prompt.fallback_hook = on_failure

# Set any hook to None to disable it.
# prompt.pre_hook = None
```

Functions used as hooks or tools must be importable and pickleable. Avoid lambdas,
nested functions, or functions defined only in `.ishrc.py` for these workers.
Key handlers and completers can be defined directly in `.ishrc.py`.

### set_tool

Register a Python callable under an internal command name in `.ishrc.py`:

```python
from my_tools import greet

prompt.set_tool("greet", function=greet)
prompt.set_tool("say", function=print)

# Alternatively, resolve a function from an already loaded plugin.
prompt.set_tool("greet", plugin_name="my_tools", function_name="greet")

# Remove a registration by omitting the function source.
# prompt.set_tool("greet")
```

Enter `greet Alice` or `greet "Ada Lovelace"` at the ish prompt. Arguments are
passed as positional strings. A repeated registration replaces the existing
callable. Supply either `function` or the pair `plugin_name` and `function_name`.

Tools run in Python workers. Shell operators, substitutions, and redirections
such as `greet Alice | cat` are handed to the shell, which needs an actual external
command or shell function of that name.

### set_key

Key handlers run in the UI process and receive a prompt-toolkit key event:

```python
def insert_pwd(event):
    """Insert a command without submitting it."""
    event.current_buffer.insert_text("pwd")


prompt.set_key("f2", handler=insert_pwd)
prompt.set_key("c-x", "c-e", handler=insert_pwd)

# Remove one sequence or every custom binding using this handler.
# prompt.set_key("f2")
# prompt.set_key(insert_pwd)
```

Use key names or `prompt_toolkit.keys.Keys` values. Multiple positional keys form
a sequence. Setting a sequence replaces its existing ish bindings, including
conditional variants. Keyword options such as `filter` and `eager` are forwarded
to prompt-toolkit's `KeyBindings.add`. Keep handlers short to avoid blocking input.

### set_float

Add a floating UI element and keep the returned handle for replacement or removal:

```python
from prompt_toolkit.widgets import Label

panel = prompt.set_float(Label("Ready"), top=0, right=0)
panel = prompt.set_float(
    Label("Updated"), target_float=panel, top=0, right=0
)

# Remove the selected float, or clear all custom floats.
# prompt.set_float(target_float=panel)
# prompt.set_float()
```

`content` accepts a prompt-toolkit container or widget. Additional keyword
arguments configure the `Float`. Replacement preserves its position in the float
list but returns a new handle; omitted options use their defaults.

### set_completer

Pass an **iterable of completers**, even when adding only one:

```python
from prompt_toolkit.completion import WordCompleter

prompt.set_completer([
    WordCompleter(["greet", "say"], ignore_case=True)
])

# Remove additional completers while retaining ish's default completion.
# prompt.set_completer([])
```

ish merges these with its default completer, removes duplicate suggestions, and
runs completion in a worker thread. Each call replaces the previous additional
completers. A custom `Completer` implementation can also be supplied; its completion
code should not mutate UI state from the worker thread.

## Development environment

The current development environment is Windows with WSL 2 running Ubuntu 26.04
on x86-64 Linux 6.6.87.2. The project uses:

| Component | Version |
| --- | --- |
| Python | 3.12.14 |
| uv | 0.12.12 |
| prompt-toolkit | 3.0.53 |
| Pygments | 2.21.0 |
| Nuitka | 4.2.1 |
| GCC / glibc in the WSL build environment | 15 / 2.43 |

For source execution, use a Linux virtual environment, including inside WSL:

```sh
uv sync --locked
uv run ish bash
```

Do not share a virtual environment between Windows Python and Linux Python.
GCC is needed for the helper when running from source. The lockfile records the
application and build dependencies.

## Support scope

ish is intended for interactive Linux terminal use, including multiline commands,
input-waiting programs, typeahead, paste, Ctrl+C, Python tools, and return from
programs such as Vim and man. Linux kernel 4.13 or newer is required for the PTY
input handling used by ish.

- The primary prompt uses prompt-toolkit editing. Native Readline/ZLE bindings,
  native editor widgets, and every aspect of shell prompt rendering are not
  reproduced. At this prompt, Ctrl+C cancels editing without changing shell status.
- If integration hooks are replaced or removed, confirm the shell is waiting for
  a command and enter `ish_recover`. BSD csh requires this explicit return after
  commands as described above.
- Long input support depends on the shell and current input state. Bash, zsh,
  tcsh, and BSD csh can accept a long first line at an eligible primary prompt.
  A long later line in a pasted block is rejected, and dash/sh retains its
  4,095-byte limit. Custom zsh SIGINT handling can disable long input support.
- Session files are stored under `~/ish/.cache` by default. This location must be
  writable and permit executable files. Regular shutdown and handled TERM/HUP
  perform terminal and session cleanup; SIGKILL and OOM termination cannot do so.
  Cleanup of a pipeline whose leading process exits before its remaining
  processes is a known remaining limitation.
- Support is bounded by the host, shell version, startup configuration, and
  terminal. Indefinite unattended operation and compatibility with every Linux
  distribution are not guaranteed. RHEL 8.10 deployment requires a compatible
  build environment and confirmation on the actual host.
