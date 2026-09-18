#!/bin/sh
# Run the Python build driver from the project root with locked build dependencies.
set -eu

if [ "$(uname -s)" != "Linux" ]; then
    printf '%s\n' 'Build ish on Linux, or inside WSL on Windows.' >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'uv was not found. Install uv and add it to PATH before building ish.' >&2
    exit 127
fi

build_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd -- "$build_script_dir/.."

# exec preserves the build exit status and forwards signals directly to uv.
exec uv run --locked --no-default-groups --group build \
    python "$build_script_dir/build_distribution.py" "$@"
