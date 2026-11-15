"""Test-only completion producer, byte-preserving tap and argv recorder.

The tap observes one cooperating generator before tcsh performs word splitting
and matching. It does not export tcsh's final candidates or grant UI ownership.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

GATE_SECONDS = 8


def save(kind, value):
    """Publish one complete record without racing the PTY test reader."""
    directory = Path(kind)
    directory.mkdir(exist_ok=True)
    target = directory / (uuid.uuid4().hex + ".json")
    pending = target.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    pending.replace(target)


def main():
    """Select the independent fixture by its executable name."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    kind = Path(sys.argv[0]).name
    if kind == "pick":
        save("arguments", sys.argv[1:])
    elif kind == "generate":
        config = json.loads(Path("producer.json").read_text())
        save("contexts", {"command_line": os.environ.get("COMMAND_LINE")})
        if config.get("gate"):
            deadline = time.monotonic() + GATE_SECONDS
            while not Path("release-generator").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("The test did not release its explicit gate")
                time.sleep(0.01)
        sys.stdout.buffer.write(bytes.fromhex(config["payload"]))
        sys.stderr.buffer.write(bytes.fromhex(config.get("stderr", "")))
        return config.get("status", 0)
    elif kind == "tap":
        # No screen data is read here. This is the existing generator's pipe.
        result = subprocess.run(["./generate"], capture_output=True, check=False)
        save(
            "taps",
            {
                "command_line": os.environ.get("COMMAND_LINE"),
                "stdout": result.stdout.hex(),
                "stderr": result.stderr.hex(),
                "status": result.returncode,
            },
        )
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    elif kind == "reader":
        Path("reader-ready").touch()
        save("stdin", {"bytes": sys.stdin.buffer.readline().hex()})
    else:
        raise ValueError(kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
