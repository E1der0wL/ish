"""Run the experimental terminal pane without changing the existing ish CLI."""

import argparse
import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path

STOP_SIGNALS = ("SIGTERM", "SIGHUP")


async def run_terminal(app):
    """Convert CLI termination signals into the application's normal cleanup."""
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        for name in STOP_SIGNALS:
            signum = getattr(signal, name)
            previous[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, app.stop, signum)
        return await app.run()
    finally:
        for signum, handler in previous.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)


def main():
    """Select a native interactive shell and start the PTK terminal application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", default="tcsh", help="native shell executable")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--clipboard",
        choices=("off", "write", "read-write"),
        default="write",
        help="child clipboard access (default: write; host permissions still apply)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="MODULE:SETUP",
        help="load an explicit terminal UI plugin (none by default)",
    )
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
            run_terminal(
                TerminalApp(
                    shell,
                    args.cwd,
                    dump_state=args.dump_state,
                    plugins=args.plugin,
                    clipboard=args.clipboard,
                )
            )
        )
    except (OSError, RuntimeError) as exc:
        print(f"ish-vt: {exc}", file=sys.stderr)
        return 1
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
