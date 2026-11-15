# Editable shell integrations

These UTF-8 text files are read afresh when an `ish-stream` session explicitly
selects UI and a matching shell plugin. They are packaged as source resources,
not compiled into the relay. The product has no `--demo` option; editable UI
samples live separately in `examples/stream_plugins/`.
Copy this directory and select it with `--integration-dir /absolute/path` to
keep local changes outside the installed package. The directory is an explicit
replacement: a missing file is not silently taken from another installation.
Changes apply to the next session; running shells retain their startup snapshot.

The first line of every shell template must be `# ish-stream-api: 1`.
`@NAME@` placeholders are supplied by the owning startup adapter and replaced
once. Values inserted by Python are already quoted for that shell. Keep those
placeholders rather than inserting a session token or a helper path by hand.
An unknown placeholder or incompatible header disables optional UI before a user
shell starts. The shell runs without UI. Plain mode does not read these files.
Selecting a shell provider alone, without UI, also leaves these files unread.

## Shared contract

The existing ordered `P` (primary decoration), `H` (hide) and `B` (native popup
callback) records use a session nonce and increasing serial number. They are
written to the controlling terminal. They are not command execution or parser
input grants. A popup requires the actual native callback and a matching live
request to the shared socket helper. Preserve that acknowledgement boundary:
the helper waits without reading command stdin, and native editing resumes
after the popup closes. Display text alone never grants popup focus.

## Files and shell policy

- `bash.sh`: Bash 5.1+ hooks, native Readline guards and an optional F2 callback.
  Selecting a popup reserves F2 in the session's supported Bash editing keymaps. It opens a
  popup only at a confirmed primary. In continuation and `read -e`, it is
  consumed without opening a popup; previous Bash F2 bindings are not restored.
  External programs still receive their native input. Ordinary Enter stays
  native. Completion, search and custom reader guards remain necessary.
  A bar without a popup leaves the user's F2 bindings intact.
- `zsh.env`, `zsh.rc`, `zsh.zsh`: native startup path preservation and ZLE
  lifecycle/widget integration. Existing zsh key restoration is retained.
- `tcsh.csh` and `tcsh-*.csh`: existing tcsh startup and popup fragments.
  The loader joins statements onto one source line to preserve `postcmd`
  ordering. Use separate full-line comments; do not add multiline shell blocks,
  heredocs or trailing inline comments to these templates.

Keep shell policy constants at the top of the owning script. The Python
adapters select startup paths and quote runtime values; PTK rendering, input
queues, popup identity checks and transport remain shared implementation.
Current providers accept non-login interactive startup. Login and other native
arguments are passed unchanged to a plain shell when optional UI preparation
rejects them; those configurations do not gain unverified prompt support.

Only preparation failures are handled by the no-UI fallback. Arbitrary syntax
errors or behavioral changes in an edited script are not transactionally rolled
back after it has begun executing. Start without `--plugin` to bypass integration
entirely. Supported configurations and verified versions are documented in
`docs/PROMPT_STREAM.md` and `docs/STREAM_VALIDATION.md` in the repository.
