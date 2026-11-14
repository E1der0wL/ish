"""Run the experimental terminal pane without changing the existing ish CLI."""

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path


def main():
    """Select a native interactive shell and start the PTK terminal application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", default="tcsh", help="native shell executable")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dump-state",
        type=Path,
        help="write opt-in screen diagnostics (contains terminal text)",
    )
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This experiment currently requires Linux/WSL and a POSIX PTY")
    shell = shutil.which(args.shell)
    if shell is None:
        parser.error(f"Shell executable not found: {args.shell}")
    if not args.cwd.is_dir():
        parser.error(f"Working directory not found: {args.cwd}")
    from .app import TerminalApp

    try:
        result = asyncio.run(
            TerminalApp(shell, args.cwd, dump_state=args.dump_state).run()
        )
    except (OSError, RuntimeError) as exc:
        print(f"ish-vt: {exc}", file=sys.stderr)
        return 1
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
