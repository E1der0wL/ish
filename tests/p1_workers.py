"""Importable Python tool fixtures for spawn-based tests."""

import os
import sys
import time


def terminal_echo():
    """Print the terminal input and PTY state received by the worker."""
    print("TTY", os.isatty(0), os.tcgetpgrp(0) == os.getpgrp(), flush=True)
    print("INPUT:" + input(), flush=True)


def large_output():
    """Write a large payload and end marker to test output draining at exit."""
    sys.stdout.write("x" * 200000 + "END_OF_TOOL\n")


def wait_forever():
    """Keep a worker running indefinitely to test cancellation and termination."""
    print("WORKER_READY", flush=True)
    time.sleep(30)


def exit_seven():
    """Return nonzero exit code 7 to the parent."""
    raise SystemExit(7)
