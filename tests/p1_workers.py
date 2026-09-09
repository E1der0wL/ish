"""Importable Python tool fixtures for spawn-based tests."""
import os
import sys
import time


def terminal_echo():
    print('TTY', os.isatty(0), os.tcgetpgrp(0) == os.getpgrp(), flush=True)
    print('INPUT:' + input(), flush=True)


def large_output():
    sys.stdout.write('x' * 200000 + 'END_OF_TOOL\n')


def wait_forever():
    print('WORKER_READY', flush=True)
    time.sleep(30)


def exit_seven():
    raise SystemExit(7)
