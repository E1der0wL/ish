# ish

ish is an interactive command editor for Linux shells. It adds multiline editing,
syntax highlighting, completion, and Python UI extensions
through prompt-toolkit while your selected shell executes commands.

Use ish on Linux or inside WSL on Windows. Native Windows and macOS execution are
not supported. Run `ish --help` for command-line options and `ish --diagnose tcsh`
for an environment report without starting an interactive session.

### Typing while a command is running

The foreground program gets the keyboard first. At the next confirmed ish
prompt, ish restores any unread text in batches and interprets editing keys
such as the arrows and Backspace. For example, ↑ during `sleep 2` may echo
`^[[A` while the command runs, then recall `sleep 2` for editing when it
finishes. Recall uses the history available to the ish editor.

In the default Emacs editing mode, Alt+B and Alt+F use the selected shell's
stock word boundaries for Bash, zsh, and tcsh. Keys returned from the
foreground PTY use default bindings. Keys typed directly into the ish
prompt or held by ish before reaching that PTY retain your `.ishrc.py`
bindings. Shell-specific Readline, ZLE, and tcsh keymaps belong to their
respective native editors. For example, some native keymaps leave Ctrl+Left
unbound while ish uses it for word motion.

Enter submits the current command line. If the shell requests another syntax
line, ish opens the supported continuation editor. Subsequent bytes stay available
for the command to read. Complete bracketed pastes keep their
contents together, and already-submitted command blocks retain their text.
The terminal can erase or transform bytes before ish gets them back. For
example, in canonical mode, Ctrl+V can quote a literal Backspace. If that
byte is forwarded to another command's canonical terminal, it can act as
Backspace there because the original quoting has already been consumed.
On eligible Bash and zsh sessions, ish holds input after Enter until a correlated
shell request identifies its consumer: PTK continuation editing, native stdin,
or the next confirmed primary editor. Programs such as Vim and ordinary Python
programs using `input()` retain their native input paths. tcsh, BSD csh, sh/dash, and
sessions using a fallback configuration retain native continuation.

## Supported shells

The selected shell must be installed on the host; the distribution does not bundle
shell executables. Specify a shell name or its executable path. If omitted, ish
selects the login shell by name.

| Shell | Minimum supported version | Argument | Support notes |
| --- | --- | --- | --- |
| Bash | 5.3.9 | `bash` | PTK primary and continuation editing when the conditions below hold; verified on 5.3.9. Native Readline stays disabled. |
| zsh | 5.5.1 | `zsh` | PTK primary and conditional continuation editing with native ZLE disabled. The semantic continuation path was verified on 5.9. |
| tcsh | 6.21.00 | `tcsh` | Supports primary prompt hooks and return to the ish editor; continuation remains native. |
| BSD csh | Not specified | `csh` or `bsd-csh` | After a command, run `ish_recover` at the native prompt to resume ish editing. |
| dash / sh | Not specified | `dash` or `sh` | The sh integration is exercised with dash. Input lines over 4,095 encoded bytes are rejected. |

A `csh` executable that resolves to tcsh uses tcsh integration. Other implementations
named `sh` are not automatically covered by dash support.

PTK continuation is selected at each confirmed primary boundary:

- Bash requires 5.3.9 or newer, `promptvars`, and ish's integration hook as the
  final `PROMPT_COMMAND` entry. User PS0 output, existing hooks, and their exit
  status are preserved. A later prompt hook or `shopt -u promptvars` selects
  native continuation for subsequent submissions.
- zsh requires `PROMPT_SUBST`, native ZLE disabled, and the installed ish
  `preexec` and interrupt acknowledgement hooks intact. A custom `TRAPINT` is
  preserved and selects native continuation. User `preexec`/`precmd` hooks and
  their status retain the existing integration behavior.

In this mode, PS2 emits a request through the private control FIFO and contributes
no visible prompt. PTK displays its own `> ` continuation prompt. Continuation
and heredoc replies are shell text. Primary submissions also go directly to the
selected shell: ish has no text-based internal command or Python tool dispatcher.
Primary display retains
the existing shell prompt compatibility and optional `prompt_text` override.
Prompt appearance does not determine input ownership.

Native fallback retains Bash's `> `, zsh's `%_> ` (such as `for> `), and tcsh's
`%R? ` (such as `foreach? `). At primary prompts, including after `ish_recover`,
integration rebuilds the applicable PS2/prompt2 and its markers. Custom
continuation colors, text, and command substitutions are not retained. See
[the input contract](docs/EDITING.md) for identities, synchronization, and the
[remaining shell boundaries](docs/HANDOFF_LIMITS.md) for unimplemented support.

ish enables **`PROMPT_SUBST` when starting zsh and when running `ish_recover`**.
It automatically loads the `zsh/zselect` module when available to hide repeated
native continuation prompts during multiline paste. The semantic PTK path does
not require this module. No `.zshrc` changes are needed.
To load the module and enable its builtin inside the zsh selected for ish:

```zsh
builtin zmodload -F zsh/zselect b:zselect
```

`PROMPT_SUBST` expands variables, commands, and arithmetic in both primary and
continuation prompts. To display literal text such as `$(date +%H:%M)`, escape the
dollar sign and double the percent signs:

```zsh
PS1='\$(date +%%H:%%M) > '
```

You can run `unsetopt PROMPT_SUBST` during the session to disable expansion;
ordinary prompt updates preserve that choice. Running `ish_recover` re-enables
the option and retries loading `zsh/zselect`. Recovery cannot provide a module
that is not installed or supported by the selected zsh build. Disabling
`PROMPT_SUBST` selects native continuation. In a native fallback configuration,
missing `zselect` can leave repeated continuation prompts visible during paste.

These minimum versions define the existing supported baseline. Select a supported
shell yourself; shell selection does not reject every older binary. Separately,
the Bash semantic capability checks its minimum version before activation.
That fallback does not establish general compatibility with older Bash releases.
Version guidance is also available through `ish --help`. Older versions may fail to
initialize or process commands incorrectly, including older tcsh releases affected
by `postcmd` loop-handling defects.

On hosts with older system shells, install a supported shell in a separate
directory and select it explicitly, for example
`ish "$HOME/opt/bash-5.3.9/bin/bash"`. The host's system shell can remain in place.

### Installing zsh/zselect

`zselect` is part of zsh's module system, not a separate command-line program or
Python package. Install the zsh package and its matching modules. These commands
are instructions to run yourself; ish does not install packages or require the
module to start.

**With repository access**

RHEL 8 / AlmaLinux 8:

```sh
sudo dnf install zsh
```

Debian / Ubuntu:

```sh
sudo apt-get update
sudo apt-get install zsh zsh-common
```

If zsh is already installed but packaged module files are missing, use
`sudo dnf reinstall zsh` or
`sudo apt-get install --reinstall zsh zsh-common`.
These packages repair the distribution's zsh; a custom zsh executable needs
modules from its own build. After installation, run the load/check command above
inside the selected zsh. Exit status 0 means the feature was enabled. Run
`ish_recover` to restore ish's prompt integration.

**Offline installation on RHEL 8.10 / AlmaLinux 8.10**

On a connected machine with the **same distribution, release, architecture, and
compatible repository snapshot** as the target, prepare the packages:

```sh
sudo dnf install dnf-plugins-core
mkdir -p zsh-offline
dnf download --resolve --alldeps --downloaddir=./zsh-offline zsh
```

`--alldeps` includes dependencies already installed on the download machine.
Use the target organization's RHEL repositories for RHEL targets; do not mix
RPMs from another distribution. Transfer the complete `zsh-offline` directory
through your approved file-transfer method. On the offline target:

```sh
sudo dnf --disablerepo='*' install ./zsh-offline/*.rpm
```

If the same zsh package version is already installed and needs repair, use:

```sh
sudo dnf --disablerepo='*' reinstall ./zsh-offline/zsh-*.rpm
```

Missing dependencies must also be transferred; the install command above cannot
download them. An internal mirror or matching installation media can also supply
the packages. See the [DNF download options](https://dnf-plugins-core.readthedocs.io/en/latest/download.html)
and [local RPM installation reference](https://dnf.readthedocs.io/en/latest/command_ref.html#install-examples).

**Offline source installation under your home directory**

Download an official release archive from the [zsh website](https://www.zsh.org/)
on a connected machine and transfer it to the target. The example uses zsh 5.9.
The target needs GCC, Make, tar/xz, and ncurses development headers: typically
`gcc make tar xz ncurses-devel` on RHEL, or
`build-essential tar xz-utils libncurses-dev` on Debian/Ubuntu. Arrange these
packages and their dependencies before building offline.

Run from the directory containing the archive:

```sh
tar -xf zsh-5.9.tar.xz
cd zsh-5.9
./configure --prefix="$HOME/opt/zsh-5.9" --enable-dynamic
make -C Src -j2
make install.bin install.modules install.fns
"$HOME/opt/zsh-5.9/bin/zsh" -f -c 'builtin zmodload -F zsh/zselect b:zselect'
ish "$HOME/opt/zsh-5.9/bin/zsh"
```

Building `Src` avoids documentation-generation dependencies. Installing modules
is essential; copying only the executable is insufficient for a dynamic build.
Do not transplant a module from an unrelated zsh build. This procedure installs
the new shell under your home and leaves the system shell in place. See the
[official zsh installation guide](https://github.com/zsh-users/zsh/blob/zsh-5.9/INSTALL).

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
and `plugin` objects. Restart ish to reload settings, or bind an explicit key
callback to `prompt.load_rc_async()` as shown below. Restart ish after changing
plugin source files.

`--no-rc` skips creating and loading this startup file. `--no-plugins` skips automatic plugin loading.
Neither option skips the selected shell's own startup files. `--home` changes
ish's settings location without changing the shell's `HOME`.

The following objects are available directly in `.ishrc.py`, without importing them:

| Object | Purpose |
| --- | --- |
| `prompt` | The active ish `Prompt` instance. Configure key bindings, completion, toolbar, right prompt, and styles. |
| `config` | The current configuration and resolved paths, such as `config.ISH_HOME`, `config.RC_FILE`, and `config.CACHE_DIR`. |
| `logger` | The session logger. Use methods such as `logger.info(...)` and `logger.warning(...)` for configuration diagnostics. |
| `option` | Parsed command-line options, including `option.shell`, `option.lang`, `option.home`, and `option.no_plugins`. |
| `plugin` | The plugin manager. `plugin.get("my_ui")` retrieves an already loaded plugin module, or returns `None` if it is unavailable. |

### Plugin metadata: PLUGIN_META

Place each plugin in its own directory under `~/ish/plugin/script/`. For example,
`my_ui` can use `my_ui/__init__.py` or `my_ui/my_ui.py` as its entry
point. If both exist, `my_ui.py` takes precedence. With `--home DIR`, the base
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
| `dependencies` | A list of **Python package** requirements in PEP 508 format, such as `["requests[socks]>=2.31,<3; python_version >= '3.10'"]`. Inactive environment markers are skipped; requested extras are checked against installed dependency metadata. Missing or incompatible packages are installed with pip into `config.PLUGIN_LIB_DIR`. Defaults to `[]`. |

For Python packages whose distribution and import names differ, use
`"distribution>=version|import_name"`, for example `"PyYAML>=6|yaml"` in
`dependencies`. This separates the name passed to pip from the module checked
for import availability. Package installation requires access to the packages
through the bundled interpreter's pip, as described in the build section.

Dependency markers use ish's Python runtime environment. Extras checks run only
when loading a plugin. Direct URL requirements (`name @ URL`) are resolved by pip
once per session because an installed package name alone cannot confirm its source.

Installations are staged before replacing package files. Failed installation or
validation preserves the previous files, and upgrades remove obsolete files and
metadata while retaining other distributions in shared namespace packages.
Libraries already imported in the current process are not hot-reloaded. If a
dependency conflicts with those libraries or with a loaded plugin's requirements,
ish logs the failure and skips that plugin. Align the dependency requirements and
restart ish before retrying a version change. Dotted import names, such as
`"distribution|namespace.package"`, are supported.

Plugins normally load before `.ishrc.py` runs. `plugin.get("my_ui")` looks up
the registered module; it does not import an arbitrary file or trigger loading.
Check for `None`, particularly when starting with `--no-plugins` or after a plugin
fails to load.

### Command execution and API compatibility

Every submitted string goes to the selected shell, including `ish_exit`,
`ish_reload`, and names previously registered as Python tools. These names have
ordinary shell meaning; without a matching shell command, the shell reports
command not found. Exit with Ctrl+D in an empty primary editor or the shell's
`exit` command.

This is a breaking change for configurations using the removed Python execution
API: `set_tool`, `internal_tools`, `internal_commands`, `exit_command`, `runner`,
`last_tool_exitcode`, `read_and_dispatch`, `pre_hook`, `post_hook`, and
`fallback_hook`, together with the `pre_exec`, `post_exec`, and `fallback`
execution methods. Remove those registrations and calls from existing rc files
and plugins.
Calls to removed methods fail; assigning an obsolete hook attribute may still
succeed as a Python assignment, but ish will not execute that hook.
Run Python programs through the shell, for example `python3 script.py`.
The shell's own `preexec`/`precmd` hooks remain part of shell integration.

Python configuration and plugins still customize the UI. Key callbacks,
completion, toolbar functions, `prompt.cmd()`, and explicit rc loading remain
available. `prompt.last_exitcode` exposes the latest received shell status as an
integer (`None` before the first result):

```python
prompt.bottom_toolbar = lambda: f"Exit: {prompt.last_exitcode}"
```

To reload settings explicitly from a key callback:

```python
async def reload_settings(event):
    await prompt.load_rc_async()
    event.app.invalidate()


prompt.set_key("f5", handler=reload_settings)
```

Reloading re-executes the rc file; it does not undo old registrations that are
absent from the new file or reload imported plugin modules.

### UI plugins

For example, create `~/ish/plugin/script/my_ui/__init__.py` with:

```python
PLUGIN_META = {
    "name": "my_ui",
    "version": "1.0.0",
    "description": "Status text for the ish toolbar.",
    "author": "Your Name",
    "requirements": [],
    "dependencies": [],
}


def status_text(exitcode):
    """Format the shell status without executing a command."""
    return f"Exit: {exitcode}"
```

For a custom home, use `DIR/plugin/script/my_ui/`. UI callbacks execute in ish's
process and can be defined directly in `.ishrc.py`; they do not require a Python
worker or pickleable functions. Keep rendering and key callbacks quick.

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
`"pwd"` as shell text. It does not itself close ish. `prompt.cmd("pwd")` provides
the same submission behavior. The supplied string replaces the current editor
text; in a continuation editor it answers that syntax request. These APIs do not
turn continuation text into a separate primary command.

### Optional paired-character editing

Quotes, backticks, and brackets insert only the character you type by default.
Backspace deletes one preceding character. To enable automatic pair insertion,
skipping an existing identical quote, and deleting adjacent pairs with Backspace,
add this example to `.ishrc.py`:

<!-- ish-example: paired-editing -->
```python
from functools import partial

from prompt_toolkit.keys import Keys

pairs = {"(": ")", "{": "}", "[": "]", '"': '"', "'": "'", "`": "`"}


def insert_pair(event, opening):
    """Pair at the input end, or skip an identical quote at the cursor."""
    buffer = event.current_buffer
    document = buffer.document
    closing = pairs[opening]
    symmetric = opening == closing
    if symmetric and document.current_char == closing:
        buffer.cursor_right()
    elif document.is_cursor_at_the_end and (
        not symmetric or document.text.count(opening) % 2 == 0
    ):
        buffer.insert_text(opening + closing)
        buffer.cursor_left()
    else:
        buffer.insert_text(opening)


def delete_pair(event):
    """Delete an adjacent configured pair, or one preceding character."""
    buffer = event.current_buffer
    document = buffer.document
    closing = pairs.get(document.char_before_cursor)
    if closing is not None and document.current_char == closing:
        buffer.cursor_right()
        buffer.delete_before_cursor(2)
    else:
        buffer.delete_before_cursor(1)


for opening in pairs:
    prompt.set_key(opening, handler=partial(insert_pair, opening=opening))
prompt.set_key(Keys.Backspace, handler=delete_pair)
```
<!-- /ish-example: paired-editing -->

Change `pairs` to choose the characters you want to pair. This is an editing
heuristic, not a shell syntax parser: quotes are counted without interpreting
escapes or shell options. It pairs only at the end of the complete input buffer.
Only symmetric pairs (quotes and backticks) skip an existing closer; closing
parentheses, braces, and brackets still insert normally. Backspace deletes any
matching adjacent pair, including one inserted manually or by paste.

These handlers apply to keys typed in the active PTK editor. Text that comes
back from a foreground program, bracketed paste contents, and previously
submitted commands keep their original quotes and parentheses.
The handlers replace only their own key sequences, leaving Enter, Tab, and other
bindings intact. Reloading the rc file does not add duplicate
bindings. Removing lines or pair entries from `.ishrc.py` does not undo
registrations in an already-running session; restart ish or explicitly remove
the previously configured keys and restore the default behavior:

```python
for opening in pairs:
    prompt.set_key(opening)
prompt.set_key(
    Keys.Backspace,
    handler=lambda event: event.current_buffer.delete_before_cursor(1),
)
```

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

prompt.set_completer(WordCompleter(["pwd", "git"], ignore_case=True))

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

For shell-aware custom completion, `prompt.completion_context(document)` returns
the current word's `start`, decoded `value`, `command` position flag, `dynamic`
flag for expansion or unsupported syntax, and open `quote`. It returns `None`
when replacing the prefix could damage text after the cursor. Literal providers
should skip dynamic contexts.
`prompt.quote_argument(value)` quotes one complete literal word for the selected
shell. Use it when replacing the whole current word, not inside existing quotes:

```python
from prompt_toolkit.completion import Completer, Completion


class ProjectCompleter(Completer):
    def get_completions(self, document, complete_event):
        context = prompt.completion_context(document)
        if context is None or context.dynamic or context.command:
            return
        for name in ("project alpha", "project beta"):
            if name.startswith(context.value):
                yield Completion(
                    text=prompt.quote_argument(name),
                    start_position=context.start - document.cursor_position,
                    display=name,
                )


prompt.set_completer(ProjectCompleter())
```

Additional completers keep control of their own insertion text; ish does not
re-quote or filter their results through its default parsing policy.
Default completion uses the selected shell's lexical rules without evaluating
variables, command substitutions, or globs. It can decline a context it cannot
interpret safely. Completion does not decide whether a command is complete or
whether to open a continuation editor; the shell supplies that boundary. These
helpers do not mirror arbitrary custom history characters, aliases, or all live
shell options.

### Auto suggestions

History-based suggestions are the default. Assign an `AutoSuggest` instance to
`prompt.auto_suggest` in `.ishrc.py` to replace them:

```python
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion


class CommandSuggest(AutoSuggest):
    def get_suggestion(self, buffer, document):
        """Suggest only the missing suffix of a matching command."""
        if not document.is_cursor_at_the_end:
            return None
        text = document.text
        for command in ("git status", "git diff", "git log --oneline"):
            if text and command.startswith(text) and command != text:
                return Suggestion(command[len(text):])
        return None


prompt.auto_suggest = CommandSuggest()
```

Return only the text to append in `Suggestion`, or `None` for no suggestion.
This controls the inline suggestion, independently of `set_completer()` and its
completion menu. The assignment also takes effect after reloading the rc file.

```python
# Disable inline suggestions.
prompt.auto_suggest = None

# Restore history-based suggestions.
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

prompt.auto_suggest = AutoSuggestFromHistory()
```

Suggestion callbacks normally run on the UI event loop, so keep them quick.
For blocking work, wrap a thread-safe provider with
`ThreadedAutoSuggest(CommandSuggest())` from `prompt_toolkit.auto_suggest`, or
implement `get_suggestion_async()` for asynchronous I/O. Worker callbacks should
not modify the UI.

### Primary prompt

By default, ish displays the prompt received from the shell. Set
`prompt.prompt_text` in `.ishrc.py` to replace its appearance:

```python
from prompt_toolkit.formatted_text import HTML

prompt.prompt_text = HTML("<ansicyan>ish</ansicyan> > ")
```

The setting follows prompt-toolkit's `AnyFormattedText` contract: plain strings,
`HTML`, `ANSI`, `FormattedText`, style/text tuple lists, objects implementing
`__pt_formatted_text__()`, and no-argument callables returning formatted text are
supported. For example:

```python
prompt.prompt_text = lambda: [("class:prompt", f"[{prompt.last_exitcode}] > ")]
```

Set `prompt.prompt_text = None` to restore the latest shell prompt, or use `""`
to hide only the prompt text. Existing command output without a trailing newline
is preserved before the replacement prompt. A multiline value uses the existing
prompt-toolkit layout, including the right prompt and completion menu.

This setting controls the primary prompt's display. Shell prompt hooks, context
updates, and input handoff keep running normally; continuation follows its
configured semantic or native path. The setting does not avoid the shell's
original primary prompt computation. Shell escapes such as
`\u`, `%n`, `$PWD`, or `$(date)` in a string are displayed literally rather than
evaluated. Dynamic callbacks run during rendering, possibly more than once per
redraw, so keep them fast and free of side effects. No polling is added.

### Read a running TUI's text screen

Call `prompt.enable_screen_tracking()` in `.ishrc.py` **before** starting Vim or
another TUI. A plugin in the ish process can then read the current foreground
PTY text even while the application owns the keyboard. For example, give a
resident plugin the getter once during setup:

```python
# .ishrc.py
prompt.enable_screen_tracking()
monitor = plugin.get("monitor")
if monitor is not None:
    monitor.read_screen = prompt.screen_snapshot
```

A callback or thread in the plugin can then use its module-level getter:

```python
# monitor.py
def current_status():
    frame = read_screen()  # Set by .ishrc.py after the plugin loads.
    if frame is not None and frame.alternate:
        return frame.lines[-1].rstrip(), (frame.cursor_row, frame.cursor_column)
    return None
```

Each snapshot is immutable. `lines` contains `rows` strings, including blank
rows and trailing spaces. Cursor coordinates are zero-based terminal cells;
a wide character can occupy two columns. The observer follows cursor motion,
overwrites, erasure, resizing, and the primary/alternate screen switch.
On leaving the alternate screen the getter returns the primary grid.
The PTY stream includes Python programs launched by the shell. PTK prompts,
toolbars and popups are drawn separately. Terminal graphics, styling and off-screen Vim
buffer contents are outside the text snapshot.

Tracking is off by default and starts with a blank grid. A repeated enable
keeps the existing content. Use `prompt.enable_screen_tracking(False)` to
discard it; `screen_snapshot()` returns `None` while tracking is off.
The build includes pyte, which ish loads on the first enable. The observer
limits each grid to 262,144 cells and stops tracking if it cannot process a
screen. Screen updates cost CPU while enabled. Ordinary scrollback
remains available independently. Read snapshots from a resident UI callback in
ish's process.

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

After creating the `my_ui` plugin shown above, save the following as
`~/ish/.ishrc.py` (or `DIR/.ishrc.py` with `--home DIR`). It configures the UI,
retrieves the plugin module, and binds shell submissions
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
    """Return pwd as the editor's shell submission."""
    event.app.exit("pwd")


prompt.set_key("f2", handler=run_pwd)

# The lookup name is the plugin directory name, without a .py suffix.
my_ui = plugin.get("my_ui")
if my_ui is not None:
    prompt.bottom_toolbar = lambda: (
        " F2: pwd | " + my_ui.status_text(prompt.last_exitcode)
    )
else:
    logger.warning("my_ui is unavailable; check plugin loading or --no-plugins.")

prompt.set_completer(WordCompleter(["pwd", "git"], ignore_case=True))
```

At a primary editor, F2 immediately submits `pwd` to the selected shell.
The plugin formats the toolbar in the UI process without registering a command.

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

### Editor requests on this branch

The session grants one editor reply at an acknowledged primary prompt or a valid
shell syntax request. Submission IDs, per-submission request sequences, primary
prompt identities, and local `EditRequest.id` values have separate roles.
Cancelled, duplicate, foreign, and out-of-order requests cannot authorize input.
The common session holds trailing bytes until their consumer is known and uses
the existing primary ACK boundary to recover unread native input.

The session calls `Prompt.prompt_async()` for text and sends each accepted reply
to the shell. Custom editors must return text. Text-based internal commands,
Python workers, and the old dispatch and Python execution-hook APIs are removed;
see the compatibility notes above. Shell integration hooks remain in place.
The existing private control FIFO still carries shell state and semantic events.
The separate tcsh syntax-FIFO experiment is not part of the live session path.

The Bash and zsh adapters implement the same contract using their own shell
boundaries. tcsh, BSD csh, and sh/dash continuation remains native because the
necessary guarantees have not been established. The goal of PTK continuation
for every supported shell is still incomplete. [EDITING.md](docs/EDITING.md)
records the architecture, verified configurations, test commands, and stage
status; [HANDOFF_LIMITS.md](docs/HANDOFF_LIMITS.md) records the remaining evidence
and required native cooperation.

The tracked contract and live continuation tests run with:

```sh
PYTHONPATH=src:tests uv run python -B -m unittest \
  test_handoff_protocol test_edit_requests test_shell_editing \
  test_bash_edit_protocol test_semantic_edit_cli \
  test_zsh_semantic test_zsh_edit_cli test_semantic_races \
  test_shell_submission_cli
```

## Support scope

ish is intended for interactive Linux terminal use, including multiline commands,
input-waiting programs, typeahead, paste, Ctrl+C, Python programs, and return from
programs such as Vim and man. Linux kernel 4.13 or newer is required for the PTY
input handling used by ish.

- The primary prompt uses prompt-toolkit editing. Native Readline/ZLE bindings,
  native editor widgets, and every aspect of shell prompt rendering are not
  reproduced. At this prompt, Ctrl+C cancels editing without changing shell status;
  Ctrl+D on an empty editor exits ish successfully. At a semantic continuation,
  Ctrl+C follows the adapter's interrupt protocol and Ctrl+D sends the PTY's
  configured EOF character to the waiting shell parser.
- Bash and zsh continuation editing uses PTK when the supported configuration
  above is active. It does not replace program stdin or Python `input()`.
- In zsh's native fallback, queued continuation prompts are hidden when
  `PROMPT_SUBST` is enabled and the `zsh/zselect` module is available. ish enables
  the option at startup and on explicit `ish_recover`; ordinary prompt updates
  preserve user changes.
  If either condition is unmet, native continuation prompts remain visible.
  This display hint does not authorize input or change TTY settings.
  With noncanonical input (such as `stty -icanon`), an incomplete queued line can
  hide a prompt that is still waiting for more input.
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
