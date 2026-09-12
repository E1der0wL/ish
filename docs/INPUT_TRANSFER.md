# Input transfer across shell, editor, and Python tools

This change addresses all four input-transfer defects recorded in
[the original observation report](INPUT_OBSERVATION.md). The metadata observer
remains available, but input transfers are now coordinated independently of
whether observation is enabled.

## Changes

### Shell prompt handoff

A fresh state frame from the shell's prompt hook starts the handoff. Keyboard
input arriving after that boundary is held instead of being written into the
PTY while the shell is finishing its prompt hook. A matching fresh prompt is
still required before the editor resumes. State receipt alone does not start
the editor or authorize a maintenance command.

At prompt acceptance, the older forwarded FIFO data is collected before the
newer held input. This closes the tcsh 6.20 window in which a later prompt and
FIFO input arrived after the editor had already resumed, leaving an earlier
command stranded until after the next user command.

Queued raw-input callbacks check whether their reader is still active before
reading. Ctrl+C continues to reach the shell during the handoff; ordinary
typeahead buffering does not make a slow prompt hook uninterruptible.

### Python tool entry

The tool receives pending FIFO bytes, saved prompt-toolkit typeahead, and any
incomplete encoded character, in that order. Reading the toolkit typeahead store
removes those keys from the next editor invocation, so a reply is not also
executed later as a shell command. Cursor-position reports are excluded from
user input.

Tool completion returns directly to the editor instead of submitting an empty
shell command. This also avoids leaving BSD csh in native mode after a tool that
did not execute a shell command.

### Python tool exit

The parent retains its worker PTY slave until the worker exits. On exit, it:

1. Stops the worker's input reader and queued input writes.
2. Reads terminal input the worker has not consumed, including an unfinished line.
3. Appends bytes still waiting in the writer queue after those terminal bytes.
4. Returns that input to the editor and closes the retained slave.
5. Drains the remaining output through the existing output path.

The temporary canonical-mode change uses `TCSANOW`, not a flush. It is performed
only after worker exit. The slave is closed before waiting for output EOF, so
retaining it for input recovery does not prevent normal EOF detection. A queued
input callback cannot read more keyboard input after the worker writer closes.

Already-consumed bytes in a tool's own memory cannot be reconstructed. For
example, a tool that intentionally reads a large input block and ignores part
of it has consumed that block; this change does not replay it. Tools that leave
background descendants sharing their PTY are not given a new session-lifetime
guarantee by this change.

### BSD csh typeahead

Before handing execution to a shell that requires explicit reconnect, ish
transfers remaining editor typeahead along with the submitted user command.
Only user-supplied bytes are sent. There is no appended restoration command,
automatic `ish_recover`, or prompt-text guessing.

BSD csh can therefore execute all queued commands while remaining in native
input. Returning to the ish editor after a shell command still requires an
explicit `ish_recover`; this change does not add a prompt hook that BSD csh
does not provide.

### Split characters

An incomplete encoded character can live in the editor decoder or in a worker's
unread terminal queue at handoff. The transfer preserves those bytes. Returned
characters also survive the new editor's cursor-position query: the protocol
reply is handled separately rather than being inserted between the bytes of
the character. Normal input continues to use the existing parser and key list.

The decoder bridge is isolated to the existing input adapter and uses the
VT100 input implementation in the pinned prompt-toolkit 3.0.53 dependency.
Changing that dependency should include rerunning the decoder contract tests.

## Validation

The same real-PTY diagnostic runner used for the original findings now checks:

- Enter followed by three-command batches in one write, adjacent writes, and
  writes separated by approximately 20 ms.
- Complete multiline `for`/`foreach` blocks.
- Typeahead while a slow command waits behind a release barrier.
- Repeated shell → Python tool → editor cycles.
- A tool invocation and its reply arriving together.
- A tool's final reply and the following shell command arriving together.
- A Unicode character split across tool entry or tool exit, including an
  intervening cursor-position response after exit.

Each run compares the exact ordered records written by the executed commands
or tools, not echoed text. BSD csh tests explicitly reconnect only when they
need another ish editing session. Observer traces are checked for overlapping
consumers and reads outside the observed lifetime.

The test environment is WSL2 Linux 6.6.87.2, Python 3.12.14 and prompt-toolkit
3.0.53. Shells include Bash 5.3.9, zsh 5.9, dash 0.5.12, tcsh 6.24.13,
real BSD csh, and a separate tcsh 6.20.00 binary. The older binary matches the
reported version, but these tests are not a certification on RHEL 8.10 itself.

All **56 real-PTY scenario runs passed** with exact expected/actual record
sequences, no observed ownership conflicts, and no truncated traces:

| Shell | Scenarios | Observation modes | Passed |
| --- | --- | --- | --- |
| Bash 5.3.9 | 8 | Enabled | 8/8 |
| zsh 5.9 | 8 | Enabled | 8/8 |
| dash 0.5.12 | 8 | Enabled | 8/8 |
| tcsh 6.24.13 | 8 | Enabled | 8/8 |
| BSD csh | 8 | Enabled | 8/8 |
| tcsh 6.20.00 | 8 | Disabled and enabled | 16/16 |

The complete regression suite passes: **208 tests in 352.394 seconds**, including
the existing Vim/man return, shell integration, and continuation tests.

The 10 added regression tests also pass, covering the failing CLI boundaries,
partial characters with CPR, transfer of unsent writer bytes, worker FD cleanup,
FIFO ordering, detached callbacks, and Ctrl+C during handoff. Ruff lint and
format checks pass for all eight changed Python files.

Local evidence (ignored by Git):

- [Exact expected/actual results for all 56 scenarios](../dist/input-transfer-results.json)
- [tcsh 6.20 enabled/disabled comparison](../dist/input-transfer-tcsh620-final/summary.json)
- [New regression tests](../dist/input-transfer-tests-final.log)
- [Final full-suite log](../dist/input-transfer-full-suite-final.log)

Reproduce the main scenarios from the project root in WSL:

```sh
PYTHONPATH=src:tests .venv/bin/python -B tests/input_observation_probe.py \
    --shells bash zsh tcsh sh \
    --scenarios enter paste slow tool tool_early tool_tail tool_split_entry tool_split_exit \
    --modes on --output dist/input-transfer
```

Set `ISH_TEST_TCSH` or `ISH_TEST_CSH` to select an older tcsh or a real BSD csh
binary. The `native` mode in the diagnostic runner was a before-change comparison
that bypassed the input adapter; it is not used for validating the new decoder bridge.

The source changes do not rebuild an existing Nuitka executable. Restart source
sessions to load the fixes; rebuild distributed executables before testing them.
