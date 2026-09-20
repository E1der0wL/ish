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

| Shell | Minimum supported version | Argument | Support notes |
| --- | --- | --- | --- |
| Bash | 5.3.9 | `bash` | Uses ish editing with native Readline disabled. PS2 uses current-shell command substitution to avoid a subshell per continuation line. |
| zsh | 5.5.1 | `zsh` | Uses ish editing at the primary prompt with native ZLE disabled. Hiding queued continuation prompts requires `PROMPT_SUBST` and `zsh/zselect`. |
| tcsh | 6.21.00 | `tcsh` | Supports prompt hooks and return to the ish editor. |
| BSD csh | Not specified | `csh` or `bsd-csh` | After a command, run `ish_recover` at the native prompt to resume ish editing. |
| dash / sh | Not specified | `dash` or `sh` | The sh integration is exercised with dash. Input lines over 4,095 encoded bytes are rejected. |

A `csh` executable that resolves to tcsh uses tcsh integration. Other implementations
named `sh` are not automatically covered by dash support.

To hide repeated zsh continuation prompts when pasting multiline commands,
**`PROMPT_SUBST` must be enabled and the `zsh/zselect` module must be available**.
In zsh, enable the option and check that the module can be loaded with:

```zsh
setopt PROMPT_SUBST
zmodload zsh/zselect
```

Keep `setopt PROMPT_SUBST` in your `.zshrc` to enable it in future sessions.
ish loads the module automatically when available, but does not enable
`PROMPT_SUBST` itself. If either condition is unmet, commands still run with
native continuation prompts, which can repeat during multiline paste.

These versions define the supported range. ish does not automatically query,
compare, or enforce shell versions; select a supported version yourself. The same
version guidance is available through `ish --help`. Older versions may fail to
initialize or process commands incorrectly, including older tcsh releases affected
by `postcmd` loop-handling defects.

On hosts with older system shells, install a supported shell in a separate
directory and select it explicitly, for example
`ish "$HOME/opt/bash-5.3.9/bin/bash"`. The host's system shell can remain in place.

## Building with build.sh

Build on **x86_64 Linux** or inside WSL. Install **uv**, **GCC**,
and the usual C development headers first. Make `uv` available
on `PATH`. The project selects Python **3.12.14** through `.python-version`.
uv prepares the build environment using `uv.lock`.

The distribution contains a dedicated, relocatable **CPython 3.12.14** runtime,
its Linux standard library and extension modules, ish, pip, the locked application
dependencies, the precompiled `ish_forward` helper, and the tcsh input library
`ish_shell.so`. Python code is loaded
normally; the build does not freeze imports or require a list of plugin modules.
The runtime archive and SHA256 are pinned in `tools/python-runtime.toml`.

From the repository root:

```sh
chmod +x tools/build.sh
./tools/build.sh
```

Build options are forwarded to the packaging driver:

```sh
./tools/build.sh --help
./tools/build.sh --output-dir "$HOME/ish-release"
./tools/build.sh --build-id local-check \
    --runtime-archive /path/to/pinned-install_only_stripped.tar.gz \
    --licenses-archive /path/to/pinned-full.tar.zst
```

The script also works when invoked by an absolute path from another directory.
Relative `--output-dir` values are resolved against the repository root. Existing
release directories are not overwritten.

The output must be on a case-sensitive Linux filesystem. If the checkout is on a
Windows drive mounted under `/mnt/c` or `/mnt/d`, use
`./tools/build.sh --output-dir "$HOME/ish-release"`. Transfer the result as a
`tar.gz` archive; copying the unpacked Python tree to a case-insensitive filesystem
can collide with case-sensitive runtime data.

By default, the result is written under
`dist/cpython/<build-id>/`. The builder prints the actual path. Distribute
the **entire output directory**, keeping `bin/` and the relative `ish -> ./bin/ish`
launcher link together:

```text
<release>/
├── ish -> ./bin/ish
├── README.md
├── build.json
└── bin/
    ├── ish
    ├── libexec/
    │   ├── ish_forward
    │   └── ish_shell.so
    └── python/
```

Run the launcher from the release directory:

```sh
cd /path/to/release
./ish bash
./ish tcsh
./ish --home "$HOME/ish-profile" zsh
```

Normal use requires no host Python, uv, GCC, or network access. The selected shell
and external commands must still be installed on the host. The launcher preserves
the caller's working directory and shell environment, and uses Python's isolated
startup mode so unrelated `PYTHONHOME`, `PYTHONPATH`, or user-site packages cannot
replace the packaged application. Plugins and their library directory are added
explicitly by ish, as before.

The installation can be moved as a unit and may be read-only. Settings, plugins,
logs, and session files stay under the configured ish home. There is no onefile
extraction or background runtime download; keep the installation available while
sessions are running. Upgrade by unpacking a new directory and restarting ish.

The bundled interpreter is also usable directly:

```sh
./bin/python/bin/python3 my_script.py
./bin/python/bin/python3 -m pip --version
```

Plugin dependencies use that same interpreter's pip and install into
`config.PLUGIN_LIB_DIR`, without modifying the installation. On an offline host,
provide compatible wheels through pip's `PIP_NO_INDEX=1` and `PIP_FIND_LINKS`
settings. Packages compiled from source still need their own build tools and
system dependencies. Standard-library GUI features require a display; OS-specific
modules such as `winreg` are unavailable on Linux. This upstream runtime excludes
the optional `dbm.gnu` (`_gdbm`) and `nis` extensions; `dbm.ndbm` is available.
Deprecated `tkinter.tix` needs Tix, which is also not bundled. See the upstream
[technical notes](https://gregoryszorc.com/docs/python-build-standalone/main/technotes.html)
for those build choices. The distribution does not claim to supply every optional
standard-library backend.

Build downloads require network access unless Python, the locked packages, and
the pinned runtime and license archives are already available. `--runtime-archive`
and `--licenses-archive` accept offline copies and still verify their SHA256. The runtime's
native dependency licenses are preserved under `bin/python/licenses/`;
Python-package licenses remain in their installed metadata.

Build on the oldest Linux/glibc environment you intend to support. A newer Ubuntu
build is not automatically compatible with RHEL 8.10, even with portable Python:
the native helpers also link to glibc. The release workflow builds against
glibc 2.28 and checks every bundled ELF file before publishing a candidate archive.

## Customizing ish

Edit `~/ish/.ishrc.py` for your settings. With `--home DIR`, use
`DIR/.ishrc.py` instead. Interactive startup creates an empty file if it is missing
and preserves existing files. This file receives `prompt`, `config`, `logger`, `option`,
and `plugin` objects. Enter `ish_reload` to reload the rc file in a running session;
restart ish after changing plugin source files.

`--no-rc` skips creating and loading this startup file. `--no-plugins` skips automatic plugin loading.
Neither option skips the selected shell's own startup files. `--home` changes
ish's settings location without changing the shell's `HOME`.

The following objects are available directly in `.ishrc.py`, without importing them:

| Object | Purpose |
| --- | --- |
| `prompt` | The active ish `Prompt` instance. Configure hooks, tools, key bindings, completion, toolbar, right prompt, and styles. |
| `config` | The current configuration and resolved paths, such as `config.ISH_HOME`, `config.RC_FILE`, and `config.CACHE_DIR`. |
| `logger` | The session logger. Use methods such as `logger.info(...)` and `logger.warning(...)` for configuration diagnostics. |
| `option` | Parsed command-line options, including `option.shell`, `option.lang`, `option.home`, and `option.no_plugins`. |
| `plugin` | The plugin manager. `plugin.get("my_tools")` retrieves an already loaded plugin module, or returns `None` if it is unavailable. |

### Plugin metadata: PLUGIN_META

Place each plugin in its own directory under `~/ish/plugin/script/`. For example,
`my_tools` can use `my_tools/__init__.py` or `my_tools/my_tools.py` as its entry
point. If both exist, `my_tools.py` takes precedence. With `--home DIR`, the base
directory is `DIR/plugin/script/`.

Define `PLUGIN_META` as a top-level dictionary literal in the entry point.
Metadata is read before importing the plugin, so use literal strings and lists;
do not construct the dictionary with function calls, variables, or imports.

| Key | Definition and current behavior |
| --- | --- |
| `name` | A string identifying the plugin in its metadata. Keep it equal to the directory name. The loader currently registers plugins by directory name, regardless of this value. |
| `version` | The plugin version as a string, such as `"1.0.0"`. Defaults to `"0.0.0"`; other plugins can require a compatible version. |
| `description` | A short description string. Defaults to `""`. The spelling is `description`, not `descrption`. |
| `author` | The author's name or attribution as a string. Defaults to `""`. |
| `module` | The loaded Python module, populated by the loader in `PluginInfo.module`. Omit it from `PLUGIN_META`: a supplied value does not select an entry point or replace the loaded module. Use `plugin.get(...)` to access the module. |
| `requirements` | A list of other **ish plugin** names, optionally with version constraints, such as `["shared_tools>=1.0,<2"]`. These plugins must exist under the plugin source directory; this does not download them. Defaults to `[]`. |
| `dependencies` | A list of **Python package** requirements, such as `["requests>=2.31,<3"]`. Missing or incompatible packages are installed with pip into `config.PLUGIN_LIB_DIR`. Defaults to `[]`. |

For Python packages whose distribution and import names differ, use
`"distribution>=version|import_name"`, for example `"PyYAML>=6|yaml"` in
`dependencies`. This separates the name passed to pip from the module checked
for import availability. Package installation requires access to the packages
through the bundled interpreter's pip, as described in the build section.

Installations are staged before replacing package files. Failed installation or
validation preserves the previous files, and upgrades remove obsolete files and
metadata while retaining other distributions in shared namespace packages.
Libraries already imported in the current process are not hot-reloaded. If a
dependency conflicts with those libraries or with a loaded plugin's requirements,
ish logs the failure and skips that plugin. Align the dependency requirements and
restart ish before retrying a version change. Dotted import names, such as
`"distribution|namespace.package"`, are supported.

Plugins normally load before `.ishrc.py` runs. `plugin.get("my_tools")` looks up
the registered module; it does not import an arbitrary file or trigger loading.
Check for `None`, particularly when starting with `--no-plugins` or after a plugin
fails to load.

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
PLUGIN_META = {
    "name": "my_tools",
    "version": "1.0.0",
    "description": "Shell hooks and greeting commands for ish.",
    "author": "Your Name",
    "requirements": [],
    "dependencies": [],
}


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

Each tool receives the exported environment from the latest confirmed shell
context, including variables changed or removed since ish started. Its working
directory comes from the live shell process, so an unset or modified `PWD` does
not redirect the tool. Both are applied in the worker before importing the tool
module. Worker changes do not affect ish or the shell. Shell-local variables,
aliases, and interpreter startup settings such as Python's import path are not
synchronized. If the context or directory is unavailable, the tool fails instead
of using ish's startup directory.

`prompt.last_exitcode` exposes the latest received shell status or completed tool
status as an integer (`None` before the first result). Normal tool returns,
including a returned integer, mean success (`0`); use `raise SystemExit(7)` to
report status `7`. Unhandled exceptions report `1`, and an unhandled Ctrl+C
reports `130`. Signal termination is displayed as `128 + signal_number`.
`prompt.last_tool_exitcode` retains the raw worker result (negative for signal
termination) until the next tool starts. Unavailable shell context reports `1`.
Worker startup or I/O failures still follow the session's existing error cleanup.

For example, display the latest result in `.ishrc.py`:

```python
prompt.bottom_toolbar = lambda: f"Exit: {prompt.last_exitcode}"
```

Python tools do not change the native shell's `$?` or `$status`.
`prompt.context.exitcode` continues to contain the shell's reported status, and
Python tools do not invoke `pre_hook`, `post_hook`, or `fallback_hook`.

### set_key

Key handlers run in the UI process and receive a prompt-toolkit key event:

```python
def insert_pwd(event):
    """Insert a command without submitting it."""
    event.current_buffer.insert_text("pwd")


prompt.set_key("f2", handler=insert_pwd)
prompt.set_key("c-x", "c-e", handler=insert_pwd)


def run_pwd(event):
    """Submit a command immediately."""
    event.app.exit("pwd")


prompt.set_key("f3", handler=run_pwd)

# Remove one sequence or every custom binding using this handler.
# prompt.set_key("f2")
# prompt.set_key(insert_pwd)
```

Use key names or `prompt_toolkit.keys.Keys` values. Multiple positional keys form
a sequence. Setting a sequence replaces its existing ish bindings, including
conditional variants. Keyword options such as `filter` and `eager` are forwarded
to prompt-toolkit's `KeyBindings.add`. Keep handlers short to avoid blocking input.

`event.app.exit("pwd")` ends the current prompt-toolkit input operation and returns
`"pwd"` to ish for command dispatch. It does not itself close ish. The supplied
string is submitted immediately instead of the current editor text. It can also
name a registered internal tool, for example `event.app.exit("greet Alice")`.

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

Pass a single `Completer` instance or an iterable of instances:

```python
from prompt_toolkit.completion import WordCompleter

prompt.set_completer(WordCompleter(["greet", "say"], ignore_case=True))

# Multiple sources can be supplied as a list, tuple, or generator.
# prompt.set_completer([first_completer, second_completer])

# Remove additional completers while retaining ish's default completion.
# prompt.set_completer()  # None or [] also clears additional sources.
```

ish lists these sources before its default completer, removes duplicate suggestions
while keeping the first occurrence, and runs completion in a worker thread.
Each call replaces the previous additional completers; invalid sources raise
`TypeError` without replacing the current setup.
A custom `Completer` subclass instance can also be supplied directly; its completion
code should not mutate UI state from the worker thread.

### Bottom toolbar, right prompt, and styles

Assign `prompt.bottom_toolbar` to show a bottom toolbar and `prompt.rprompt` to
show a right prompt. Each accepts plain text, prompt-toolkit formatted text
(such as `HTML` or style/text tuples), or a callable returning either. A callable
is evaluated during rendering, so keep it fast. Set either attribute to `None`
to hide it.

Assign a prompt-toolkit `Style` object to `prompt.style`. `Style.from_dict(...)`
maps style class names to color and formatting rules. Use `bottom-toolbar` for
the toolbar background, `bottom-toolbar.text` for its text, `rprompt` for the right
prompt, and `completion-menu.*` for completion candidates and their descriptions.
The example below uses `noreverse` to override the toolbar's default reverse style.
For additional formatting options, see the
[prompt-toolkit documentation](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html#adding-a-bottom-toolbar).

### Complete .ishrc.py example

After creating the `my_tools` plugin shown above, save the following as
`~/ish/.ishrc.py` (or `DIR/.ishrc.py` with `--home DIR`). It configures the UI,
retrieves the plugin module, registers its hooks and tool, and binds commands
to function keys.

```python
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style

# prompt, config, logger, option, and plugin are supplied by ish.
logger.info("Loading ish settings from %s", config.RC_FILE)

prompt.bottom_toolbar = " F2: pwd "
prompt.rprompt = f" {option.shell} "
prompt.style = Style.from_dict({
    "bottom-toolbar": "bg:#263238 #eeeeee noreverse",
    "bottom-toolbar.text": "#eeeeee",
    "rprompt": "bg:#263238 #80cbc4",
    "completion-menu.completion": "bg:#263238 #eeeeee",
    "completion-menu.completion.current": "bg:#80cbc4 #102027",
    "completion-menu.meta.completion": "bg:#37474f #cfd8dc",
    "completion-menu.meta.completion.current": "bg:#80cbc4 #102027",
    "completion-menu.multi-column-meta": "bg:#37474f #cfd8dc",
})


def run_pwd(event):
    """Submit pwd through ish's command dispatcher."""
    event.app.exit("pwd")


prompt.set_key("f2", handler=run_pwd)

# The lookup name is the plugin directory name, without a .py suffix.
my_tools = plugin.get("my_tools")
if my_tools is not None:
    prompt.pre_hook = my_tools.before_command
    prompt.post_hook = my_tools.after_command
    prompt.fallback_hook = my_tools.on_failure
    prompt.set_tool("greet", function=my_tools.greet)
    prompt.set_completer(WordCompleter(["greet"], ignore_case=True))

    def run_greeting(event):
        """Run the registered Python tool with a fixed argument."""
        event.app.exit("greet Alice")

    prompt.set_key("f3", handler=run_greeting)
    prompt.bottom_toolbar += "| F3: greet "
else:
    logger.warning("my_tools is unavailable; check plugin loading or --no-plugins.")
```

F2 immediately submits `pwd` to the selected shell. When `my_tools` is loaded,
F3 dispatches `greet Alice` to its Python worker. The rc file's key handlers stay
in the UI process, while the plugin's importable hooks and tool run in workers.

## Development environment

The current development environment is Windows with WSL 2 running Ubuntu 26.04
on x86-64 Linux 6.6.87.2. The project uses:

| Component | Version |
| --- | --- |
| Python | 3.12.14 |
| uv | 0.12.12 |
| prompt-toolkit | 3.0.53 |
| Pygments | 2.21.0 |
| Bundled CPython build | python-build-standalone 20260901 |
| GCC / glibc in the WSL build environment | 15 / 2.43 |

For source execution, use a Linux virtual environment, including inside WSL:

```sh
uv sync --locked
uv run ish bash
```

Do not share a virtual environment between Windows Python and Linux Python.
GCC is needed for the native helpers when running from source. The lockfile records the
application and build dependencies.

## Support scope

ish is intended for interactive Linux terminal use, including multiline commands,
input-waiting programs, typeahead, paste, Ctrl+C, Python tools, and return from
programs such as Vim and man. Linux kernel 4.13 or newer is required for the PTY
input handling used by ish.

- The primary prompt uses prompt-toolkit editing. Native Readline/ZLE bindings,
  native editor widgets, and every aspect of shell prompt rendering are not
  reproduced. At this prompt, Ctrl+C cancels editing without changing shell status.
- In zsh, queued continuation prompts are hidden when `PROMPT_SUBST` is enabled
  and the `zsh/zselect` module is available. ish preserves the existing option;
  otherwise, it displays native continuation prompts. The readiness check does
  not consume input or change TTY settings. With noncanonical input (such as
  `stty -icanon`), an incomplete queued line can hide a prompt that is still
  waiting for more input.
- If integration hooks are replaced or removed, confirm the shell is waiting for
  a command and enter `ish_recover`. BSD csh requires this explicit return after
  commands as described above.
- Long input support depends on the shell and current input state. Bash, zsh,
  tcsh, and BSD csh can accept a long first line at an eligible primary prompt.
  A long later line in a pasted block is rejected, and dash/sh retains its
  4,095-byte limit. Custom zsh SIGINT handling can disable long input support.
- tcsh keeps its native editor for secondary input, including lines over 4,095
  bytes once that editor is waiting. A bundled Linux preload library prevents
  tcsh from hiding trailing input in its private read-ahead buffer. It affects
  only the shell's own TTY readiness query, and is removed from the environment
  inherited by external programs. tcsh must be dynamically linked and allow
  preloading. Its editing TTY profile uses normal CR-to-LF conversion instead of
  LF-to-CR conversion; custom Enter bindings should treat LF as newline. Disabling
  `edit` also disables native long-secondary-line support.
- Session files are stored under `~/ish/.cache` by default. This location must be
  writable and permit executable files. Regular shutdown and handled TERM/HUP
  perform terminal and session cleanup; SIGKILL and OOM termination cannot do so.
  During handled TERM/HUP, foreground pipeline cleanup verifies surviving group
  members even if the leading process has exited. This requires permission to
  inspect processes; jobs that leave the group or session are outside that cleanup.
- Support is bounded by the host, shell version, startup configuration, and
  terminal. Indefinite unattended operation and compatibility with every Linux
  distribution are not guaranteed. RHEL 8.10 deployment requires a compatible
  build environment and confirmation on the actual host.
