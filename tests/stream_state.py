"""Collect test failure evidence without reaping, signalling or reading a child."""

from __future__ import annotations

import json
import os
import signal
import termios
from pathlib import Path

INPUT_BYTES = 1000
OUTPUT_BYTES = 4000
STATUS_FIELDS = {
    "State",
    "PPid",
    "NSpgid",
    "NSsid",
    "SigPnd",
    "ShdPnd",
    "SigBlk",
    "SigIgn",
    "SigCgt",
}


def snapshot(pid, fd, *, returncode=None, cleanup=False):
    """Observe process/PTY state; leave waitpid ownership with the calling fixture."""
    result = {"pid": pid, "phase": "cleanup" if cleanup else "before-cleanup"}
    child = result["child"] = {}
    if returncode is not None:
        child.update(state="reaped", returncode=returncode)
    elif pid is None:
        child["state"] = "not-started"
    else:
        try:
            event = os.waitid(
                os.P_PID, pid, os.WEXITED | os.WSTOPPED | os.WNOHANG | os.WNOWAIT
            )
            if event is None:
                child["state"] = "no-wait-event"
            elif event.si_code == os.CLD_EXITED:
                child.update(state="exited", returncode=event.si_status)
            elif event.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                child.update(
                    state="signaled",
                    returncode=-event.si_status,
                    signal=signal.Signals(event.si_status).name,
                    core_dumped=event.si_code == os.CLD_DUMPED,
                )
            else:
                child.update(state="stopped", signal=event.si_status)
        except (AttributeError, OSError) as error:
            child.update(state="unavailable", error=repr(error))
    if pid is not None:
        for name, operation in (("pgid", os.getpgid), ("sid", os.getsid)):
            try:
                result[name] = operation(pid)
            except OSError as error:
                result[name] = repr(error)
        # Linux-only detail supplements waitid; its absence never changes the test.
        proc = Path("/proc") / str(pid)
        try:
            result["proc_status"] = {
                key: value.strip()
                for line in (proc / "status").read_text().splitlines()
                if ":" in line
                for key, value in (line.split(":", 1),)
                if key in STATUS_FIELDS
            }
            result["wchan"] = (proc / "wchan").read_text().strip()
        except OSError as error:
            result["proc_error"] = repr(error)
    if fd is not None:
        try:
            result["foreground_pgid"] = os.tcgetpgrp(fd)
        except OSError as error:
            result["foreground_pgid"] = repr(error)
        try:
            attrs = termios.tcgetattr(fd)
            result["tty"] = {
                "iflag": attrs[0],
                "oflag": attrs[1],
                "cflag": attrs[2],
                "lflag": attrs[3],
                "vintr": repr(attrs[6][termios.VINTR]),
            }
        except (OSError, termios.error) as error:
            result["tty_error"] = repr(error)
    return result


def failure(reason, state, output, sent=b"", *, pty_end=None):
    """Keep process evidence distinct from the PTY's EIO/EOF observation."""
    evidence = {"process": state, "pty_end": pty_end}
    return (
        f"{reason}\nstate: {json.dumps(evidence, sort_keys=True)}"
        f"\nrecent input: {bytes(sent[-INPUT_BYTES:])!r}"
        f"\nrecent output: {bytes(output[-OUTPUT_BYTES:])!r}"
    )
