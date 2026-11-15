"""Stream a native shell; enable the prompt-only PTK demonstration explicitly."""

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path


def main():
    """Run the isolated experiment without changing ish or ish-vt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", default="tcsh")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--demo",
        action="store_true",
        help="enable the experimental primary-prompt panel and F2 popup",
    )
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This experiment requires Linux/WSL and a controlling PTY")
    if not os.isatty(0) or not os.isatty(1):
        parser.error("stdin and stdout must be terminals")
    shell = shutil.which(args.shell)
    if shell is None:
        parser.error(f"Shell executable not found: {args.shell}")
    if not args.cwd.is_dir():
        parser.error(f"Working directory not found: {args.cwd}")
    from .app import App

    try:
        app = App([shell, "-i"], str(args.cwd), demo=args.demo)
        result = asyncio.run(app.run())
        if app.error:
            print(f"ish-stream: {app.error}", file=sys.stderr)
            return 1
        return result if result >= 0 else 128 - result
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ish-stream: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
