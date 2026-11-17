"""Check restoration against real host replies, not simulated terminal responses."""

from __future__ import annotations

import argparse
import os
import re
import select
import signal
import termios
import time
import traceback
import tty
from pathlib import Path

from terminal_host import HostTerminal, record
from test_terminal_cli import STARTUP_DEADLINE

MODES = (1, 6, 7, 12, 25, 1000, 1002, 1003, 1004, 1006, 2004)
REPORT = re.compile(rb"\x1b\[\?([0-9]+);([0-4])\$y")
CURSOR = re.compile(rb"\x1bP1\$r([1-6]) q\x1b\\")
POSITION = re.compile(rb"\x1b\[([0-9]+);([0-9]+)R")
ATTRIBUTES = re.compile(rb"\x1bP1\$r([0-9:;]*)m\x1b\\")
DEADLINE = 3
EXITS = ("normal", "hup", "term", "exception", "suspend")
FAILURE_PLUGIN = """PLUGIN_META = {"version": "1.0"}
TERMINAL_API = 1
from pathlib import Path
def setup(api):
    @api.bind("f3")
    def fail(event):
        (Path.home() / "exception-triggered").write_text("triggered")
        event.app.exit(exception=RuntimeError("restore probe exception"))
"""


def write(data):
    """Do not discard short writes on the actual host connection."""
    view = memoryview(data)
    while view:
        view = view[os.write(1, view) :]


def observe():
    """Accept only actual replies; a deadline never invents a starting value."""
    write(
        b"".join(f"\x1b[?{mode}$p".encode() for mode in MODES)
        + b"\x1bP$q q\x1b\\\x1b[6n\x1bP$qm\x1b\\"
    )
    end = time.monotonic() + DEADLINE
    data = bytearray()
    reports = {}
    cursor = None
    position = None
    attributes = None
    while time.monotonic() < end:
        if select.select([0], [], [], 0.05)[0]:
            data.extend(os.read(0, 65536))
        reports = {int(mode): int(value) for mode, value in REPORT.findall(data)}
        shapes = CURSOR.findall(data)
        cursor = int(shapes[-1]) if shapes else None
        positions = POSITION.findall(data)
        position = list(map(int, positions[-1])) if positions else None
        styles = ATTRIBUTES.findall(data)
        attributes = styles[-1].decode("ascii") if styles else None
        if (
            len(reports) == len(MODES)
            and cursor is not None
            and position is not None
            and attributes is not None
        ):
            break
    return {
        "modes": reports,
        "cursor": cursor,
        "position": position,
        "attributes": attributes,
    }


def apply(state):
    """Restore only observed mutable values, leaving unknown modes untouched."""
    if state["cursor"] is not None:
        write(f"\x1b[{state['cursor']} q".encode())
    # Tracking modes are mutually exclusive on Windows Terminal: resetting
    # another tracking mode after enabling 1000 can disable all tracking again.
    for value in (2, 1):
        for mode, observed in state["modes"].items():
            if observed == value:
                write(f"\x1b[?{mode}{'h' if value == 1 else 'l'}".encode())
    if state["position"] is not None:
        row, column = state["position"]
        write(f"\x1b[{row};{column}H".encode())
    if state["attributes"] is not None:
        write(f"\x1b[0m\x1b[{state['attributes']}m".encode())


def run_case(directory, shell, ending):
    """Use native shell lifetime and independently query the host after PTK exits."""
    directory.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "shell": shell, "exit": ending}
    terminal = None
    saved = termios.tcgetattr(0)
    initial = None
    try:
        tty.setraw(0)
        initial = observe()
        if not {6, 7, 25, 1004, 2004}.issubset(initial["modes"]):
            raise AssertionError("Host did not answer the required restoration queries")
        if any(initial["modes"][mode] not in (1, 2) for mode in (6, 7, 25, 1004, 2004)):
            raise AssertionError("Host does not expose required mutable mode values")
        # Deliberately start with non-default settings. This distinguishes exact
        # restoration from blindly disabling every mode during cleanup.
        selected = {
            "modes": dict(initial["modes"]),
            "cursor": initial["cursor"],
            "position": [3, 9],
            "attributes": "1;3;31;44" if initial["attributes"] is not None else None,
        }
        selected["modes"].update(
            {6: 1, 7: 2, 25: 2, 1000: 1, 1004: 1, 1006: 1, 2004: 1}
        )
        if initial["cursor"] is not None:
            selected["cursor"] = 3
            selected["modes"][12] = 1
        apply(selected)
        before = observe()
        if any(before[key] != selected[key] for key in ("modes", "cursor", "position")):
            raise AssertionError(
                ("Host rejected the test's starting settings", selected, before)
            )
        if before["attributes"] not in ("1;3;31;44", "0;1;3;31;44"):
            raise AssertionError(
                "Host did not confirm the selected test text attributes"
            )
        arguments = []
        if ending == "exception":
            plugin = directory / "plugins" / "failure"
            plugin.mkdir(parents=True)
            (plugin / "__init__.py").write_text(FAILURE_PLUGIN, encoding="utf-8")
            arguments = [
                "--plugin-dir",
                str(plugin.parent),
                "--plugin",
                "failure",
                "--log-level",
                "CRITICAL",
            ]
        terminal = HostTerminal(
            directory, shell=shell, plugins=False, extra_args=arguments
        )
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        if ending == "suspend":
            os.kill(terminal.pid, signal.SIGTSTP)

            def stopped():
                pid, status = os.waitpid(terminal.pid, os.WNOHANG | os.WUNTRACED)
                if not pid:
                    return False
                if not os.WIFSTOPPED(status):
                    terminal.status = os.waitstatus_to_exitcode(status)
                    raise AssertionError("ish exited instead of suspending")
                return True

            terminal.require(stopped)
            terminal.receive()
            during = observe()
            result["suspended_host"] = during
            result["suspended_termios_restored"] = (
                termios.tcgetattr(terminal.fd) == terminal.attrs
            )
            if during != before or not result["suspended_termios_restored"]:
                raise AssertionError(
                    ("Suspend did not restore the host", before, during)
                )
            os.kill(terminal.pid, signal.SIGCONT)
            terminal.submit("printf resumed > suspend-result")
            terminal.result("suspend-result", b"resumed")
            terminal.require(terminal.at_prompt)
            terminal.send(b"\x04")
            expected = 0
        elif ending == "normal":
            terminal.send(b"\x04")
            expected = 0
        elif ending == "exception":
            terminal.send(b"\x1bOR")
            expected = 1
        else:
            signum = signal.SIGHUP if ending == "hup" else signal.SIGTERM
            os.kill(terminal.pid, signum)
            expected = 128 + signum
        terminal.require(terminal.exited)
        terminal.receive()
        status = terminal.status
        if (
            ending == "exception"
            and (terminal.root / "exception-triggered").read_text() != "triggered"
        ):
            raise AssertionError("The explicit PTK exception was not triggered")
        outer_restored = termios.tcgetattr(terminal.fd) == terminal.attrs
        terminal.close()
        terminal = None
        after = observe()
        result.update(
            before=before,
            after=after,
            returncode=status,
            outer_termios_restored=outer_restored,
        )
        if status != expected or not outer_restored:
            raise AssertionError(
                ("Exit status or termios mismatch", status, expected, outer_restored)
            )
        if after != before:
            raise AssertionError(("Actual host state was not restored", before, after))
        result["status"] = "passed"
    except Exception:
        result.update(status="failed", error=traceback.format_exc())
    finally:
        try:
            if terminal is not None:
                # A failed stop-time assertion must not leave a stopped fixture.
                if ending == "suspend" and terminal.status is None:
                    os.kill(terminal.pid, signal.SIGCONT)
                terminal.close()
        except Exception:
            result.update(status="failed", cleanup_error=traceback.format_exc())
        finally:
            try:
                if initial is not None:
                    apply(initial)
            finally:
                termios.tcsetattr(0, termios.TCSANOW, saved)
                result["host_termios_restored"] = termios.tcgetattr(0) == saved
                if not result["host_termios_restored"]:
                    result.update(
                        status="failed", cleanup_error="Host termios was not restored"
                    )
                record(directory, "result.json", result)
    return result


def main():
    """Require actual terminal replies for all three shells and cleanup paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("An actual interactive host terminal is required")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    summary = {"status": "running", "expected": 15, "cases": []}
    for shell in ("tcsh", "bash", "zsh"):
        for ending in EXITS:
            name = f"{shell}-{ending}"
            result = run_case(directory / name, shell, ending)
            summary["cases"].append({"name": name, "status": result["status"]})
            record(directory, "summary.json", summary)
            if result["status"] != "passed":
                summary["status"] = "failed"
                record(directory, "summary.json", summary)
                print(
                    result.get(
                        "error", result.get("cleanup_error", "Restoration failed")
                    ),
                    flush=True,
                )
                return 1
    summary["status"] = "passed"
    record(directory, "summary.json", summary)
    print("Actual host restoration: 15 cases passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
