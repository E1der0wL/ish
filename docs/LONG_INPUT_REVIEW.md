# Long command input: TTY staging experiment

Date: 2026-09-12

Historical experiment report. The unrestricted candidate below was rejected.
The subsequent implementation gates each shell and input context before any
transmission; see [gated long-input handling](LONG_INPUT.md) for current behavior.

## Decision

**Do not enable the experimental transport in the production engine.** Keeping
the native editors disabled and temporarily changing the inner PTY's input mode
successfully transports long, valid first lines. However, the experiment exposed
a parser-recovery failure in dash and does not safely handle long lines later in
a pasted block. It therefore fails the requested condition that existing shell
behavior remain reliable before deployment.

No production Python files, shell integration scripts, or editor settings were
changed in this investigation. The candidate is installed only in isolated test
homes by explicit diagnostic runners. The previously implemented termination
and input-ownership changes remain in place.

## Candidate examined

The experiment keeps Bash `--noediting`, zsh `unsetopt zle`, and the existing
tcsh editor policy. For a first physical line exceeding the canonical limit, it:

1. Opens the PTY slave with `TIOCGPTPEER` and saves its current terminal settings.
2. Disables `ICANON` and `ECHO`, setting `VMIN=1` and `VTIME=0`.
3. Streams the prefix while withholding its terminating newline. Existing
   backpressure, cancellation generations, and typeahead deferral remain active.
4. Checks slave input readiness with `poll`, then restores the saved settings
   before sending the newline and any remaining lines.
5. Restores settings in cleanup and before delivering a submission interrupt.

The temporary slave descriptor is closed after staging; there is no permanent
additional reader, supervisor process, or idle polling. The experiment adjusts
echo masking for the suppressed prefix rather than enabling a second editor.

This is a transport experiment, not a new acknowledgement protocol. An empty
kernel input queue does not establish that the shell has retained an entire
command or that its parser has not rejected an earlier fragment. Linux's
[N_TTY documentation](https://www.kernel.org/doc/html/latest/driver-api/tty/n_tty.html)
describes the canonical line limit and noncanonical input path. Neither is an
acknowledgement of command parsing or execution.

## Findings

### Blocking: dash can reject a fragment before the newline is submitted

The controlled input is a syntax error followed by a long word and a harmless
file-creation command, all on one physical line:

```text
) <12,000 ASCII x bytes>; touch unexpected; python3 consumer.py
```

In the direct PTY comparison, the canonical version does not create `unexpected`.
The staged version can recover from the initial syntax error and interpret the
later suffix as a new command, creating that file. These fixtures run only in
private temporary directories.

With the real ish integration, the same failure also interacts with the prompt
hook and typeahead forwarder: a suffix reappears in the editor and the expected
empty prompt does not return within the observation deadline. The initial CLI
case and three subsequent repetitions reproduced this behavior. A short,
unmodified canonical syntax-error control returns normally.

Restoring TTY settings before the final newline is insufficient. The parser can
already have abandoned the original input before that restoration. The current
fresh-prompt handshake protects normal ownership transfers; it does not make a
partially streamed command an atomic parser transaction.

### Unresolved: later physical lines in a pasted block

Changing modes for only the first line leaves subsequent long lines subject to
the existing canonical truncation problem. The explicit probe was:

```text
touch first
printf '%s' '<12,000 ASCII x bytes>' > payload; python3 consumer.py
```

The Bash/dash cases did not produce the expected payload. One zsh run wrote only
4,331 bytes and still returned to the prompt; this number is an observed result,
not a stable limit. Thus prompt return or exit status alone cannot validate
preservation of a submitted block.

This is an existing limitation left unresolved by the candidate, not a new
regression caused by the first-line branch. Automatically switching modes for
every later line is unsafe: a preceding line may have started `read`, `cat`,
Vim, a pipeline, or another stdin consumer. The existing protocol does not
identify the consumer of every physical line in an arbitrary pasted block.

### Confirmed: leaving noncanonical mode active changes program input

The direct probe starts a Python stdin consumer and enters `ab`, DEL, `c`, Enter.
With noncanonical mode left active, the program observes `ICANON=False`,
`ECHO=False`, and receives `ab\x7fc`. With successful staged restoration it
observes both original flags and receives `ac`.

This confirms why a permanent `stty -icanon -echo` change is unsuitable. It is
not necessary to re-enable Readline or ZLE to transmit long input, but correct
restoration and parser-error handling are both necessary.

### Separate: native BSD csh limits remain

Real BSD csh rejects the 12,000-byte single-word case with `Word too long` even
after the PTY limit has been bypassed. A long command made of shorter words
succeeds. Its `foreach` also has an argument-count limit; the follow-up uses 24
500-byte words instead of 6,000 one-byte words to avoid testing that separate
native restriction.

These are native shell restrictions, not evidence that the transport failed to
deliver all bytes. The test executable is actual BSD csh, not `/bin/csh` when
that path resolves to tcsh.

## Results

The initial CLI matrix contains 42 cases across six executable configurations.
The direct PTY matrix contains 36 cases and intentionally includes failing
canonical and noncanonical controls. These are diagnostic records, not an
all-passing acceptance suite.

| Shell | 12,000-byte word on the first line | Long command made of short words | Saved TTY modes in the following stdin consumer |
| --- | --- | --- | --- |
| Bash 5.3.9 | Exact payload | Exact payload | Restored |
| zsh 5.9 | Exact payload | Exact payload | Restored |
| dash 0.5.12 (`sh`) | Exact valid payload; parser-error case fails | Exact valid payload | Restored on successful cases |
| tcsh 6.24.13 | Exact payload | Exact payload | Restored |
| tcsh 6.20.00 | Exact payload | Exact payload | Restored |
| BSD csh | Native `Word too long` error | Exact payload | Restored on successful cases |

Successful POSIX open-quote continuation probes preserve the long prefix and
the subsequent newline/tail. Corrected tcsh continuation probes use `foreach`.
The final 24-word foreach probes pass on tcsh 6.24, tcsh 6.20, and BSD csh,
each producing the expected four-byte `loop` file; BSD csh returns to its native
prompt as designed.
The initial C-shell quote-continuation cases used POSIX multiline single quotes;
they are not treated as regressions because that input is not valid C-shell
continuation syntax. Corrected results are stored separately in the follow-up
report.

BSD csh deliberately returns to its native prompt until explicit `ish_recover`.
An `editor_ready=false` entry for that shell alone is not a failure. Exact files,
terminal settings, and the native prompt are the relevant observations.

With the candidate installer present in isolated CLI fixture homes, **65 existing
regression tests passed in 235.876 seconds**. Coverage includes:

- Primary-prompt Ctrl+C and waiting stdin consumers.
- Enter typeahead, multiline paste, slow commands, and Python tool handoffs.
- Cancellation during large submissions and a program that keeps reading after
  handling SIGINT.
- tcsh history, runtime hook replacement, periodic hooks, and foreach interruption.
- Vim and man return on every supported adapter, including immediate editing.

The compatibility run uses actual BSD csh and the separate tcsh 6.20 executable.
These passing tests do not clear the new long-input failures: most established
cases have short first physical lines and consequently retain the normal send
path. The complete repository suite was not rerun for this rejected candidate.
Ruff lint and format checks pass for all 63 checked Python files (existing
source/tests plus the five explicit diagnostic scripts). `git diff --check`
also passes.

The host is WSL2 Linux 6.6.87.2 and Python 3.12.14. No corporate RHEL 8.10 host,
Bash 4.4.20, or zsh 5.5.1 was available, so this investigation does not establish
compatibility with those installations or their startup files.

## Local evidence and reproduction

The experimental scripts under `dev/` and evidence under `dist/` remain excluded
from Git, as requested for those directories. They are local diagnostic files;
normal ish startup does not load them.

- `dev/long_input_candidate.py`: isolated first-line staging candidate.
- `dev/long_input_probe.py`: direct PTY comparisons.
- `dev/long_input_ish_probe.py`: actual ish CLI matrix.
- `dev/long_input_followups.py`: corrected continuations and repeated dash controls.
- `dev/long_input_regressions.py`: existing tests with an isolated candidate installer.
- `dist/long-input-mode-probe.json`: direct terminal results.
- `dist/long-input-ish-probe.json`: initial 42-case CLI results.
- `dist/long-input-followups.json`: corrected C-shell and repeated dash cases.
- `dist/long-input-dash-control.json`: identical long input under two TTY modes.
- `dist/long-input-regressions.log`: 65 passing existing regression tests.
- `dist/long-input-*-*.pty`: raw synthetic follow-up terminal transcripts.

Run from the project root in WSL:

```sh
PYTHONPATH=src:tests:dev .venv/bin/python -B dev/long_input_probe.py
PYTHONPATH=src:tests:dev .venv/bin/python -B dev/long_input_ish_probe.py
PYTHONPATH=src:tests:dev .venv/bin/python -B dev/long_input_followups.py
ISH_TEST_CSH="$PWD/dist/stability-tools/root/usr/bin/bsd-csh" \
ISH_TEST_TCSH="$PWD/dist/tcsh-compat/tcsh-6.20.00/tcsh" \
PYTHONPATH=src:tests:dev .venv/bin/python -B dev/long_input_regressions.py
```

The legacy import-time generator `tests/test.py` is not imported or executed.

## Requirements before applying a replacement

A production implementation must account for parser aborts before a final
newline, distinguish subsequent command input from program stdin, preserve
typeahead on both sides of cancellation, and restore the actual saved terminal
settings without racing a new input owner. Passing long, valid first-line tests
alone does not meet these requirements.

A deliberately narrower capability for selected shells and first physical lines
could be evaluated separately, with unsupported cases rejected before any part
of a block executes. That narrower behavior was not silently substituted for the
requested general strengthening in this change.
