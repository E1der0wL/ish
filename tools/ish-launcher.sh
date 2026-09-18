#!/bin/sh
# Resolve PATH invocations and symlinks without changing the caller's cwd.
set -eu
case "$0" in
    */*) ish_launcher=$0 ;;
    *) ish_launcher=$(command -v -- "$0") ;;
esac
ish_launcher=$(readlink -f -- "$ish_launcher")
ish_directory=$(dirname -- "$ish_launcher")
# Isolate interpreter imports without changing the environment passed to the shell.
# exec keeps the Python process responsible for signals, exit status, and cleanup.
exec "$ish_directory/python/bin/python3" -I -m ish.main "$@"
