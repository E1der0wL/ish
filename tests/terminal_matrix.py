"""Verify all six native shell/UI combinations inside an actual host terminal."""

import argparse
import json
import os
from pathlib import Path

from terminal_host import HostRun, record
from terminal_scenarios import SHELLS


def main():
    """Preserve each completed result and fail if any required case did not finish."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--require-resize", action="store_true")
    parser.add_argument("--resize-each", action="store_true")
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("This host matrix requires an actual interactive terminal")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    summary = {"status": "running", "expected": 6, "cases": []}
    for shell in SHELLS:
        for plugins in (True, False):
            name = f"{shell}-{'plugins' if plugins else 'plain'}"
            require_resize = args.resize_each or (
                args.require_resize and not summary["cases"]
            )
            print(f"\r\nStarting {name}\r\n", flush=True)
            result = HostRun(
                directory / name, shell, plugins, require_resize=require_resize
            ).run()
            summary["cases"].append({"name": name, "status": result["status"]})
            record(directory, "summary.json", summary)
            if result["status"] != "passed":
                summary["status"] = "failed"
                record(directory, "summary.json", summary)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
    summary["status"] = "passed"
    record(directory, "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
